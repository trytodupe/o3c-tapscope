"""Live O3C key-depth view in the browser, independent of any game.

Polls the Col03 analog channel as fast as the device answers and pushes the newest
reading to a small page over Server-Sent Events, so three bars track the current
depth of Z / X / C (in mm) in real time. Read-only: it sends the same 0x15 frame the
capture tools use and writes nothing to the device.

    uv run python tools/live_depth.py
    # then open http://127.0.0.1:8770/ (opened automatically)

This is also the quickest way to see which analog channel a physical key drives: press
one key at a time and watch which bar moves.
"""

import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from calibration import STEP_UM, level_lut, load_calibration
from device_lock import DeviceBusy, DeviceLock
from hid_capture import devices
from level_protocol import READ_SIZE, USAGE_PAGE_ANALOG, build_command_report

TEMPLATE = Path(__file__).with_name("live_depth_template.html")
KEY_NAMES = ("Z", "X", "C")


class DeviceReader(threading.Thread):
    """Poll the analog channel and keep the newest levels under a lock."""

    def __init__(self, timeout_ms=80):
        super().__init__(daemon=True)
        self.timeout_ms = timeout_ms
        self.lock = threading.Lock()
        self.levels = [0] * len(KEY_NAMES)
        self.count = 0
        self.rate = 0.0
        self.error = ""
        self.stop_event = threading.Event()

    def snapshot(self):
        with self.lock:
            return {"levels": list(self.levels), "rate": round(self.rate, 1),
                    "count": self.count, "error": self.error}

    def run(self):
        import hid

        target = next((item for item in devices() if item.get("usage_page") == USAGE_PAGE_ANALOG), None)
        if target is None:
            with self.lock:
                self.error = "no O3C analog interface found"
            return
        try:
            handle = hid.device()
            handle.open_path(target["path"])
        except OSError as error:  # pragma: no cover - depends on the machine
            with self.lock:
                self.error = f"cannot open the device: {error}"
            return

        # Per-key info frames (cmd 0x14) carry that key's live level at payload[8];
        # reading each key explicitly also refreshes its channel, which the 0x15
        # broadcast does not do on its own.
        frames = [build_command_report(0x14, index, kind=0x04) for index in range(len(KEY_NAMES))]
        started = time.perf_counter()
        window = 0
        try:
            while not self.stop_event.is_set():
                levels = []
                try:
                    for frame in frames:
                        handle.write(frame)
                        raw = bytes(handle.read(READ_SIZE + 3072, self.timeout_ms))
                        levels.append(raw[8] if len(raw) > 8 else 0)
                except OSError as error:
                    with self.lock:
                        self.error = f"read failed: {error}"
                    break
                with self.lock:
                    self.levels = levels
                    self.count += 1
                window += 1
                now = time.perf_counter()
                if now - started >= 1.0:
                    with self.lock:
                        self.rate = window / (now - started)
                    started, window = now, 0
        finally:
            handle.close()


class LiveHandler(BaseHTTPRequestHandler):
    reader = None
    page = b""
    rate_hz = 60.0

    def log_message(self, *args):  # keep the console quiet
        pass

    def do_GET(self):
        if self.path.startswith("/events"):
            self.stream_events()
        elif self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.page)))
            self.end_headers()
            self.wfile.write(self.page)
        else:
            self.send_response(404)
            self.end_headers()

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        delay = 1.0 / max(1.0, self.rate_hz)
        try:
            while True:
                payload = json.dumps(self.reader.snapshot())
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def build_config(calibration):
    keys = []
    fallback = calibration.get("rt_range_mm")
    for key in calibration["keys"]:
        lut = level_lut(calibration.get("step_um", STEP_UM))
        rt_range = key.get("rt_range_mm") if key.get("rt_range_mm") is not None else fallback
        keys.append({
            "name": key.get("name") or KEY_NAMES[key["index"] % len(KEY_NAMES)],
            "lut_mm": lut,
            "travel_mm": round(max(lut), 3),
            "rt_range_mm": list(rt_range or [None, None])[:2],
        })
    return {
        "keys": keys[: len(KEY_NAMES)],
        "step_um": calibration.get("step_um", STEP_UM),
    }


def main():
    parser = argparse.ArgumentParser(description="Live O3C key depth in the browser")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--rate-hz", type=float, default=60.0, help="how often the page is updated")
    parser.add_argument("--calibration", type=Path, default=Path(__file__).with_name("calibration.example.json"))
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    calibration = load_calibration(args.calibration)
    config = build_config(calibration)

    try:
        device_lock = DeviceLock()
    except DeviceBusy as error:
        raise SystemExit(f"{error}; stop the other studio/live_depth first")

    reader = DeviceReader()
    reader.start()

    LiveHandler.reader = reader
    LiveHandler.rate_hz = args.rate_hz
    LiveHandler.page = TEMPLATE.read_text(encoding="utf-8").replace(
        "__CONFIG__", json.dumps(config)).encode("utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), LiveHandler)
    url = f"http://127.0.0.1:{args.port}/"
    travel = ", ".join(f"{key['name']} {key['travel_mm']:.2f} mm" for key in config["keys"])
    print(f"live depth : {url}")
    print(f"travel     : {travel}")
    if config["rt_range_mm"]:
        print("rt range   : " + " - ".join(f"{value:.2f}" for value in config["rt_range_mm"]) + " mm")
    print("press one key at a time to see which channel it drives; Ctrl+C to stop")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop_event.set()
        device_lock.close()
        server.shutdown()


if __name__ == "__main__":
    main()

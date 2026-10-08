"""Capture analog key depth and host-level keyboard events on one monotonic clock.

Two streams are recorded into a single NDJSON file:

    {"type": "levels",   "host_ns": .., "levels": [lv0, lv1, lv2]}
    {"type": "keyboard", "host_ns": .., "vk": .., "scan": .., "down": .., "injected": ..}

With ``--state-url`` a third stream is added. It polls a community state reader
(tosu), which knows the map clock the replay is written in, and records it on the
same clock:

    {"type": "state", "host_ns": .., "map_time": .., "playing": .., "checksum": ..}

That stream is a coarse anchor for the later alignment; tools/game_state.py explains
the accuracy model (tosu's map time comes from its precise loop, ~10 ms by default).

The level stream polls the Col03 channel (report 0x22) with a per-key info frame
(cmd 0x14) for every switch, ~600 Hz for all three together. Each answer carries that
key's live level at payload[8], and polling a key is also what keeps its channel fresh
- the 0x15 broadcast on its own leaves the middle channel stale - so all three keys are
polled explicitly. The keyboard stream is a
WH_KEYBOARD_LL hook, which records the logical key events the desktop receives.
hidapi cannot read the device's keyboard collections because Windows claims them for
the keyboard class driver, so the hook is the only way to observe the host-side
effect. A double click can then be attributed either to a real depth excursion or to
the HID output.

Writing is delegated to tools/ndjson_writer.py: producers only push to a queue, the
writer thread batches the flushes and keeps the file inside ``--window-min`` by
dropping the oldest records.
"""

import argparse
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from hid_capture import devices, identify
from device_lock import DeviceBusy, DeviceLock
from game_state import DEFAULT_URL, StatePoller
from level_protocol import (
    LEVEL_COUNT,
    READ_SIZE,
    USAGE_PAGE_ANALOG,
    build_command_report,
)
from ndjson_writer import NdjsonWriter

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_THREAD_STOP = 0x8000
KEY_MESSAGES = (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP)
DOWN_MESSAGES = (WM_KEYDOWN, WM_SYSKEYDOWN)
LLKHF_INJECTED = 0x10


class KeyboardHookStruct(ctypes.Structure):
    _fields_ = [
        ("vk_code", wintypes.DWORD),
        ("scan_code", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra_info", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def _load_user32():
    library = ctypes.WinDLL("user32", use_last_error=True)
    library.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
    library.SetWindowsHookExW.restype = ctypes.c_void_p
    library.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    library.UnhookWindowsHookEx.restype = wintypes.BOOL
    library.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    library.CallNextHookEx.restype = ctypes.c_ssize_t
    library.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
    library.GetMessageW.restype = ctypes.c_int
    library.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    library.PostThreadMessageW.restype = wintypes.BOOL
    return library


USER32 = _load_user32()
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.GetCurrentThreadId.restype = wintypes.DWORD


class Recorder:
    """Hand records to the writer thread; this class never touches the disk itself.

    A producer runs inside the keyboard hook or the poll loop, where a synchronous
    flush would delay the whole desktop, so the write path is only a queue push.
    """

    def __init__(self, path, window_s=0.0):
        self.writer = NdjsonWriter(path, window_s=window_s)

    @property
    def counts(self):
        return self.writer.counts

    def write(self, record):
        self.writer.write(record)

    def stats(self):
        return self.writer.stats()

    def compact(self):
        self.writer.compact()

    def close(self):
        self.writer.close()


def analog_loop(handle, recorder, stop, started, requests):
    """Poll every key's info frame and record the three live levels on one clock."""
    timeouts = 0
    consecutive = 0
    warned = False
    try:
        while not stop.is_set():
            levels = []
            for index, frame in requests:
                handle.write(frame)
                # Read until our own answer shows up, dropping anything else. A second
                # reader on the same HID path - or a stale queued report - makes a read
                # return another request's answer; abandoning the queue here instead of
                # draining it would let it grow until every read is off by one and the
                # stream stalls forever. Draining lets the loop resync once the other
                # reader goes away.
                deadline = time.perf_counter() + 0.05
                value = None
                while time.perf_counter() < deadline:
                    raw = bytes(handle.read(READ_SIZE, 5))
                    if len(raw) > 8 and raw[6] == 0x14 and raw[7] == index:
                        value = raw[8]
                        break
                if value is None:
                    timeouts += 1
                    levels = None
                    break
                levels.append(value)
            if levels is None:
                consecutive += 1
                # A few timeouts are normal; a long run means replies are going
                # somewhere else (a second reader on the same HID path) or the device
                # stopped answering. Say so once instead of stalling silently.
                if consecutive >= 20 and not warned:
                    warned = True
                    recorder.write({
                        "type": "note",
                        "message": (
                            f"no valid answer from the device for {consecutive} polls; another "
                            "process may be reading the same HID path (close live_depth or a "
                            "second studio), or the device stopped responding"
                        ),
                    })
                continue
            consecutive = 0
            if warned:
                warned = False
                recorder.write({"type": "note", "message": "", "recovered": True})
            recorder.write(
                {
                    "type": "levels",
                    "host_ns": time.perf_counter_ns() - started,
                    "levels": levels,
                }
            )
    except OSError as error:
        recorder.write({"type": "note", "message": f"analog stream stopped: {error!r}"})
    recorder.write({"type": "analog_end", "timeouts": timeouts})


class KeyboardHook(threading.Thread):
    """Low-level keyboard hook; it receives events only while its own thread pumps messages."""

    def __init__(self, recorder, stop, started):
        super().__init__(daemon=True)
        self.recorder = recorder
        self.stop_event = stop
        self.started = started
        self.thread_id = None
        self.installed = threading.Event()
        self.hook = None
        self.proc = HOOKPROC(self._handle)

    def _handle(self, code, wparam, lparam):
        message = int(wparam)
        if code >= 0 and message in KEY_MESSAGES:
            info = ctypes.cast(lparam, ctypes.POINTER(KeyboardHookStruct)).contents
            self.recorder.write(
                {
                    "type": "keyboard",
                    "host_ns": time.perf_counter_ns() - self.started,
                    "vk": int(info.vk_code),
                    "scan": int(info.scan_code),
                    "down": message in DOWN_MESSAGES,
                    "injected": bool(info.flags & LLKHF_INJECTED),
                }
            )
        return USER32.CallNextHookEx(None, code, wparam, lparam)

    def run(self):
        self.thread_id = KERNEL32.GetCurrentThreadId()
        self.hook = USER32.SetWindowsHookExW(WH_KEYBOARD_LL, self.proc, None, 0)
        if not self.hook:
            self.recorder.write(
                {"type": "note", "message": f"SetWindowsHookExW failed: {ctypes.get_last_error()}"}
            )
            self.installed.set()
            return
        self.installed.set()
        message = wintypes.MSG()
        while not self.stop_event.is_set():
            result = USER32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1) or message.message == WM_THREAD_STOP:
                break
            USER32.TranslateMessage(ctypes.byref(message))
            USER32.DispatchMessageW(ctypes.byref(message))
        USER32.UnhookWindowsHookEx(self.hook)

    def shutdown(self):
        if self.thread_id is not None:
            USER32.PostThreadMessageW(self.thread_id, WM_THREAD_STOP, 0, 0)


def main():
    parser = argparse.ArgumentParser(description="Capture key depth together with host keyboard events")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--out", type=Path, default=Path("tap-capture.jsonl"))
    parser.add_argument(
        "--key",
        type=int,
        default=0,
        help="deprecated and ignored; every key is read via cmd 0x14",
    )
    parser.add_argument("--stop-file", type=Path, help="stop cleanly once this file exists")
    parser.add_argument(
        "--state-url",
        nargs="?",
        const=DEFAULT_URL,
        help="also record the live map time from a state reader (tosu) at this URL",
    )
    parser.add_argument("--state-poll-ms", type=float, default=50.0)
    parser.add_argument(
        "--window-min",
        type=float,
        default=10.0,
        help="keep only this many minutes of capture in the file (0 keeps everything)",
    )
    args = parser.parse_args()

    found = devices()
    if args.list:
        for index, device in enumerate(found):
            print(json.dumps({"index": index, **identify(device)}, ensure_ascii=False))
        return

    analog = [d for d in found if d.get("vendor_id") == 0x8089 and d.get("usage_page") == USAGE_PAGE_ANALOG]
    if not analog:
        raise SystemExit("No analog interface (usage_page 0xFF12) found; run --list.")

    import hid

    try:
        device_lock = DeviceLock()
    except DeviceBusy as error:
        raise SystemExit(f"{error}; stop the other studio/live_depth first")

    # cmd 0x14 per key: payload[8] is that key's live level, and the read is what
    # keeps the channel fresh (the 0x15 broadcast alone leaves the middle one stale).
    requests = [(index, build_command_report(0x14, index, kind=0x04))
                for index in range(LEVEL_COUNT)]

    started = time.perf_counter_ns()
    stop = threading.Event()
    threads = []
    recorder = Recorder(args.out, window_s=args.window_min * 60.0)
    recorder.write(
        {
            "type": "metadata",
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "timestamp_source": "native_perf_counter_ns",
            "analog": identify(analog[0]),
            "level_command": "0x14",
            "level_count": LEVEL_COUNT,
            "window_s": args.window_min * 60.0,
            "keyboard_source": "WH_KEYBOARD_LL",
            "state": None if not args.state_url else {
                "source": "state_reader",
                "url": args.state_url,
                "poll_ms": args.state_poll_ms,
            },
        }
    )
    analog_handle = hid.device()
    analog_handle.open_path(analog[0]["path"])
    analog_handle.set_nonblocking(False)
    threads.append(
        threading.Thread(target=analog_loop, args=(analog_handle, recorder, stop, started, requests), daemon=True)
    )
    hook = KeyboardHook(recorder, stop, started)
    threads.append(hook)

    if args.state_url:
        def emit_state(state, recorder=recorder, started=started):
            # The whole file shares one clock base, so the reader's stamp is
            # rebased onto this run's start like every other stream.
            record = {"type": "state", "host_ns": int(state["host_ms"] * 1e6) - started}
            for key in (
                "map_time", "state", "state_name", "playing", "focused", "paused",
                "checksum", "beatmap_file", "artist", "title", "version",
            ):
                record[key] = state[key]
            recorder.write(record)

        threads.append(
            StatePoller(emit_state, url=args.state_url, poll_ms=args.state_poll_ms, stop=stop)
        )

    for thread in threads:
        thread.start()
    hook.installed.wait(timeout=2.0)
    print(f"{len(threads)} streams -> {args.out}; press Ctrl+C or wait for --duration.", file=sys.stderr)
    try:
        while True:
            if args.stop_file and args.stop_file.exists():
                break
            if args.duration and (time.perf_counter_ns() - started) / 1e9 >= args.duration:
                break
            time.sleep(0.2)
            if not any(thread.is_alive() for thread in threads):
                break
    except KeyboardInterrupt:
        pass
    stop.set()
    hook.shutdown()
    for thread in threads:
        thread.join(timeout=2.0)
    analog_handle.close()
    device_lock.close()
    elapsed = (time.perf_counter_ns() - started) / 1e9
    # Trim before reporting: the end record carries what the window dropped, and
    # close() trims again after this record has been written.
    recorder.compact()
    stats = recorder.stats()
    recorder.write({
        "type": "end",
        "seconds": elapsed,
        "counts": recorder.counts,
        "window_s": stats["window_s"],
        "dropped_window": stats["dropped_window"],
        "dropped_overflow": stats["dropped_overflow"],
    })
    recorder.close()
    print(json.dumps({"seconds": round(elapsed, 2), **recorder.counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()

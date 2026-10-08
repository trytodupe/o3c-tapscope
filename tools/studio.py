"""One local process that owns the O3C, records sessions and aligns exported replays.

The browser cannot read the analog channel (the keyboard collections are claimed by
the Windows keyboard class driver), so the control UI has to sit on top of a native
process. This daemon does three things at once:

* live depth for the page, over Server-Sent Events, at the same ~650 Hz the capture
  tools use - one device owner, so nothing fights over the HID handle;
* an arm/disarm recording session written as the usual tap NDJSON
  (``levels`` / ``keyboard`` / ``state``), one file per Start/Stop;
* a watcher on the osu! replay folder. When an exported ``.osr`` settles it runs
  ``replay_view.py`` on the capture, and that picks the *last* play in the session
  with the tosu state stream as the coarse anchor.

A capture session usually holds many plays; you export the one you care about, which
is almost always the one that just ended, and the tosu ``state`` stream is what makes
"the last play" precise (the map clock restarts every play, so the final run is a
whole play).

    uv run python tools/studio.py
    # then open http://127.0.0.1:8770/

Read-only on the device: the same confirmed 0x14 info frames the capture uses.
"""

import argparse
import json
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import settings as settings_module
from device_lock import DeviceBusy, DeviceLock
from game_state import StatePoller
from hid_capture import devices, identify
from level_protocol import LEVEL_COUNT, USAGE_PAGE_ANALOG, build_command_report
from live_depth import build_config
from ndjson_writer import NdjsonWriter
from replay_view import ReplaySpec, render_page, stage_replay, stage_skin

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
TEMPLATE = TOOLS / "studio.html"

SHELL_URL = "/output/replay.html"

DEFAULT_WINDOW_MIN = 10.0
MAX_WINDOW_MIN = 120.0
DEFAULT_WINDOW_S = DEFAULT_WINDOW_MIN * 60.0


def window_seconds(text):
    """Window length in seconds from an UI value in minutes.

    None (nothing sent), a missing value or 0 all mean "no window"; anything else is
    clamped, so a hand-written request cannot ask for an unbounded file.
    """
    if text is None or text == "":
        return None
    try:
        minutes = float(text)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return 0.0
    return min(minutes, MAX_WINDOW_MIN) * 60.0


class StudioRecorder:
    """Fan the three streams into the live snapshot and (when armed) the session file.

    The device, keyboard hook and tosu poller run for the whole process; recording is
    only the act of attaching a file, so live depth keeps working between sessions.
    Producers never touch the file itself: they update the snapshot and hand the record
    to the writer's queue, which is what keeps a slow disk out of the keyboard hook.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.writer = None
        self.counts = {}
        self.levels = [0] * LEVEL_COUNT
        self.count = 0
        self.rate = 0.0
        self.error = ""
        self.window_s = 0.0
        self._window_start = time.perf_counter()
        self._window = 0

    def is_armed(self):
        with self.lock:
            return self.writer is not None

    def session_samples(self):
        """Level records written in the current (or last) session, not since startup."""
        with self.lock:
            return self.counts.get("levels", 0)

    def snapshot(self):
        with self.lock:
            return {
                "levels": list(self.levels),
                "rate": round(self.rate, 1),
                "count": self.count,
                "error": self.error,
            }

    def open(self, path, window_s=0.0):
        with self.lock:
            self.writer = NdjsonWriter(path, window_s=window_s)
            self.counts = {}
            self.window_s = window_s

    def close(self):
        with self.lock:
            writer, self.writer = self.writer, None
        if writer is not None:
            writer.close()

    def pin(self, lo_ns):
        """Keep the rolling window from dropping a play that has just ended."""
        with self.lock:
            writer = self.writer
        if writer is not None:
            writer.pin(lo_ns)

    def unpin(self):
        with self.lock:
            writer = self.writer
        if writer is not None:
            writer.unpin()

    def stats(self):
        with self.lock:
            writer = self.writer
        return writer.stats() if writer is not None else {}

    def compact(self):
        """Trim the session file to its window before the caller reports totals."""
        with self.lock:
            writer = self.writer
        if writer is not None:
            writer.compact()

    def write(self, record):
        kind = record.get("type")
        with self.lock:
            if kind == "levels":
                self.levels = list(record["levels"])
                self.count += 1
                self._window += 1
                now = time.perf_counter()
                if now - self._window_start >= 1.0:
                    self.rate = self._window / (now - self._window_start)
                    self._window_start, self._window = now, 0
            elif kind == "note":
                self.error = "" if record.get("recovered") else record.get("message", "")
            writer = self.writer
            if writer is None:
                return
            self.counts[kind] = self.counts.get(kind, 0) + 1
        writer.write(record)


class ReplayWatcher:
    """Detect an exported .osr only once its size and mtime stop changing.

    osu! may still be writing when the file first appears, so a single sighting is not
    enough - the second identical signature is taken as "written".
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.known = set()
        self.pending = {}

    def prime(self):
        try:
            files = list(self.directory.glob("*.osr"))
        except OSError:
            files = []
        self.known.update(path.name for path in files)

    def scan_once(self):
        ready = []
        try:
            files = list(self.directory.glob("*.osr"))
        except OSError:
            return ready
        for path in files:
            if path.name in self.known:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            if self.pending.get(path.name) == signature:
                self.known.add(path.name)
                self.pending.pop(path.name, None)
                ready.append(path)
            else:
                self.pending[path.name] = signature
        return ready


class PlayRuns:
    """Pin the window on a finished play so a late export can still be aligned.

    A ``playing`` false -> true transition starts a run; true -> false pins the writer
    at that run's first sample. The next run releases the pin, so at most one finished
    play is held past the window, and the pin's own hold bounds the file length.
    """

    def __init__(self, recorder, poll_ms):
        self.recorder = recorder
        # The run start is only known to one poll period, so back-date it to make sure
        # the first sample of the play cannot fall outside the pinned range.
        self.back_ns = int(poll_ms * 1e6)
        self.running = False
        self.run_start_ns = None

    def update(self, state, host_ns):
        playing = bool(state.get("playing"))
        if playing and not self.running:
            self.recorder.unpin()
            self.run_start_ns = host_ns - self.back_ns
        elif not playing and self.running and self.run_start_ns is not None:
            self.recorder.pin(self.run_start_ns)
        self.running = playing


class ReplayStore:
    """The aligned-replay manifest: one JSON list that survives a restart.

    The studio only writes here; the payloads the entries point at are written next to
    each replay by ``stage_replay``. ``path=None`` keeps the list in memory, which is
    what the tests use.
    """

    def __init__(self, path):
        self.path = None if path is None else Path(path)
        self.lock = threading.Lock()
        self.entries = self._read()

    def _read(self):
        if self.path is None:
            return []
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return loaded if isinstance(loaded, list) else []

    def list(self):
        with self.lock:
            return list(self.entries)

    def add(self, entry):
        with self.lock:
            self.entries = [item for item in self.entries if item.get("id") != entry["id"]]
            self.entries.insert(0, entry)
            self._write()

    def _write(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        temporary.replace(self.path)


class Studio:
    """Recording sessions, replay handling and the status the page polls."""

    def __init__(self, recorder, captures_dir, render, analog_info, tosu_url, state_poll_ms,
                 default_window_s=DEFAULT_WINDOW_S, store=None, settings=None, settings_path=None):
        self.lock = threading.Lock()
        self.recorder = recorder
        self.captures_dir = Path(captures_dir)
        self.render = render
        self.analog_info = analog_info
        self.tosu_url = tosu_url
        self.state_poll_ms = state_poll_ms
        self.default_window_s = default_window_s
        self.started = time.perf_counter_ns()
        self.arm_started = None
        self.capture_path = None
        self.store = store if store is not None else ReplayStore(None)
        self.settings = settings or {}
        self.settings_path = settings_path
        self.watch_status = "watching"
        self.watch_dir = ""
        self.poller = None

    def arm(self, window_s=None):
        if self.recorder.is_armed():
            return
        if window_s is None:
            window_s = self.default_window_s
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        path = self.captures_dir / f"tap-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        self.recorder.open(path, window_s=window_s)
        self.arm_started = time.perf_counter_ns()
        self.recorder.write({
            "type": "metadata",
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "timestamp_source": "native_perf_counter_ns",
            "analog": self.analog_info,
            "level_command": "0x14",
            "level_count": LEVEL_COUNT,
            "window_s": window_s,
            "keyboard_source": "WH_KEYBOARD_LL",
            "state": {
                "source": "state_reader",
                "url": self.tosu_url,
                "poll_ms": self.state_poll_ms,
            },
        })
        with self.lock:
            self.capture_path = path

    def disarm(self):
        if not self.recorder.is_armed():
            return
        # Trim first: the end record reports what the window dropped, and the final
        # compaction inside close() would otherwise happen after it.
        self.recorder.compact()
        stats = self.recorder.stats()
        self.recorder.write({
            "type": "end",
            "seconds": round((time.perf_counter_ns() - self.arm_started) / 1e9, 2),
            "counts": dict(self.recorder.counts),
            "window_s": stats.get("window_s", 0.0),
            "dropped_window": stats.get("dropped_window", 0),
            "dropped_overflow": stats.get("dropped_overflow", 0),
        })
        self.recorder.close()

    def _tosu_state(self):
        poller = self.poller
        last = poller.last if poller is not None else None
        return {
            "url": self.tosu_url,
            "connected": bool(poller and poller.samples),
            "samples": poller.samples if poller else 0,
            "errors": poller.errors if poller else 0,
            "playing": bool(last.get("playing")) if last else False,
            "checksum": (last or {}).get("checksum", ""),
            "artist": (last or {}).get("artist", ""),
            "title": (last or {}).get("title", ""),
            "version": (last or {}).get("version", ""),
        }

    def status(self):
        snapshot = self.recorder.snapshot()
        with self.lock:
            capture = self.capture_path
            watch_status = self.watch_status
        pages = self.store.list()
        return {
            "armed": self.recorder.is_armed(),
            "capture": capture.name if capture else None,
            "capture_path": str(capture) if capture else None,
            "samples": self.recorder.session_samples(),
            "rate": snapshot["rate"],
            "window_s": self.recorder.window_s,
            "device_error": snapshot["error"],
            "tosu": self._tosu_state(),
            "watch": {"status": watch_status, "dir": self.watch_dir},
            "pages": pages,
        }

    def handle_replay(self, osr_path):
        """Auto path: align an exported replay against the session that just ended."""
        with self.lock:
            capture = self.capture_path
        if capture is None:
            self._set_watch(f"{osr_path.name}: no capture recorded yet")
            return
        self._set_watch(f"aligning {osr_path.name}…")
        try:
            self.stage(osr_path, capture)
        except (Exception, SystemExit) as error:  # a bad replay must not kill the watcher
            self._set_watch(f"{osr_path.name}: {error}")
            return
        self._set_watch(f"{osr_path.name} aligned")

    def stage(self, osr_path, capture_path):
        """Align one replay against one capture, list it and return the entry."""
        slug = f"{time.strftime('%Y%m%d-%H%M%S')}-{osr_path.stem}"
        entry = self.render(osr_path, capture_path, slug)
        entry.setdefault("url", SHELL_URL + "?id=" + urllib.parse.quote(entry["id"], safe=""))
        self.store.add(entry)
        return entry

    def sources(self):
        """What the manual "view a replay" picker can offer."""
        with self.lock:
            current = Path(self.capture_path).name if self.capture_path else ""
        return {
            "osu_root": self.settings.get("osu_root", ""),
            "replays": _file_list(self.watch_dir, "*.osr"),
            "captures": _file_list(self.captures_dir, "*.jsonl"),
            "current_capture": current,
        }

    def view(self, osr_name, capture_name):
        """Manual path: align a hand-picked replay against a hand-picked capture.

        No tosu and no watcher involved - the whole point is that a replay plus a tap
        capture on disk is enough to produce a timeline page.
        """
        osr_path = _contained(self.watch_dir, osr_name, ".osr")
        if osr_path is None:
            raise ValueError("pick a replay from the list")
        if capture_name:
            capture_path = _contained(self.captures_dir, capture_name, ".jsonl")
        else:
            with self.lock:
                capture_path = self.capture_path
        if capture_path is None:
            raise ValueError("pick a capture from the list")
        return self.stage(osr_path, capture_path)["url"]

    def _set_watch(self, text):
        with self.lock:
            self.watch_status = text


class StudioHandler(SimpleHTTPRequestHandler):
    studio = None
    page = b""
    rate_hz = 60.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/events":
            return self.stream_events()
        if path == "/api/status":
            return self.send_json(self.studio.status())
        if path == "/api/pages":
            return self.send_json({"pages": self.studio.status()["pages"]})
        if path == "/api/settings":
            return self.send_json(self.studio.settings or {})
        if path == "/api/sources":
            return self.send_json(self.studio.sources())
        if path in ("/", "/index.html"):
            return self.send_page()
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/start":
            query = urllib.parse.parse_qs(parsed.query)
            self.studio.arm(window_seconds((query.get("window_min") or [None])[0]))
            return self.send_json(self.studio.status())
        if parsed.path == "/api/stop":
            self.studio.disarm()
            return self.send_json(self.studio.status())
        if parsed.path == "/api/settings":
            saved = settings_module.save(self.studio.settings_path, self.read_json())
            return self.send_json({"ok": True, "restart_required": True, "settings": saved})
        if parsed.path == "/api/pick-folder":
            return self.send_json({"path": pick_directory()})
        if parsed.path == "/api/view":
            payload = self.read_json()
            try:
                url = self.studio.view(payload.get("osr", ""), payload.get("capture", ""))
            except (Exception, SystemExit) as error:  # SystemExit is a BaseException
                return self.send_json({"ok": False, "error": str(error)})
            return self.send_json({"ok": True, "url": url})
        self.send_response(404)
        self.end_headers()

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def send_page(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.page)))
        self.end_headers()
        self.wfile.write(self.page)

    def send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        delay = 1.0 / max(1.0, self.rate_hz)
        try:
            while True:
                payload = json.dumps(self.studio.recorder.snapshot())
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def _file_list(folder, pattern):
    """Files directly in ``folder`` matching ``pattern``, newest first."""
    if not folder:
        return []
    try:
        files = [path for path in Path(folder).glob(pattern) if path.is_file()]
    except OSError:
        return []
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [{"name": path.name, "mtime": path.stat().st_mtime} for path in files]


def _contained(folder, name, suffix):
    """A file strictly inside ``folder`` with ``suffix``, or None (no path traversal)."""
    if not folder or not name or Path(name).name != name or not name.lower().endswith(suffix):
        return None
    path = Path(folder) / name
    return path if path.is_file() else None


def pick_directory():
    """Absolute folder path chosen in a native Windows dialog, or "" when cancelled.

    A browser cannot hand the server an absolute path, so the local process opens the
    dialog itself. A child process keeps the HTTP thread free of any GUI requirement.
    """
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = 'Select a folder';"
        "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def select_device(path=""):
    """The analog interface to own: the saved path when it matches, else the first."""
    analog = [item for item in devices() if item.get("usage_page") == USAGE_PAGE_ANALOG]
    if path:
        for item in analog:
            if str(item.get("path")) == path:
                return item
    return analog[0] if analog else None


def stage_command(songs, out, calibration, osr_path, capture_path, slug):
    """The studio's render callback: one replay to payload.json plus its assets."""
    if songs is None:
        raise RuntimeError("set the osu! folder in the studio settings, then restart")
    spec = ReplaySpec(
        replay=osr_path,
        songs=songs,
        capture=capture_path,
        calibration_data=calibration,
    )
    return stage_replay(spec, Path(out) / slug)


class StudioServer(ThreadingHTTPServer):
    # Windows' SO_REUSEADDR lets a second bind silently share the port with a server
    # that is still running (a leftover live_depth, say), and the browser then hits
    # whichever one wins the race. Refuse to share so the conflict is visible.
    allow_reuse_address = False


def main():
    parser = argparse.ArgumentParser(description="O3C capture studio: record, watch, align")
    parser.add_argument("--settings", type=Path, default=ROOT / "output" / "settings.json",
                        help="where the web-UI settings live")
    parser.add_argument("--osu", type=Path, help="override the osu! folder for this run")
    parser.add_argument("--port", type=int, help="override the saved port for this run")
    parser.add_argument("--rate-hz", type=float, default=60.0, help="how often the live page updates")
    parser.add_argument("--captures", type=Path, default=ROOT / "output" / "captures")
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "replays")
    parser.add_argument("--skin", type=Path, help="override the saved skin folder for this run")
    parser.add_argument("--skin-url", help="skin URL to use as-is instead of staging --skin")
    parser.add_argument("--tosu-url", help="override the saved tosu URL for this run")
    parser.add_argument("--state-poll-ms", type=float, default=50.0)
    parser.add_argument("--window-min", type=float,
                        help="override the saved capture window in minutes (0 keeps everything)")
    parser.add_argument("--no-watch", action="store_true",
                        help="do not auto-align new .osr files (the page can still align one)")
    parser.add_argument("--no-tosu", action="store_true",
                        help="do not poll tosu; align from the press sequence alone")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    settings = settings_module.load(args.settings)
    if args.osu is not None:
        settings["osu_root"] = str(args.osu)
    calibration = settings_module.calibration_data(settings)
    config = build_config(calibration)
    port = args.port if args.port is not None else settings["port"]
    tosu_url = args.tosu_url or settings["tosu_url"]
    window_min = args.window_min if args.window_min is not None else settings["window_min"]

    # Imported here so the module can be imported (and tested) off Windows, where
    # tap_capture's ctypes user32 binding would fail at import time.
    from tap_capture import KeyboardHook, analog_loop

    # The custom skin lives once under the served root and is referenced by the shared
    # shell, so it is not copied into every replay folder.
    skin_url = args.skin_url
    skin_source = args.skin or (Path(settings["skin"]) if settings["skin"] else None)
    if skin_url is None and skin_source is not None:
        staged = ROOT / "output" / "skin"
        count = stage_skin(skin_source, staged)
        skin_url = "/output/skin"
        print(f"skin   : {skin_source} -> {staged} ({count} files)")

    # The viewer is one shared shell over the per-replay payload.json files. Rewrite it
    # on every start so a template change reaches every replay without re-staging them.
    replays_dir = Path(args.out).resolve()
    try:
        base = "/" + replays_dir.relative_to(ROOT).as_posix() + "/"
    except ValueError:
        base = replays_dir.as_posix().rstrip("/") + "/"
    shell_path = ROOT / "output" / "replay.html"
    shell_path.parent.mkdir(parents=True, exist_ok=True)
    shell_path.write_text(render_page({"base": base, "skin": skin_url}), encoding="utf-8")

    songs_dir = settings_module.subdir(settings, "Songs")
    watch_dir = settings_module.subdir(settings, "Replays") or (ROOT / "output" / "no-replays")

    target = select_device(settings["device_path"])
    recorder = StudioRecorder()
    studio = Studio(recorder, args.captures,
                    lambda osr, capture, slug: stage_command(
                        songs_dir, args.out, calibration, osr, capture, slug),
                    identify(target) if target else {}, tosu_url, args.state_poll_ms,
                    default_window_s=window_seconds(window_min) or 0.0,
                    store=ReplayStore(replays_dir / "replays.json"),
                    settings=settings, settings_path=args.settings)

    stop = threading.Event()
    threads = []
    device_lock = None

    if target is None:
        recorder.error = "no O3C analog interface found"
    else:
        try:
            device_lock = DeviceLock()
        except DeviceBusy as error:
            raise SystemExit(f"{error}; stop the other live_depth/studio first")
        import hid
        try:
            handle = hid.device()
            handle.open_path(target["path"])
            handle.set_nonblocking(False)
            requests = [(index, build_command_report(0x14, index, kind=0x04))
                        for index in range(LEVEL_COUNT)]
            analog = threading.Thread(
                target=analog_loop,
                args=(handle, recorder, stop, studio.started, requests),
                daemon=True,
            )
            threads.append(analog)
        except OSError as error:
            recorder.error = f"cannot open the device: {error}"

    hook = KeyboardHook(recorder, stop, studio.started)
    threads.append(hook)

    def emit_state(state):
        if not recorder.is_armed():
            return
        host_ns = int(state["host_ms"] * 1e6) - studio.started
        runs.update(state, host_ns)
        record = {"type": "state", "host_ns": host_ns}
        for key in ("map_time", "state", "state_name", "playing", "focused", "paused",
                    "checksum", "beatmap_file", "artist", "title", "version"):
            record[key] = state[key]
        recorder.write(record)

    if not args.no_tosu:
        runs = PlayRuns(recorder, args.state_poll_ms)
        poller = StatePoller(emit_state, url=tosu_url, poll_ms=args.state_poll_ms, stop=stop)
        studio.poller = poller
        threads.append(poller)
    else:
        print("tosu       : off (alignment uses the press sequence only)")

    for thread in threads:
        thread.start()
    hook.installed.wait(timeout=2.0)

    studio.watch_dir = str(watch_dir)
    watcher = None
    if args.no_watch:
        studio.watch_status = "watcher off (align a replay from the page)"
    else:
        watcher = ReplayWatcher(watch_dir)
        watcher.prime()
    studio.watch_dir = str(watch_dir)

    StudioHandler.studio = studio
    StudioHandler.rate_hz = args.rate_hz
    StudioHandler.page = (TEMPLATE.read_text(encoding="utf-8")
                          .replace("__CONFIG__", json.dumps(config)).encode("utf-8"))

    try:
        server = StudioServer(("127.0.0.1", port), StudioHandler)
    except OSError as error:
        raise SystemExit(
            f"cannot bind 127.0.0.1:{port} ({error}); another live_depth/studio is "
            "probably still running - stop it or pass --port"
        )
    url = f"http://127.0.0.1:{port}/"
    print(f"studio     : {url}")
    print(f"osu        : {settings['osu_root'] or '(not set - edit it in the web UI)'}")
    if args.no_watch:
        print(f"replays    : {watch_dir} (watcher off - align from the page)")
    else:
        print(f"replays    : watching {watch_dir}")
    print(f"captures   : {args.captures}")
    if skin_url:
        print(f"skin       : {skin_url}")
    if not args.no_open:
        webbrowser.open(url)

    def watch_loop():
        while not stop.is_set():
            for path in watcher.scan_once():
                studio.handle_replay(path)
            stop.wait(1.0)

    if watcher is not None:
        watcher_thread = threading.Thread(target=watch_loop, daemon=True)
        watcher_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        studio.disarm()
        hook.shutdown()
        if device_lock is not None:
            device_lock.close()
        server.shutdown()


if __name__ == "__main__":
    main()

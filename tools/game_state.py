"""Read the live osu! game state from a community state reader (tosu).

A replay says what was played but not when. Aligning the device capture to it needs a
second clock that knows both the host clock and the map clock, and tosu - the
maintained successor of gosumemory, and the source most osu OBS overlays read from -
exposes exactly that over HTTP:

    GET http://127.0.0.1:24050/json/v2   ->   beatmap.time.live  (current map time)

The map time comes from tosu's *precise* data loop, not the slow one: tosu polls
``global.playTime`` every ``PRECISE_DATA_POLL_RATE`` (10 ms by default, 1 ms minimum)
and serves it as ``beatmap.time.live``, while the slower ``POLL_RATE`` (150 ms) drives
most other fields. A sample is therefore stale by up to one precise poll, and
``host - map`` is biased high by that amount. The lower edge of the distribution is
the offset; the spread is the staleness we actually saw. That is tight enough to
decide *which* press-sequence match to trust, the failure mode that matters on a
repetitive map; tools/osu_align.py then refines the offset to sub-millisecond
precision around it.

``--poll-ms`` here and ``--state-poll-ms`` in the capture only bound how often *we*
sample; they do not change how fresh tosu's value is.

The parser accepts the v2 schema and the older v1/gosumemory one
(``gameplay.time.live`` / ``menu.state``) so a different reader still works.
"""

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:24050"
V2_PATH = "/json/v2"
V1_PATH = "/json"

# GameState.play in tosu/gosumemory. The name is only a fallback for readers that
# do not report a number.
PLAYING_STATE = 2
PLAYING_NAMES = ("play", "playing")

# urllib honours the system proxy, which on Windows would try to send a request for
# 127.0.0.1 through it. Localhost must never be proxied here.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def extract_state(payload):
    """Normalise a state-reader answer into the fields the alignment needs."""
    if not isinstance(payload, dict):
        return None
    beatmap = _mapping(payload.get("beatmap"))
    gameplay = _mapping(payload.get("gameplay"))
    game = _mapping(payload.get("game"))
    menu = _mapping(payload.get("menu"))

    time_node = _mapping(beatmap.get("time")) or _mapping(gameplay.get("time"))
    raw_time = time_node.get("live")
    map_time = float(raw_time) if isinstance(raw_time, (int, float)) else None

    state_node = payload.get("state") if isinstance(payload.get("state"), dict) else menu.get("state")
    if isinstance(state_node, dict):
        number, name = state_node.get("number"), state_node.get("name") or ""
    else:
        number, name = state_node, ""
    number = int(number) if isinstance(number, (int, float)) else None
    if number is not None:
        playing = number == PLAYING_STATE
    else:
        playing = str(name).strip().lower() in PLAYING_NAMES

    return {
        "map_time": map_time,
        "state": number,
        "state_name": str(name),
        "playing": playing,
        "focused": bool(game.get("focused", True)),
        "paused": bool(game.get("paused", False)),
        "checksum": str(beatmap.get("checksum") or "").lower(),
        "beatmap_file": str(_mapping(payload.get("directPath")).get("beatmapFile") or ""),
        "artist": str(beatmap.get("artistUnicode") or beatmap.get("artist") or ""),
        "title": str(beatmap.get("titleUnicode") or beatmap.get("title") or ""),
        "version": str(beatmap.get("version") or ""),
    }


def fetch_state(url=DEFAULT_URL, timeout=1.0, path=V2_PATH, opener=OPENER):
    """One HTTP read of the state reader. Raises on transport errors."""
    request = urllib.request.Request(
        url.rstrip("/") + path, headers={"Accept": "application/json", "Connection": "keep-alive"}
    )
    with opener.open(request, timeout=timeout) as answer:
        return json.loads(answer.read().decode("utf-8", "replace"))


def monotone_runs(samples):
    """Split (host_ms, map_ms) samples where the map clock jumps backwards."""
    runs = []
    current = []
    previous = None
    for host_ms, map_ms in samples:
        if previous is not None and map_ms < previous:
            runs.append(current)
            current = []
        current.append((host_ms, map_ms))
        previous = map_ms
    if current:
        runs.append(current)
    return runs


def play_runs(samples, min_samples=8):
    """Contiguous playing runs, oldest first; each map play restarts its clock."""
    ordered = sorted((float(host), float(value)) for host, value in samples)
    return [run for run in monotone_runs(ordered) if len(run) >= min_samples]


def last_play_run(samples, min_samples=8):
    """The most recent play in a capture that spans several maps.

    A long session records many plays; an exported replay is almost always the one
    that just ended, so alignment has to pick the tail run rather than mix every play
    of the same map into one window (which is what a plain checksum filter does).
    """
    runs = play_runs(samples, min_samples=min_samples)
    return runs[-1] if runs else []


def _rate(run):
    """Least-squares d(map)/d(host) over a run; 1.0 when the clocks tick together."""
    count = len(run)
    if count < 8:
        return 1.0
    mean_host = sum(host for host, _ in run) / count
    mean_map = sum(value for _, value in run) / count
    numerator = sum((host - mean_host) * (value - mean_map) for host, value in run)
    denominator = sum((host - mean_host) ** 2 for host, _ in run)
    return numerator / denominator if denominator else 1.0


def coarse_offset(samples, percentile=0.05, min_samples=8):
    """Coarse host -> map offset from (host_ms, map_ms) pairs.

    ``offset_ms`` uses the same sign as tools/osu_align.py: ``map = host + offset``.

    Only the first observation of each distinct map value is used, because that is
    the sample closest to the reader's own read instant; repeats only widen the
    staleness window. The estimate is a low percentile rather than the minimum so a
    single scheduling hiccup cannot drag the anchor down.
    """
    ordered = sorted((float(host), float(value)) for host, value in samples)
    runs = [run for run in monotone_runs(ordered) if len(run) >= min_samples]
    if not runs:
        return None
    run = max(runs, key=len)

    first = []
    previous = None
    for host_ms, map_ms in run:
        if map_ms != previous:
            first.append((host_ms, map_ms))
            previous = map_ms
    if len(first) < 4:
        first = run

    lags = sorted(host - value for host, value in first)
    index = max(0, min(len(lags) - 1, int(len(lags) * percentile)))
    return {
        "offset_ms": -lags[index],
        "best_lag_ms": lags[0],
        "stale_ms": lags[-1] - lags[0],
        "pairs": len(first),
        "samples": len(samples),
        "run": len(run),
        "rate": _rate(run),
        "map_from_ms": run[0][1],
        "map_to_ms": run[-1][1],
    }


def load_state_samples(path, checksum=None):
    """(host_ms, map_ms) pairs from a capture, keeping only samples taken in play.

    When ``checksum`` is given only samples from that beatmap are kept, so a capture
    that spans several plays cannot average their clocks into one wrong anchor.
    Without it the first checksum seen is reported, which is fine for a single map.
    """
    wanted = str(checksum).lower() if checksum else None
    samples = []
    seen = ""
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("type") != "state":
                continue
            value = str(record.get("checksum") or "").lower()
            if wanted is None:
                seen = seen or value
            elif value != wanted:
                continue
            if not record.get("playing") or record.get("map_time") is None:
                continue
            samples.append((record["host_ns"] / 1e6, float(record["map_time"])))
    return samples, (wanted or seen)


class StatePoller(threading.Thread):
    """Poll a state reader and hand each sample to a callback with a host stamp.

    The timestamp is taken before the request is sent: the value was read by the
    reader at some instant before it answers, so stamping early keeps the estimate on
    the low edge of the staleness window instead of smearing it by the round trip.
    """

    def __init__(self, emit, url=DEFAULT_URL, poll_ms=50.0, stop=None, timeout=1.0):
        super().__init__(daemon=True)
        self.emit = emit
        self.url = url
        self.poll_ms = max(1.0, float(poll_ms))
        self.timeout = timeout
        self.stop_event = stop or threading.Event()
        self.path = V2_PATH
        self.samples = 0
        self.errors = 0
        self.last = None
        self.ready = threading.Event()

    def read_once(self):
        started_ms = time.perf_counter_ns() / 1e6
        try:
            payload = fetch_state(self.url, self.timeout, self.path)
        except urllib.error.HTTPError as error:
            if self.path == V2_PATH and error.code in (404, 400):
                self.path = V1_PATH
            self.errors += 1
            return None
        except (urllib.error.URLError, OSError, ValueError):
            self.errors += 1
            return None
        state = extract_state(payload)
        if state is None:
            self.errors += 1
            return None
        state["host_ms"] = started_ms
        state["schema"] = "v2" if self.path == V2_PATH else "v1"
        return state

    def run(self):
        while not self.stop_event.is_set():
            state = self.read_once()
            if state is not None:
                self.samples += 1
                self.last = state
                self.ready.set()
                self.emit(state)
            self.stop_event.wait(self.poll_ms / 1000.0)


def probe(url=DEFAULT_URL, timeout=2.0):
    """One read for setup checks; reports what the reader actually answered."""
    result = {"url": url, "ok": False}
    for path, schema in ((V2_PATH, "v2"), (V1_PATH, "v1")):
        try:
            payload = fetch_state(url, timeout, path)
        except urllib.error.HTTPError as error:
            result.setdefault("errors", []).append(f"{path}: HTTP {error.code}")
            continue
        except (urllib.error.URLError, OSError, ValueError) as error:
            result.setdefault("errors", []).append(f"{path}: {error}")
            continue
        state = extract_state(payload)
        if state is None:
            result.setdefault("errors", []).append(f"{path}: not a JSON object")
            continue
        result.update({"ok": True, "schema": schema, "path": path, **state})
        return result
    return result


def main():
    parser = argparse.ArgumentParser(description="Read the live osu! state from tosu")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--probe", action="store_true", help="print one answer and exit")
    parser.add_argument("--out", type=Path, help="record into this NDJSON file")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--poll-ms", type=float, default=50.0)
    args = parser.parse_args()

    if args.probe or args.out is None:
        answer = probe(args.url)
        print(json.dumps(answer, indent=2, ensure_ascii=False))
        raise SystemExit(0 if answer["ok"] else 1)

    stop = threading.Event()
    pairs = []
    started = time.perf_counter_ns()
    with args.out.open("w", encoding="utf-8", newline="") as target:
        target.write(json.dumps({
            "type": "metadata",
            "schema_version": 1,
            "source": "game_state",
            "url": args.url,
            "poll_ms": args.poll_ms,
            "timestamp_source": "native_perf_counter_ns",
        }, ensure_ascii=False) + "\n")

        def emit(state):
            target.write(json.dumps({"type": "state", "host_ns": int(state["host_ms"] * 1e6), **state},
                                    ensure_ascii=False) + "\n")
            target.flush()
            if state["playing"] and state["map_time"] is not None:
                pairs.append((state["host_ms"], state["map_time"]))
            print(f"{state['host_ms'] / 1000:9.3f}s  state={state['state_name'] or state['state']} "
                  f"map={state['map_time']} playing={state['playing']}", file=sys.stderr)

        poller = StatePoller(emit, url=args.url, poll_ms=args.poll_ms, stop=stop)
        poller.start()
        try:
            while True:
                if args.duration and (time.perf_counter_ns() - started) / 1e9 >= args.duration:
                    break
                if not poller.is_alive():
                    break
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        stop.set()
        poller.join(timeout=2.0)

    print(json.dumps({"samples": poller.samples, "errors": poller.errors,
                      "playing_samples": len(pairs)}, ensure_ascii=False))
    summary = coarse_offset(pairs)
    if summary:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print("no playing samples: start the map, or check --url", file=sys.stderr)


if __name__ == "__main__":
    main()

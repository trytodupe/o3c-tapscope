"""Stage a replay, its beatmap and an optional device capture for the replay viewer.

Three lanes share a single time axis so note timing and key depth can be compared
directly:

    bar 1  note starts against the presses the replay recorded
    bar 2  the first switch (Z) - depth in millimetres when a capture is attached
    bar 3  the second switch (X)

A replay alone carries no depth, so without ``--capture`` the two lower lanes fall
back to the key state the replay recorded and say so on the page. Depth comes from
``tools/tap_capture.py``; its clock is aligned to the replay by matching press
sequences, the same way ``tools/osu_align.py`` does it.

``stage_replay`` writes the payload and the assets into one folder; the studio serves
a single shared shell (``render_page``) over all of them, so no per-replay HTML is
ever generated.
"""

import argparse
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from analyze import replay_frames
from beatmap import (
    filename_terms,
    find_beatmap,
    find_beatmap_in_store,
    hit_windows,
    pair_presses,
    parse_beatmap,
)
from calibration import STEP_UM, level_lut, load_calibration
from game_state import coarse_offset, last_play_run, load_state_samples
from osu_align import align_with_anchor

TEMPLATE = Path(__file__).with_name("replay_view_template.html")

# osu!stable sets the mouse and the keyboard bit for the same physical press, and the
# mouse bit alone when the player clicks, so a lane is the union of both.
STREAM_BITS = {"K1": 0x01 | 0x04, "K2": 0x02 | 0x08}
STREAM_KEYS = {"K1": "Z", "K2": "X"}
KEY_ORDER = ("K1", "K2")

MOD_BITS = (
    (1, "NF"), (2, "EZ"), (8, "HD"), (16, "HR"), (32, "SD"), (64, "DT"), (256, "HT"),
    (512, "NC"), (1024, "FL"), (2048, "AT"), (4096, "SO"), (8192, "AP"), (16384, "PF"),
    (1 << 20, "FI"), (1 << 21, "RD"), (1 << 29, "V2"), (1 << 30, "MR"),
)


def mod_names(mods):
    return "".join(name for bit, name in MOD_BITS if mods & bit) or "NM"


def stream_edges(frames):
    """Press and release instants per lane, plus the frame interval actually used."""
    edges = {name: [] for name in KEY_ORDER}
    held = {name: False for name in KEY_ORDER}
    for time_ms, _, _, keys in frames:
        for name in KEY_ORDER:
            pressed = bool(keys & STREAM_BITS[name])
            if pressed != held[name]:
                edges[name].append([float(time_ms), pressed])
                held[name] = pressed
    intervals = [b[0] - a[0] for a, b in zip(frames, frames[1:]) if b[0] > a[0]]
    intervals.sort()
    interval = intervals[len(intervals) // 2] if intervals else 0.0
    return edges, interval


def parse_mm_list(text, count):
    """Per-lane millimetre values from "1.8,1.8"; the last value repeats for extras."""
    if text is None:
        return [None] * count
    values = [float(part) for part in str(text).replace(";", ",").split(",") if part.strip()]
    if not values:
        return [None] * count
    return [values[min(index, len(values) - 1)] for index in range(count)]


def _rt_pair(value):
    """An RT bound pair as ``[low, high]``; missing or short input stays ``None``."""
    if value is None:
        return [None, None]
    values = list(value)
    return [values[0] if len(values) > 0 else None,
            values[1] if len(values) > 1 else None]


def build_calibration_payload(calibration, fallback_rt=None):
    keys = []
    for key in calibration["keys"]:
        lut = level_lut(calibration.get("step_um", STEP_UM))
        # RT bounds are per key: each switch has its own rest and actuation depths.
        rt_range = _rt_pair(key["rt_range_mm"]) if key.get("rt_range_mm") is not None else _rt_pair(fallback_rt)
        keys.append({
            "index": key["index"],
            "name": key.get("name") or STREAM_KEYS.get(KEY_ORDER[key["index"] % 2], "?"),
            "lut_mm": lut,
            "travel_mm": round(max(lut), 3),
            "rt_range_mm": rt_range,
        })
    return {
        "source": calibration.get("source", "file"),
        "captured_at": calibration.get("captured_at", ""),
        "step_um": calibration.get("step_um", STEP_UM),
        "keys": keys,
    }


def decimate_levels(samples, max_points):
    """Keep bucket edges and each channel's extremes so short spikes survive."""
    if max_points <= 0 or len(samples) <= max_points:
        return samples
    buckets = max(1, max_points // 8)
    size = len(samples) / buckets
    kept = []
    for index in range(buckets):
        start = int(index * size)
        stop = max(start + 1, int((index + 1) * size))
        bucket = samples[start:stop]
        chosen = {start, stop - 1}
        for channel in range(1, len(bucket[0])):
            chosen.add(start + max(range(len(bucket)), key=lambda offset: bucket[offset][channel]))
            chosen.add(start + min(range(len(bucket)), key=lambda offset: bucket[offset][channel]))
        kept.extend(samples[offset] for offset in sorted(chosen))
    return kept


def capture_levels(path, window=None):
    """Read a tap capture: host press times and the analog samples.

    ``window`` is an optional ``(host_lo_ms, host_hi_ms)`` pair; a capture that spans
    several plays is cut to the one that matches the replay so menu time and other
    plays do not leak into the timeline.
    """
    lo_ms, hi_ms = window if window else (None, None)
    presses = []
    samples = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            host_ms = record.get("host_ns", 0) / 1e6
            if lo_ms is not None and not (lo_ms <= host_ms <= hi_ms):
                continue
            kind = record.get("type")
            if kind == "keyboard":
                if record.get("down") and not record.get("injected"):
                    presses.append(host_ms)
            elif kind == "levels":
                samples.append([host_ms] + [int(v) for v in record["levels"]])
    presses.sort()
    samples.sort(key=lambda sample: sample[0])
    return presses, samples


def replay_state_samples(edges, duration_ms):
    """Square waves for the fallback lanes: the key state the replay recorded."""
    times = sorted({time for edge_list in edges.values() for time, _ in edge_list})
    state = {name: False for name in KEY_ORDER}
    index = {name: 0 for name in KEY_ORDER}
    samples = []
    for time in times + [float(duration_ms)]:
        for name in KEY_ORDER:
            while index[name] < len(edges[name]) and edges[name][index[name]][0] <= time:
                state[name] = edges[name][index[name]][1]
                index[name] += 1
        samples.append([time, 1.0 if state["K1"] else 0.0, 1.0 if state["K2"] else 0.0])
    return samples


def played_until(frames):
    """Time the replay's own frames end.

    A cleared play keeps recording past the last note (the cursor keeps moving), while
    an attempt exported after a fail stops at the moment of failure. So this is the
    boundary between "could still have been hit" and "was never played".
    """
    return float(frames[-1][0]) if frames else 0.0


def split_missed(missed, played_until_ms, window_ms):
    """Separate real misses from notes the replay never reached.

    No press can exist after the last frame, so every note further than one judgement
    window past it is unplayed rather than missed. Counting those as misses would make
    an abandoned attempt look like a catastrophically bad one and hide the fail point.
    """
    tail = played_until_ms + window_ms
    return (
        [note for note in missed if note["time"] <= tail],
        [note for note in missed if note["time"] > tail],
    )


@dataclass
class ReplaySpec:
    """Everything ``build_payload`` reads, so the CLI and the studio share one path."""

    replay: Path
    songs: Path | None = None
    lazer_files: Path | None = None
    beatmap: Path | None = None
    capture: Path | None = None
    tolerance_ms: float = 6.0
    state_window_ms: float = 250.0
    no_state_window: bool = False
    max_points: int = 240000
    keys: str = "Z,X"
    rt_range_mm: str | None = None
    calibration_data: dict = field(default_factory=dict)


def build_payload(spec):
    metadata, frames = replay_frames(spec.replay)
    edges, frame_interval = stream_edges(frames)
    streams = {name: [time for time, down in edges[name] if down] for name in KEY_ORDER}

    beatmap_path = spec.beatmap
    if beatmap_path is None:
        # osu!stable keeps named folders under Songs; osu!lazer keeps every file in a
        # flat content-addressed store. Try the matching one, then the other, so a
        # replay exported by either client resolves when the map exists in both.
        if spec.songs is not None and (Path(spec.songs).is_dir() or spec.lazer_files is None):
            beatmap_path = find_beatmap(
                metadata["beatmap_hash"], spec.songs, filename_terms(spec.replay.name)
            )
        if beatmap_path is None and spec.lazer_files is not None:
            beatmap_path = find_beatmap_in_store(metadata["beatmap_hash"], spec.lazer_files)
        if beatmap_path is None:
            raise SystemExit(
                f"No beatmap matched hash {metadata['beatmap_hash']} under "
                f"{spec.songs or spec.lazer_files}; pass --beatmap"
            )
    beatmap = parse_beatmap(beatmap_path)
    windows = hit_windows(beatmap["od"])
    pairs, stray, missed = pair_presses(beatmap["notes"], streams, windows)
    played_until_ms = played_until(frames)
    missed, unplayed = split_missed(missed, played_until_ms, windows["50"])

    duration_ms = max([float(beatmap["length_ms"])] + [time for time, _, _, _ in frames[-1:]])
    levels = {"mode": "replay", "alignment": None, "samples": replay_state_samples(edges, duration_ms)}
    if spec.capture is not None:
        # Keep only the play whose beatmap hash matches this replay; a capture can
        # span several plays and mixing them poisons both the anchor and the match.
        # A session can hold many plays. Keep the last one that carries this replay's
        # checksum; when no sample matches (a reader that reports no checksum, or a
        # hash the reader spells differently) fall back to the last play at all.
        state_pairs, _ = load_state_samples(spec.capture, metadata["beatmap_hash"])
        if not state_pairs:
            state_pairs, _ = load_state_samples(spec.capture)
        state_pairs = last_play_run(state_pairs)
        window = None
        if state_pairs:
            hosts = [time for time, _ in state_pairs]
            window = (min(hosts), max(hosts))
        host_presses, samples = capture_levels(spec.capture, window)
        merged_host = sorted(host_presses)
        merged_replay = sorted(time for times in streams.values() for time in times)

        # The capture may carry a coarse clock from a live state reader. It is only
        # a hint - it decides which press-sequence match to trust - so a missing
        # anchor just falls back to the unrestricted search.
        anchor = coarse_offset(state_pairs) if (state_pairs and not spec.no_state_window) else None

        alignment, source = align_with_anchor(
            merged_host,
            merged_replay,
            spec.tolerance_ms,
            None if anchor is None else anchor["offset_ms"],
            spec.state_window_ms,
        )
        if alignment is None:
            raise SystemExit("Capture and replay do not share any press sequence; check --capture")
        if anchor is not None:
            alignment["anchor"] = anchor
            alignment["anchor_used"] = source == "anchor"
        offset = alignment["offset_ms"]
        shifted = [[sample[0] + offset] + sample[1:] for sample in samples]
        levels = {
            "mode": "analog",
            "alignment": alignment,
            "samples": decimate_levels(shifted, spec.max_points),
            "sample_count": len(samples),
        }
        duration_ms = max(duration_ms, shifted[-1][0] if shifted else 0.0)

    keys = [part.strip() for part in spec.keys.split(",") if part.strip()]
    # The RT bounds are per key and absolute from the key top (0 = rest), so a capture,
    # a replay and the device config share one axis. ``--rt-range-mm`` is a broadcast
    # convenience; per-key settings/calibration win over it.
    given_range = parse_mm_list(spec.rt_range_mm, 2)
    fallback_rt = given_range if any(value is not None for value in given_range) \
        else spec.calibration_data.get("rt_range_mm")
    calibration_payload = build_calibration_payload(spec.calibration_data, fallback_rt)
    return {
        "meta": {
            "player": metadata["player"],
            "mods": mod_names(metadata["mods"]),
            "mode": metadata["mode"],
            "beatmap": {
                "artist": beatmap["artist"],
                "title": beatmap["title"],
                "version": beatmap["version"],
                "creator": beatmap["creator"],
                "od": beatmap["od"],
                "ar": beatmap["ar"],
                "bpm": beatmap["bpm"],
                "path": beatmap["path"],
                "hash": metadata["beatmap_hash"],
            },
            "note_count": len(beatmap["notes"]),
            "duration_ms": duration_ms,
            "frame_interval_ms": round(frame_interval, 2),
            "keys": keys,
            "streams": {name: STREAM_KEYS[name] for name in KEY_ORDER},
        },
        "windows": windows,
        "notes": [[note["time"], note["kind"], note["x"], note["y"]] for note in beatmap["notes"]],
        "presses": streams,
        "pairs": pairs,
        "stray": stray,
        "missed": [{"time": note["time"]} for note in missed],
        # An incomplete attempt keeps the full beatmap on the time axis so the gap is
        # visible, and reports the notes it never reached separately.
        "played_until_ms": played_until_ms,
        "complete": not unplayed,
        "unplayed": [{"time": note["time"]} for note in unplayed],
        "levels": levels,
        "calibration": calibration_payload,
    }


def stage_skin(source, target):
    """Copy an osu! skin folder next to the page and index it for loadSkinFromDir.

    The renderer fetches ``<baseUrl>/index.json`` and then every listed file, so the
    skin has to live under the served tree. A skin that points its fonts at a subfolder
    (``HitCirclePrefix: numbers/default``) only resolves when those relative paths are
    copied too, which is why this walks the folder instead of taking the top level only.
    """
    target.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name.lower() in {"desktop.ini", "thumbs.db"}:
            continue
        relative = path.relative_to(source).as_posix()
        if relative.lower() == "index.json":
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        files.append(relative)
    (target / "index.json").write_text(json.dumps({"files": files}, indent=2), encoding="utf-8")
    return len(files)


def copy_song(beatmap_path, out_dir):
    """Copy the beatmap's ``AudioFilename`` next to the replay; None when absent."""
    osu_text = (out_dir / "beatmap.osu").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^AudioFilename\s*:\s*(.+?)\s*$", osu_text, re.MULTILINE)
    if match is None:
        return None
    source = beatmap_path.parent / match.group(1).strip()
    if not source.is_file():
        return None
    target = out_dir / ("song" + source.suffix.lower())
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return target.name


def stage_replay(spec, out_dir):
    """Build the payload and write it with the assets the viewer fetches.

    No HTML is produced: the studio serves one shared shell and points it at
    ``payload.json``, so a staged replay is only data plus the files the playfield
    loads (the .osr, the .osu and the song). Returns the list entry for the studio.
    """
    payload = build_payload(spec)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    osr_target = out_dir / "replay.osr"
    if spec.replay.resolve() != osr_target.resolve():
        shutil.copyfile(spec.replay, osr_target)
    beatmap_path = Path(payload["meta"]["beatmap"]["path"])
    beatmap_target = out_dir / "beatmap.osu"
    if beatmap_path.resolve() != beatmap_target.resolve():
        shutil.copyfile(beatmap_path, beatmap_target)
    payload["assets"] = {
        "osr": "replay.osr",
        "beatmap": "beatmap.osu",
        "song": copy_song(beatmap_path, out_dir),
    }
    (out_dir / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    meta = payload["meta"]
    return {
        "id": out_dir.name,
        "name": spec.replay.name,
        "player": meta["player"],
        "mods": meta["mods"],
        "artist": meta["beatmap"]["artist"],
        "title": meta["beatmap"]["title"],
        "version": meta["beatmap"]["version"],
        "note_count": meta["note_count"],
        "duration_ms": meta["duration_ms"],
        "has_depth": payload["levels"]["mode"] == "analog",
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }


def render_page(config):
    """The one shared viewer shell: the template with only ``__CONFIG__`` filled in."""
    return TEMPLATE.read_text(encoding="utf-8").replace("__CONFIG__", json.dumps(config))


def main():
    parser = argparse.ArgumentParser(
        description="Stage one replay as payload.json plus the assets the viewer fetches"
    )
    parser.add_argument("replay", type=Path)
    parser.add_argument("--beatmap", type=Path, help="the .osu file; found by hash when omitted")
    parser.add_argument("--songs", type=Path, default=Path(r"D:\osu!\Songs"))
    parser.add_argument("--lazer", type=Path,
                        help="osu!lazer folder, or its 'files' store; searched when --songs misses")
    parser.add_argument("--capture", type=Path, help="tap capture with analog levels for this play")
    parser.add_argument("--calibration", type=Path, help="calibration JSON; read from the device when omitted")
    parser.add_argument("--out", type=Path, required=True,
                        help="folder that receives payload.json, replay.osr, beatmap.osu and song.*")
    parser.add_argument("--tolerance-ms", type=float, default=6.0)
    parser.add_argument(
        "--state-window-ms",
        type=float,
        default=250.0,
        help="how far around the state reader anchor the press search may look",
    )
    parser.add_argument("--no-state-window", action="store_true", help="ignore the state reader anchor")
    parser.add_argument("--max-points", type=int, default=240000)
    parser.add_argument("--keys", default="Z,X", help="host key names for lane 2 and 3")
    parser.add_argument("--rt-range-mm", help="RT-range bounds in mm from the key top, e.g. 1.0,3.6")
    args = parser.parse_args()

    # The position scale is fixed at 50 um per raw count, so the calibration file is
    # only needed for the key names and the RT-range bounds. Reading the device table
    # is unnecessary, and would only risk colliding with a live poller.
    cache = Path(__file__).with_name("calibration.example.json")
    try:
        calibration_data = load_calibration(args.calibration or cache)
    except (OSError, ValueError, KeyError):
        calibration_data = {
            "source": "default",
            "step_um": STEP_UM,
            "keys": [{"index": index, "name": name, "table": []}
                     for index, name in enumerate(("Z", "X", "C"))],
        }

    lazer_files = None
    if args.lazer is not None:
        # Accept either the lazer data folder or the content-addressed store itself.
        nested = args.lazer / "files"
        lazer_files = nested if nested.is_dir() else args.lazer

    spec = ReplaySpec(
        replay=args.replay,
        songs=args.songs,
        lazer_files=lazer_files,
        beatmap=args.beatmap,
        capture=args.capture,
        tolerance_ms=args.tolerance_ms,
        state_window_ms=args.state_window_ms,
        no_state_window=args.no_state_window,
        max_points=args.max_points,
        keys=args.keys,
        rt_range_mm=args.rt_range_mm,
        calibration_data=calibration_data,
    )
    entry = stage_replay(spec, args.out)
    print(f"replay : {entry['player']} - {entry['artist']} - {entry['title']} "
          f"[{entry['version']}] {entry['mods']}")
    print(f"notes  : {entry['note_count']}")
    print(f"depth  : {'analog capture' if entry['has_depth'] else 'key state only'}")
    print(f"staged : {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()

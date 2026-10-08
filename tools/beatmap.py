"""Locate and parse the osu! beatmap a replay was played on.

A replay stores only the MD5 of the ``.osu`` file, so recovering the map means hashing
local candidates. The replay file name is the only hint about the folder, so name
matching narrows the scan before anything is hashed.

Only the fields the timeline viewer needs are parsed: metadata, timing, difficulty
(for the hit windows) and the hit object start times.
"""

import hashlib
import re
from bisect import bisect_left
from pathlib import Path

MODE_NAMES = {0: "osu!standard", 1: "osu!taiko", 2: "osu!catch", 3: "osu!mania"}

HIT_CIRCLE = 1
HIT_SLIDER = 2
HIT_SPINNER = 8

_TRAILING_DATE = re.compile(r"\s*\(\d{4}-\d{2}-\d{2}\)[^()]*$")
_BRACKETS = re.compile(r"\[[^\]]*\]")


# osu!lazer keeps every imported file in a content-addressed store: the path is the
# SHA-256 of the bytes and there is no name, so a beatmap is only recognisable by its
# content. A beatmap's .osu starts with this header; the file store has no extension
# to filter on, which is why the scan sniffs the bytes first and hashes only the hits.
OSU_HEADER = b"osu file format"
_BOM = b"\xef\xbb\xbf"

# The MD5 -> path index of a lazer store is rebuilt lazily and kept for the life of the
# process: staging several replays against one library should not rescan every time.
_STORE_INDEX = {}


def file_hash(path):
    """MD5 of the raw file bytes; this is the value a replay stores."""
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def _is_beatmap_blob(path):
    try:
        with path.open("rb") as handle:
            head = handle.read(len(OSU_HEADER) + len(_BOM))
    except OSError:
        return False
    return head.lstrip(_BOM).startswith(OSU_HEADER)


def _index_store(files_root):
    """MD5 -> path for every .osu blob under a lazer file store."""
    index = {}
    for path in files_root.rglob("*"):
        if path.is_file() and _is_beatmap_blob(path):
            index[file_hash(path)] = path
    return index


def find_beatmap_in_store(beatmap_hash, files_root):
    """Return the lazer ``.osu`` blob whose MD5 matches ``beatmap_hash``, or None.

    Lazer addresses files by SHA-256 and never stores the original name, so the only
    way back to the beatmap is to sniff every blob for the ``.osu`` header and hash the
    candidates. The index is cached per store and rebuilt on a miss, which also picks
    up beatmaps imported while the studio was running.
    """
    files_root = Path(files_root)
    if not files_root.is_dir():
        raise SystemExit(f"osu!lazer files folder not found: {files_root}")
    wanted = beatmap_hash.lower()
    key = str(files_root)
    cached = _STORE_INDEX.get(key)
    if cached is not None and wanted in cached:
        return cached[wanted]
    _STORE_INDEX[key] = _index_store(files_root)
    return _STORE_INDEX[key].get(wanted)


def filename_terms(name):
    """Artist/title guesses from a replay file name, longest first.

    ``[SHK]player - artist - title [diff] (2026-10-08) Osu.osr`` yields artist and
    title; the player tag and difficulty bracket carry no folder information.
    """
    stem = _TRAILING_DATE.sub("", Path(name).stem)
    stem = _BRACKETS.sub(" ", stem)
    terms = [part.strip().lower() for part in stem.split(" - ")]
    return [term for term in terms if len(term) >= 4]


def _words(text):
    """Alphanumeric words, so punctuation cannot decide whether a folder matches.

    A replay name says ``yoshikawa45 vs. siesta45`` while the song folder says
    ``yoshikawa45 vs siesta45``; comparing whole phrases made every folder score 0.
    """
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def find_beatmap(beatmap_hash, songs_root, terms=(), max_folders=400):
    """Return the ``.osu`` whose MD5 matches ``beatmap_hash``, or None.

    Folders sharing more hint words are hashed first, so the common case (one song
    folder matches the artist and title) reads a handful of files instead of the
    whole library.
    """
    songs_root = Path(songs_root)
    if not songs_root.is_dir():
        raise SystemExit(f"Songs folder not found: {songs_root}")
    wanted = _words(" ".join(terms))
    ranked = []
    for folder in songs_root.iterdir():
        if not folder.is_dir():
            continue
        score = len(wanted & _words(folder.name)) if wanted else 0
        if wanted and not score:
            continue
        ranked.append((score, folder))
    ranked.sort(key=lambda item: (-item[0], item[1].name))
    if not ranked:
        # The name hinted at nothing (a single token that is not an artist/title, or a
        # renamed replay). Fall back to the whole library by hash instead of giving up.
        ranked = [(0, folder) for folder in songs_root.iterdir() if folder.is_dir()]
        ranked.sort(key=lambda item: item[1].name)
    for _, folder in ranked[:max_folders]:
        for candidate in sorted(folder.glob("*.osu")):
            if file_hash(candidate) == beatmap_hash.lower():
                return candidate
    return None


def _parse_object(line):
    parts = line.split(",")
    type_bits = int(parts[3])
    if type_bits & HIT_SPINNER:
        kind = "spinner"
    elif type_bits & HIT_SLIDER:
        kind = "slider"
    else:
        kind = "circle"
    return {
        "time": int(float(parts[2])),
        "x": int(float(parts[0])),
        "y": int(float(parts[1])),
        "kind": kind,
    }


def _bpm(timing_points):
    """BPM of the first uninherited timing point, which is what a viewer needs."""
    for parts in timing_points:
        if len(parts) > 1 and parts[6:7] != ["0"]:
            beat_length = float(parts[1])
            if beat_length > 0:
                return round(60000.0 / beat_length, 3)
    return 0.0


def parse_beatmap(path):
    path = Path(path)
    section = ""
    general, metadata, difficulty = {}, {}, {}
    timing_points = []
    notes = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "HitObjects":
            notes.append(_parse_object(line))
        elif section == "TimingPoints":
            timing_points.append(line.split(","))
        elif ":" in line:
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if section == "General":
                general[key] = value
            elif section == "Metadata":
                metadata[key] = value
            elif section == "Difficulty":
                difficulty[key] = value
    notes.sort(key=lambda note: note["time"])
    mode = int(general.get("Mode", 0) or 0)
    circle_size = float(difficulty.get("CircleSize", 4) or 4)
    if mode == 3:
        columns = max(1, round(circle_size))
        for note in notes:
            note["column"] = min(columns - 1, max(0, int(note["x"] * columns // 512)))
    return {
        "path": str(path),
        "mode": mode,
        "mode_name": MODE_NAMES.get(mode, f"mode {mode}"),
        "artist": metadata.get("ArtistUnicode") or metadata.get("Artist", ""),
        "title": metadata.get("TitleUnicode") or metadata.get("Title", ""),
        "creator": metadata.get("Creator", ""),
        "version": metadata.get("Version", ""),
        "audio": general.get("AudioFilename", ""),
        "od": float(difficulty.get("OverallDifficulty", 5) or 5),
        "ar": float(difficulty.get("ApproachRate", difficulty.get("OverallDifficulty", 5)) or 5),
        "circle_size": circle_size,
        "bpm": _bpm(timing_points),
        "length_ms": notes[-1]["time"] if notes else 0,
        "notes": notes,
    }


def hit_windows(od):
    """Approximate osu!standard judgement windows in ms (published stable formulas)."""
    od = min(10.0, max(0.0, float(od)))
    return {"300": 80 - 6 * od, "100": 140 - 8 * od, "50": 200 - 10 * od}


def judgement(delta_ms, windows):
    for name in ("300", "100", "50"):
        if abs(delta_ms) <= windows[name]:
            return name
    return None


def pair_presses(notes, streams, windows):
    """Match presses to the note they were aiming at.

    ``streams`` maps a stream name to a sorted list of press times. Each press takes
    the closest still-unclaimed note inside the 50 window; everything else is left
    unpaired so the viewer can show stray presses and unhit notes separately.
    """
    presses = sorted(
        (time_ms, name) for name, times in streams.items() for time_ms in times
    )
    note_times = [note["time"] for note in notes]
    claimed = set()
    pairs = []
    stray = []
    for time_ms, name in presses:
        best_index = None
        best_delta = None
        # Only notes inside the widest judgement window can claim this press, so the
        # search stays a couple of candidates even on dense maps.
        low = bisect_left(note_times, time_ms - windows["50"])
        high = bisect_left(note_times, time_ms + windows["50"] + 1)
        for index in range(low, high):
            if index in claimed:
                continue
            delta = time_ms - note_times[index]
            if best_delta is None or abs(delta) < abs(best_delta):
                best_index, best_delta = index, delta
        if best_index is None:
            stray.append({"time": time_ms, "stream": name})
            continue
        claimed.add(best_index)
        pairs.append(
            {
                "note": notes[best_index]["time"],
                "press": time_ms,
                "stream": name,
                "delta": best_delta,
                "judgement": judgement(best_delta, windows),
            }
        )
    pairs.sort(key=lambda pair: pair["note"])
    missed = [note for index, note in enumerate(notes) if index not in claimed]
    return pairs, stray, missed

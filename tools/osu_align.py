"""Align a host-side capture with an osu! replay by matching key transitions.

tools/tap_capture.py records the game switches as ordinary host key events on a
monotonic clock; the replay records those same presses as K1/K2 bit transitions in
map time. Both come from the same physical presses, so the two sequences of press
instants can be matched: try every plausible offset, score it by how many host
presses land within a tolerance of a replay press, then refine with the mean
residual. A correct alignment shows up as a near 1:1 match with sub-millisecond
residuals; a wrong key assignment shows up as a much lower match count.

The tool writes an aligned NDJSON that also carries the replay edges as synthetic
key events (vk 0x101/0x102), so tools/plot_depth.py can draw the device depth, the
host events and the replay events on one axis.
"""

import argparse
import json
import sys
from bisect import bisect_left
from pathlib import Path

from analyze import replay_frames
from game_state import coarse_offset, load_state_samples
from plot_depth import find_episodes

REPLAY_KEYS = ("K1", "K2")
REPLAY_BITS = {"K1": 0x01, "K2": 0x02}
REPLAY_VK = {"K1": 0x101, "K2": 0x102}
VK_NAMES = {0x5A: "Z", 0x58: "X", 0x43: "C"}


def load_capture(path):
    edges = []
    levels = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            kind = record.get("type")
            if kind == "keyboard":
                if record.get("injected"):
                    continue
                edges.append(
                    {
                        "host_ms": record["host_ns"] / 1e6,
                        "vk": int(record["vk"]),
                        "down": bool(record["down"]),
                    }
                )
            elif kind == "levels":
                levels.append((record["host_ns"] / 1e6, [int(v) for v in record["levels"]]))
    edges.sort(key=lambda edge: edge["host_ms"])
    return edges, levels


def replay_edges(frames):
    edges = []
    previous = 0
    for time_ms, _, _, keys in frames:
        changed = previous ^ keys
        for name in REPLAY_KEYS:
            if changed & REPLAY_BITS[name]:
                edges.append(
                    {
                        "replay_ms": float(time_ms),
                        "key": name,
                        "down": bool(keys & REPLAY_BITS[name]),
                    }
                )
        previous = keys
    return edges


def match_count(host_times, replay_times, offset, tolerance_ms):
    """Greedy monotone match; returns (matches, signed residuals)."""
    matched = 0
    residuals = []
    index = 0
    for host_ms in host_times:
        target = host_ms + offset
        while index < len(replay_times) and replay_times[index] < target - tolerance_ms:
            index += 1
        if index < len(replay_times) and abs(replay_times[index] - target) <= tolerance_ms:
            residuals.append(replay_times[index] - target)
            matched += 1
            index += 1
    return matched, residuals


def offset_candidates(host_times, replay_times, tolerance_ms, window):
    """Offsets worth scoring: a sweep of the anchor window plus press-pair seeds.

    Seeding from the first few host presses is not enough when the host stream has
    extra downs the replay does not - holding a switch for a stream makes Windows
    auto-repeat it - because the first events can all be repeats with no replay
    partner, hiding the true offset. So the seeds are spread across the whole
    sequence, and inside an anchor window the search is a dense sweep so the sharp
    peak cannot fall between two seeds.
    """
    offsets = []
    if window is not None:
        center, span = window
        step = max(1.0, tolerance_ms / 2.0)
        value = center - span
        while value <= center + span + 1e-9:
            offsets.append(value)
            value += step
    stride = max(1, len(host_times) // 16)
    for seed in host_times[::stride][:16]:
        for candidate in replay_times:
            offset = candidate - seed
            if window is not None and abs(offset - window[0]) > window[1]:
                continue
            offsets.append(offset)
    return offsets


def align(host_times, replay_times, tolerance_ms, window=None):
    """Find the host -> map offset, optionally restricted to a window.

    ``window`` is ``(center_ms, span_ms)`` in the same sign convention as the
    returned offset. An external clock (tools/game_state.py) supplies it so a
    repetitive map cannot settle on a match that is a beat away.
    """
    if not host_times or not replay_times:
        return None
    best = None
    for offset in offset_candidates(host_times, replay_times, tolerance_ms, window):
        matched, residuals = match_count(host_times, replay_times, offset, tolerance_ms)
        if not matched:
            continue
        mean = sum(residuals) / len(residuals)
        rank = (matched, -abs(mean))
        if best is None or rank > best[0]:
            best = (rank, offset)
    if best is None:
        return None
    matched, residuals = match_count(host_times, replay_times, best[1], tolerance_ms)
    refinement = best[1] + (sum(residuals) / len(residuals) if residuals else 0.0)
    matched, residuals = match_count(host_times, replay_times, refinement, tolerance_ms)
    return {
        "offset_ms": refinement,
        "matched": matched,
        "total": len(replay_times),
        "max_residual_ms": max((abs(value) for value in residuals), default=0.0),
    }


def align_with_anchor(host_times, replay_times, tolerance_ms, anchor_ms=None, span_ms=250.0):
    """Align, using an external clock as a prior rather than as the answer.

    A clean capture matches every host press at the true offset, so no other offset
    can beat that count: rank the two searches by match count and give a tie to the
    anchor. That keeps the useful half of the prior - on a repetitive map several
    offsets share the top count and the one inside the anchor window is the right
    one - without letting a stale anchor drag the result away, which is what happens
    when the window is trusted outright.

    Returns ``(alignment, source)``; ``source`` says which search was used.
    """
    full = align(host_times, replay_times, tolerance_ms)
    if anchor_ms is None:
        return full, "unrestricted"
    windowed = align(host_times, replay_times, tolerance_ms, window=(anchor_ms, span_ms))
    if windowed is None:
        return full, "unrestricted (nothing matched near the anchor)"
    if full is None:
        return windowed, "anchor"
    if windowed["matched"] >= full["matched"]:
        return windowed, "anchor"
    return full, "unrestricted (it matched more than the anchor window)"


def assign_keys(host_by_vk, replay_by_key, offset, tolerance_ms):
    total = {}
    for vk, times in host_by_vk.items():
        for name in REPLAY_KEYS:
            matched, _ = match_count(times, replay_by_key[name], offset, tolerance_ms)
            total[(vk, name)] = matched
    first, second = REPLAY_KEYS
    direct = total.get((list(host_by_vk)[0], first), 0) + total.get((list(host_by_vk)[1], second), 0)
    swapped = total.get((list(host_by_vk)[0], second), 0) + total.get((list(host_by_vk)[1], first), 0)
    vks = list(host_by_vk)
    pairs = [(vks[0], first), (vks[1], second)] if direct >= swapped else [(vks[0], second), (vks[1], first)]
    return dict(pairs), total


def make_level_lookup(levels):
    times = [time_ms for time_ms, _ in levels]

    def lookup(time_ms, index):
        position = bisect_left(times, time_ms)
        candidates = [levels[offset] for offset in (position - 1, position) if 0 <= offset < len(levels)]
        if not candidates:
            return None
        nearest = min(candidates, key=lambda entry: abs(entry[0] - time_ms))
        return nearest[1][index]

    return lookup


def double_clicks(edges, vks, gap_ms):
    findings = []
    for vk in vks:
        stream = [edge for edge in edges if edge["vk"] == vk]
        index = 0
        while index + 2 < len(stream):
            first, second, third = stream[index : index + 3]
            if first["down"] and not second["down"] and third["down"]:
                gap = third["host_ms"] - second["host_ms"]
                if gap <= gap_ms:
                    findings.append(
                        {
                            "vk": vk,
                            "release_ms": second["host_ms"],
                            "press_ms": third["host_ms"],
                            "gap_ms": gap,
                        }
                    )
                index += 2
            else:
                index += 1
    return findings


def main():
    parser = argparse.ArgumentParser(description="Align a capture with an osu! replay")
    parser.add_argument("capture", type=Path)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--keys", default="90,88", help="host vk codes of the game switches")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tolerance-ms", type=float, default=6.0)
    parser.add_argument("--gap-ms", type=float, default=60.0, help="release-to-press gap flagged as a double click")
    parser.add_argument("--level-threshold", default="6,4,8", help="per-switch raw level that counts as actuated")
    parser.add_argument("--um-per-level", type=float, default=50.0, help="raw to micrometre scale (linear 50 um/count)")
    parser.add_argument(
        "--state-window-ms",
        type=float,
        default=250.0,
        help="restrict the press search to this span around the state reader's anchor",
    )
    parser.add_argument("--no-state-window", action="store_true", help="ignore the state reader anchor")
    args = parser.parse_args()

    vks = [int(item) for item in args.keys.split(",")]
    edges, levels = load_capture(args.capture)
    metadata, frames = replay_frames(args.replay)
    replays = replay_edges(frames)

    host_by_vk = {
        vk: [edge["host_ms"] for edge in edges if edge["vk"] == vk and edge["down"]] for vk in vks
    }
    replay_by_key = {
        name: [edge["replay_ms"] for edge in replays if edge["key"] == name and edge["down"]]
        for name in REPLAY_KEYS
    }
    merged_host = sorted(time for times in host_by_vk.values() for time in times)
    merged_replay = sorted(time for times in replay_by_key.values() for time in times)

    print(f"capture : {len(levels)} level samples, {len(edges)} host edges")
    for vk, times in host_by_vk.items():
        print(f"  vk {vk} ({VK_NAMES.get(vk, '?')}) presses: {len(times)}")
    print(f"replay  : player={metadata.get('player')!r} frames={len(frames)}")
    for name in REPLAY_KEYS:
        print(f"  {name} presses: {len(replay_by_key[name])}")

    coarse = None
    window = None
    state_pairs, state_checksum = load_state_samples(args.capture)
    if state_pairs and not args.no_state_window:
        coarse = coarse_offset(state_pairs)
        if coarse is not None:
            replay_hash = str(metadata.get("beatmap_hash") or "").lower()
            if state_checksum and replay_hash and state_checksum != replay_hash:
                print(
                    f"state   : the reader saw map {state_checksum}, the replay is {replay_hash}; "
                    "ignoring the anchor"
                )
                coarse = None
            else:
                window = (coarse["offset_ms"], args.state_window_ms)
                print(
                    f"state   : {len(state_pairs)} samples, coarse offset {coarse['offset_ms']:.1f} ms "
                    f"(staleness {coarse['stale_ms']:.0f} ms, rate {coarse['rate']:.4f}, "
                    f"{coarse['pairs']} pairs)"
                )

    anchor_ms = None if coarse is None else coarse["offset_ms"]
    alignment, source = align_with_anchor(
        merged_host, merged_replay, args.tolerance_ms, anchor_ms, args.state_window_ms
    )
    if alignment is None:
        raise SystemExit("No alignment found; check --keys and the replay.")
    alignment["coarse"] = coarse
    print(
        f"align   : offset {alignment['offset_ms']:.2f} ms, "
        f"matched {alignment['matched']}/{alignment['total']} presses, "
        f"max residual {alignment['max_residual_ms']:.2f} ms"
        + (f", reader said {coarse['offset_ms']:.1f} ms ({source})" if coarse else "")
    )

    assignment, totals = assign_keys(host_by_vk, replay_by_key, alignment["offset_ms"], args.tolerance_ms)
    for (vk, name), count in totals.items():
        print(f"  vk {VK_NAMES.get(vk, vk)} -> {name}: {count} matched")
    print(f"assignment: {[(VK_NAMES.get(vk, vk), name) for vk, name in assignment.items()]}")

    thresholds = [int(item) for item in args.level_threshold.split(",")]
    switch_count = len(levels[0][1]) if levels else 0
    activations = {}
    for index in range(switch_count):
        points = [(time_ms, values[index], 0) for time_ms, values in levels]
        episodes = find_episodes(points, thresholds[index] if index < len(thresholds) else thresholds[-1])
        activations[index] = episodes
        raw = [values[index] for _, values in levels]
        print(
            f"switch {index}: raw {min(raw)}..{max(raw)}, "
            f"{len(episodes)} activation(s) at threshold "
            f"{thresholds[index] if index < len(thresholds) else thresholds[-1]}"
        )

    findings = double_clicks(edges, vks, args.gap_ms)
    print(f"host double-click candidates (release->press <= {args.gap_ms} ms): {len(findings)}")
    lookup = make_level_lookup(levels)
    for finding in findings:
        vk = finding["vk"]
        replay_ms = finding["press_ms"] + alignment["offset_ms"]
        depth = [lookup(finding["release_ms"], index) for index in range(switch_count)]
        depth_press = [lookup(finding["press_ms"], index) for index in range(switch_count)]
        print(
            f"  vk {VK_NAMES.get(vk, vk)} release {finding['release_ms'] / 1000:.3f} s "
            f"-> press +{finding['gap_ms']:.1f} ms (replay {replay_ms:.0f} ms) "
            f"depth at release {depth}, at press {depth_press}"
        )

    args.out.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "metadata",
        "schema_version": 2,
        "alignment": {
            "offset_ms": alignment["offset_ms"],
            "matched": alignment["matched"],
            "total": alignment["total"],
            "max_residual_ms": alignment["max_residual_ms"],
            "state_reader": alignment.get("coarse"),
            "replay": metadata,
            "assignment": {str(vk): name for vk, name in assignment.items()},
        },
    }
    replay_records = [
        {
            "type": "keyboard",
            "host_ns": int((edge["replay_ms"] - alignment["offset_ms"]) * 1e6),
            "vk": REPLAY_VK[edge["key"]],
            "scan": 0,
            "down": edge["down"],
            "injected": False,
        }
        for edge in replays
    ]
    with (args.out / "aligned.jsonl").open("w", encoding="utf-8", newline="") as target:
        target.write(json.dumps(header, ensure_ascii=False) + "\n")
        for record in replay_records:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
        with args.capture.open(encoding="utf-8") as source:
            for line in source:
                if line.startswith('{"type": "metadata"'):
                    continue
                target.write(line if line.endswith("\n") else line + "\n")
    (args.out / "alignment.json").write_text(
        json.dumps(header["alignment"], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\naligned ndjson: {(args.out / 'aligned.jsonl').resolve()}")
    print(
        "plot it with: "
        f"python tools/plot_depth.py {args.out / 'aligned.jsonl'} --out depth-aligned.html"
    )


if __name__ == "__main__":
    main()

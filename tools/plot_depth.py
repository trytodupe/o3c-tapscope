"""Render a key-depth capture (analog levels plus host keyboard edges) as HTML.

Handles the NDJSON captures written by tools/hall_capture.py and tools/tap_capture.py:

    {"type": "levels",   "host_ns": .., "levels": [lv0, lv1, lv2]}
    {"type": "keyboard", "host_ns": .., "vk": .., "down": ..}

Switch index becomes its own series and its own activation analysis. Captures made
before the frame layout was decoded stored `{"level": u16, "state": u16}` where the
u16 actually packed two switch levels and `state` held the third; those are unpacked
here so old files plot correctly instead of showing a phantom value.

The level axis stays raw because the physical unit is not calibrated yet, so neither
the summary nor the plot claims millimetres. The signal that matters is an activation
that ends and restarts much faster than a finger can move, which is what a spurious
double click looks like.
"""

import argparse
import json
from bisect import bisect_left
from pathlib import Path

VK_NAMES = {
    0x101: "replay K1",
    0x102: "replay K2",
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x10: "Shift",
    0x11: "Ctrl",
    0x12: "Alt",
    0x1B: "Esc",
    0x20: "Space",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x41: "A",
    0x42: "B",
    0x43: "C",
    0x44: "D",
    0x45: "E",
    0x46: "F",
    0x47: "G",
    0x48: "H",
    0x49: "I",
    0x4A: "J",
    0x4B: "K",
    0x4C: "L",
    0x4E: "N",
    0x4F: "O",
    0x50: "P",
    0x53: "S",
    0x55: "U",
    0x56: "V",
    0x57: "W",
    0x58: "X",
    0x59: "Y",
    0x5A: "Z",
    0x87: "F24",
}

DETAIL_LIMIT = 40


def vk_name(vk):
    return VK_NAMES.get(vk, f"0x{vk:02X}")


def load(path):
    series = {}
    edges = []
    meta = {}
    with path.open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            kind = record.get("type")
            if kind == "levels":
                time_ms = record["host_ns"] / 1e6
                for index, level in enumerate(record["levels"]):
                    series.setdefault(index, []).append((time_ms, int(level), 0))
            elif kind in ("sample", "level"):
                packed_level = int(record["level"])
                packed_state = int(record.get("state", 0))
                meta["legacy_layout_unpacked"] = True
                time_ms = record["host_ns"] / 1e6
                for index, level in enumerate(
                    (packed_level & 0xFF, (packed_level >> 8) & 0xFF, packed_state & 0xFF)
                ):
                    series.setdefault(index, []).append((time_ms, level, 0))
            elif kind == "keyboard":
                edges.append((record["host_ns"] / 1e6, int(record["vk"]), bool(record["down"])))
            elif kind == "metadata":
                meta = record
    for points in series.values():
        points.sort(key=lambda point: point[0])
    edges.sort(key=lambda edge: edge[0])
    return series, edges, meta


def decimate(points, limit):
    """Keep the extremes of every bucket so short spikes survive downsampling."""
    if limit <= 0 or len(points) <= limit:
        return points
    buckets = max(1, limit // 2)
    size = len(points) / buckets
    kept = []
    for index in range(buckets):
        start = int(index * size)
        stop = max(start + 1, int((index + 1) * size))
        bucket = points[start:stop]
        lowest = min(bucket, key=lambda point: point[1])
        highest = max(bucket, key=lambda point: point[1])
        kept.extend(sorted((lowest, highest), key=lambda point: point[0]))
    return kept


def parse_thresholds(text, count):
    """Per-switch activation thresholds; the last value repeats for extra switches.

    The three magnetic switches do not share an idle level (Z rests at 0-1, X at 0,
    C at 1-4), so one global threshold either misses shallow presses or turns the C
    key's rest position into a phantom activation.
    """
    values = [int(item) for item in (part.strip() for part in text.split(",")) if item]
    if not values:
        raise SystemExit("--threshold needs at least one level, e.g. 6,4,8")
    return [values[index] if index < len(values) else values[-1] for index in range(count)]


def find_episodes(points, threshold):
    episodes = []
    start = None
    for index, (_, level, _) in enumerate(points):
        if level >= threshold and start is None:
            start = index
        elif level < threshold and start is not None:
            episodes.append(slice(start, index))
            start = None
    if start is not None:
        episodes.append(slice(start, len(points)))
    return episodes


def build_holds(edges, kinds):
    holds = []
    opened = {}
    for (time_ms, vk, _), kind in zip(edges, kinds):
        if kind == 1:
            opened[vk] = time_ms
        elif kind == 0 and vk in opened:
            holds.append((opened.pop(vk), time_ms, vk))
    if edges:
        for vk, time_ms in opened.items():
            holds.append((time_ms, edges[-1][0], vk))
    holds.sort()
    return holds


def classify_edges(edges):
    """Tag every edge: 0 = release, 1 = real press, 2 = auto-repeat while held.

    Windows auto-repeat emits a stream of key-down events (about 30 ms apart after a
    ~500 ms delay) while a key is held without any key-up between them. Counting those
    as presses would look exactly like a double click, so they are separated here.
    """
    kinds = []
    held = set()
    for _, vk, down in edges:
        if down:
            if vk in held:
                kinds.append(2)
            else:
                kinds.append(1)
                held.add(vk)
        else:
            kinds.append(0)
            held.discard(vk)
    return kinds


def nearest(points, times, time_ms):
    index = bisect_left(times, time_ms)
    candidates = [points[i] for i in (index - 1, index) if 0 <= i < len(points)]
    if not candidates:
        return None
    return min(candidates, key=lambda point: abs(point[0] - time_ms))


def summarize(series, edges, holds, thresholds, gap_ms, origin):
    lines = []
    for key in sorted(series):
        points = series[key]
        threshold = thresholds[key] if key < len(thresholds) else thresholds[-1]
        times = [point[0] for point in points]
        duration = (times[-1] - times[0]) / 1000
        levels = [point[1] for point in points]
        rate = (len(points) - 1) / duration if duration > 0 else 0.0
        episodes = find_episodes(points, threshold)
        lines.append(
            f"key {key}: {len(points)} samples over {duration:.3f} s -> {rate:.1f} Hz,"
            f" raw {min(levels)}..{max(levels)}, {len(episodes)} activation(s)"
            f" at threshold {threshold}"
        )
        for index, episode in enumerate(episodes):
            chunk = points[episode]
            peak = max(point[1] for point in chunk)
            peak_time = next(point[0] for point in chunk if point[1] == peak)
            lines.append(
                f"  [{index}] start {chunk[0][0] - origin:10.3f} ms  duration {chunk[-1][0] - chunk[0][0]:8.3f} ms"
                f"  rise {peak_time - chunk[0][0]:7.3f} ms  peak {peak:3d}"
            )
        for index in range(1, len(episodes)):
            gap = points[episodes[index].start][0] - points[episodes[index - 1].stop - 1][0]
            flag = "   <-- rapid re-actuation" if gap < gap_ms else ""
            lines.append(f"  gap {index - 1}->{index}: {gap:.3f} ms{flag}")
        if not episodes:
            lines.append("  (never crossed the threshold)")
    kinds = classify_edges(edges)
    real = sum(1 for kind in kinds if kind == 1)
    repeats = sum(1 for kind in kinds if kind == 2)
    lines.append(
        f"keyboard: {len(edges)} events, {real} real press(es), {repeats} auto-repeat down(s),"
        f" {len(holds)} held interval(s)"
    )
    presses = [(time_ms, vk) for (time_ms, vk, _), kind in zip(edges, kinds) if kind == 1]
    by_vk = {}
    for time_ms, vk in presses:
        by_vk.setdefault(vk, []).append(time_ms)
    for vk in sorted(by_vk):
        times_vk = by_vk[vk]
        detail = f"  vk {vk_name(vk):>9}  presses {len(times_vk)}"
        if len(times_vk) > 1:
            shortest = min(second - first for first, second in zip(times_vk, times_vk[1:]))
            marker = "   <-- rapid repeat" if shortest < gap_ms else ""
            detail += f"  min down-down {shortest:.3f} ms{marker}"
        lines.append(detail)
    if presses and len(presses) <= DETAIL_LIMIT:
        for time_ms, vk in presses:
            best = None
            for key in sorted(series):
                points = series[key]
                time_list = [point[0] for point in points]
                point = nearest(points, time_list, time_ms)
                if point and (best is None or point[1] > best[1]):
                    best = (point[0], point[1], key)
            if best:
                lines.append(
                    f"  vk {vk_name(vk):>9} down at {time_ms - origin:10.3f} ms -> key {best[2]} level {best[1]}"
                    f" ({best[0] - time_ms:+.3f} ms offset)"
                )
    else:
        lines.append(f"  ({len(presses)} presses; per-press cross-check suppressed)")
    lines.append("note: level axis is raw (uncalibrated); do not read it as millimetres")
    return lines


TEMPLATE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Key depth vs time</title>
<style>
body{font:14px/1.5 system-ui;margin:20px;background:#0f1419;color:#e6edf3}
h1{font-size:18px;font-weight:600}
.controls{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:10px 0}
button{font:inherit;padding:6px 12px;background:#1f2933;color:#e6edf3;border:1px solid #38424d;border-radius:4px;cursor:pointer}
canvas{width:100%;height:540px;border:1px solid #2b3440;background:#0b0f13;display:block;touch-action:none;cursor:crosshair}
pre{background:#161c22;padding:12px;white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace}
.hint{color:#8b98a5}
</style>
<h1>Key depth vs time</h1>
<div class="controls">
  <button id="reset">Reset zoom</button>
  <span class="hint">wheel = zoom, drag = pan, double click = reset</span>
  <span id="cursor">-</span>
</div>
<canvas id="chart"></canvas>
<pre id="stats"></pre>
<script>
const capture = __CAPTURE__;
const palette = ["#31c48d", "#4c8dff", "#f4a261", "#e06c9f", "#c792ea", "#ffd166"];
const canvas = document.getElementById("chart");
const context = canvas.getContext("2d");
const cursor = document.getElementById("cursor");
const series = capture.series.map((entry, index) => ({
  key: entry.key,
  color: palette[index % palette.length],
  points: entry.points,
  times: entry.points.map(point => point[0]),
}));
const edges = capture.edges;
const thresholds = capture.threshold;
let origin = Infinity, last = -Infinity, maxLevel = 1;
for (const entry of series){
  if (entry.points.length){
    origin = Math.min(origin, entry.times[0]);
    last = Math.max(last, entry.times[entry.times.length - 1]);
  }
  for (const point of entry.points) if (point[1] > maxLevel) maxLevel = point[1];
}
if (!Number.isFinite(origin)){ origin = 0; last = 1; }
maxLevel = Math.max(maxLevel, ...thresholds, 1) + 1;
const full = {t0: origin, t1: last};
let view = {...full};
let dims = {w: 900, h: 540};
const padding = {left: 58, right: 16, top: 16, bottom: 32};
document.getElementById("stats").textContent = capture.summary.join("\\n");

function layout(){
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 900;
  const h = 540;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  dims = {w, h};
}
function plotWidth(){ return dims.w - padding.left - padding.right; }
function plotHeight(){ return dims.h - padding.top - padding.bottom; }
function xOf(t){ return padding.left + (t - view.t0) / ((view.t1 - view.t0) || 1) * plotWidth(); }
function yOf(v){ return padding.top + (1 - v / maxLevel) * plotHeight(); }
function tOf(px){ return view.t0 + (px - padding.left) / plotWidth() * (view.t1 - view.t0); }
function clampView(){
  const span = Math.min(view.t1 - view.t0, full.t1 - full.t0);
  const t0 = Math.max(full.t0, Math.min(view.t0, full.t1 - span));
  view = {t0, t1: t0 + span};
}
function lowerBound(times, t){
  let low = 0, high = times.length;
  while (low < high){ const mid = (low + high) >> 1; if (times[mid] < t) low = mid + 1; else high = mid; }
  return low;
}
function draw(){
  context.clearRect(0, 0, dims.w, dims.h);
  context.font = "12px system-ui";
  const yStep = maxLevel > 60 ? 10 : maxLevel > 24 ? 5 : 2;
  context.lineWidth = 1;
  for (let value = 0; value <= maxLevel; value += yStep){
    const y = yOf(value);
    context.strokeStyle = thresholds.includes(value) ? "#7c5cff" : "#1b2430";
    context.beginPath(); context.moveTo(padding.left, y); context.lineTo(padding.left + plotWidth(), y); context.stroke();
    context.fillStyle = thresholds.includes(value) ? "#a99bff" : "#8b98a5";
    context.fillText(String(value), 12, y + 4);
  }
  const span = view.t1 - view.t0;
  const step = Math.max(1, Math.round(span / 8));
  for (let t = Math.ceil(view.t0 / step) * step; t <= view.t1; t += step){
    const x = xOf(t);
    context.strokeStyle = "#141b24";
    context.beginPath(); context.moveTo(x, padding.top); context.lineTo(x, padding.top + plotHeight()); context.stroke();
    context.fillStyle = "#8b98a5";
    context.fillText(((t - origin) / 1000).toFixed(2) + "s", x - 12, dims.h - 12);
  }
  context.fillStyle = "#8b98a5";
  context.fillText("raw level", 12, padding.top - 2);
  context.save();
  context.beginPath();
  context.rect(padding.left, padding.top, plotWidth(), plotHeight());
  context.clip();
  for (const [from, to] of capture.holds){
    if (to < view.t0 || from > view.t1) continue;
    context.fillStyle = "rgba(255, 255, 255, 0.07)";
    context.fillRect(xOf(from), padding.top, Math.max(1, xOf(to) - xOf(from)), plotHeight());
  }
  for (const entry of series){
    if (!entry.points.length) continue;
    const first = lowerBound(entry.times, view.t0);
    const stop = Math.min(entry.points.length, lowerBound(entry.times, view.t1) + 1);
    if (stop <= first) continue;
    const count = stop - first;
    const stride = Math.max(1, Math.floor(count / 20000));
    context.strokeStyle = entry.color;
    context.lineWidth = 1.2;
    context.beginPath();
    for (let index = first; index < stop; index += stride){
      const point = entry.points[index];
      const x = xOf(point[0]), y = yOf(point[1]);
      if (index === first) context.moveTo(x, y); else context.lineTo(x, y);
    }
    context.stroke();
    if (count <= 2000){
      context.fillStyle = entry.color;
      for (let index = first; index < stop; index++){
        const point = entry.points[index];
        context.beginPath(); context.arc(xOf(point[0]), yOf(point[1]), 1.8, 0, Math.PI * 2); context.fill();
      }
    }
  }
  context.lineWidth = 1;
  for (const [time_ms, vk, kind] of edges){
    if (time_ms < view.t0 || time_ms > view.t1) continue;
    const x = xOf(time_ms);
    context.strokeStyle = kind === 1 ? "#f4a261" : kind === 2 ? "#6b4f2a" : "#5a6b7b";
    context.setLineDash(kind === 1 ? [] : [4, 4]);
    context.lineWidth = kind === 1 ? 1.4 : 1;
    context.beginPath(); context.moveTo(x, padding.top); context.lineTo(x, padding.top + plotHeight()); context.stroke();
  }
  context.setLineDash([]);
  context.lineWidth = 1;
  context.restore();
  let legendX = padding.left + 8, legendY = padding.top + 14;
  for (const entry of series){
    context.fillStyle = entry.color;
    context.fillRect(legendX, legendY - 9, 10, 10);
    context.fillStyle = "#c9d5e1";
    context.fillText(`key ${entry.key}`, legendX + 15, legendY);
    legendX += 70;
  }
}
function showCursor(px){
  const t = tOf(px);
  const parts = [];
  for (const entry of series){
    if (!entry.points.length) continue;
    const index = Math.min(entry.points.length - 1, Math.max(0, lowerBound(entry.times, t)));
    const point = entry.points[index];
    parts.push(`switch ${entry.key} level=${point[1]} @${(point[0] - origin).toFixed(2)}ms`);
  }
  cursor.textContent = parts.join("   |   ") || "-";
}
canvas.addEventListener("wheel", event => {
  event.preventDefault();
  const anchor = tOf(event.offsetX);
  const factor = Math.exp(event.deltaY * 0.0015);
  const t0 = anchor - (anchor - view.t0) * factor;
  const t1 = anchor + (view.t1 - anchor) * factor;
  if (t1 - t0 < 0.05) return;
  view = {t0, t1};
  clampView();
  draw();
}, {passive: false});
let drag = null;
canvas.addEventListener("pointerdown", event => {
  drag = {x: event.offsetX, t0: view.t0, t1: view.t1};
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener("pointermove", event => {
  if (drag){
    const shift = (event.offsetX - drag.x) / plotWidth() * (drag.t1 - drag.t0);
    view = {t0: drag.t0 - shift, t1: drag.t1 - shift};
    clampView();
    draw();
  } else {
    showCursor(event.offsetX);
  }
});
canvas.addEventListener("pointerup", () => { drag = null; });
canvas.addEventListener("dblclick", () => { view = {...full}; draw(); });
document.getElementById("reset").onclick = () => { view = {...full}; draw(); };
window.addEventListener("resize", () => { layout(); draw(); });
layout();
draw();
</script></html>
"""


def main():
    parser = argparse.ArgumentParser(description="Plot key depth (and keyboard edges) from a capture")
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, default=Path("depth-plot.html"))
    parser.add_argument(
        "--threshold",
        default="6,4,8",
        help="comma-separated raw level per switch that counts as actuated; the last value repeats",
    )
    parser.add_argument("--gap-ms", type=float, default=30.0, help="release-to-press gap flagged as suspicious")
    parser.add_argument("--max-points", type=int, default=600000, help="per-key point budget for the HTML")
    args = parser.parse_args()

    series, edges, meta = load(args.capture)
    thresholds = parse_thresholds(args.threshold, max(series, default=-1) + 1)
    kinds = classify_edges(edges)
    holds = build_holds(edges, kinds)
    origin = min((points[0][0] for points in series.values() if points), default=0.0)
    summary = summarize(series, edges, holds, thresholds, args.gap_ms, origin)
    if meta.get("legacy_layout_unpacked"):
        summary.append(
            "note: capture predates the decoded layout; the u16 level/state pair was unpacked"
            " back into the three switch levels"
        )
    payload_series = []
    for key in sorted(series):
        points = series[key]
        reduced = decimate(points, args.max_points)
        if len(reduced) != len(points):
            summary.append(f"key {key}: plotted {len(reduced)} of {len(points)} samples (extremes preserved)")
        payload_series.append({"key": key, "points": [[p[0], p[1], p[2]] for p in reduced]})
    payload = {
        "series": payload_series,
        "edges": [[time_ms, vk, kind] for (time_ms, vk, _), kind in zip(edges, kinds)],
        "holds": holds,
        "threshold": thresholds,
        "summary": summary,
        "meta": {
            key: meta[key]
            for key in ("analog", "request_index", "level_count", "keyboard_source")
            if key in meta
        },
    }
    args.out.write_text(TEMPLATE.replace("__CAPTURE__", json.dumps(payload)), encoding="utf-8")
    print("\n".join(summary))
    print(f"\nplot: {args.out.resolve()}")


if __name__ == "__main__":
    main()

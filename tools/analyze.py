import argparse
import csv
import json
import lzma
import math
import struct
from pathlib import Path


class ReplayReader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, length):
        if length < 0 or self.offset + length > len(self.data):
            raise ValueError("Truncated or invalid replay")
        result = self.data[self.offset:self.offset + length]
        self.offset += length
        return result

    def string(self):
        marker = self.take(1)[0]
        if marker == 0:
            return ""
        if marker != 11:
            raise ValueError("Invalid replay string marker")
        length = 0
        for shift in range(0, 35, 7):
            value = self.take(1)[0]
            length |= (value & 127) << shift
            if value < 128:
                return self.take(length).decode("utf-8")
        raise ValueError("Invalid replay string length")


def replay_frames(path):
    reader = ReplayReader(path.read_bytes())
    mode = reader.take(1)[0]
    reader.take(4)
    beatmap_hash = reader.string()
    player = reader.string()
    reader.string()
    reader.take(12 + 4 + 2 + 1)
    mods = struct.unpack("<I", reader.take(4))[0]
    reader.string()
    reader.take(8)
    length = struct.unpack("<i", reader.take(4))[0]
    decoded = lzma.decompress(reader.take(length)).decode("ascii")
    elapsed = 0
    frames = []
    for record in decoded.split(","):
        if not record:
            continue
        delta, position_x, position_y, keys = record.split("|")
        delta = int(delta)
        if delta == -12345:
            continue
        elapsed += delta
        frames.append((elapsed, float(position_x), float(position_y), int(keys)))
    return {"mode": mode, "mods": mods, "beatmap_hash": beatmap_hash, "player": player}, frames


def fit_anchors(path):
    with path.open(newline="", encoding="utf-8") as source:
        anchors = [(float(row["host_ms"]), float(row["replay_ms"])) for row in csv.DictReader(source)]
    if not anchors or not all(math.isfinite(value) for pair in anchors for value in pair):
        raise ValueError("Provide at least one finite anchor")
    host_mean = sum(pair[0] for pair in anchors) / len(anchors)
    replay_mean = sum(pair[1] for pair in anchors) / len(anchors)
    variance = sum((pair[0] - host_mean) ** 2 for pair in anchors)
    if len(anchors) > 1 and variance == 0:
        raise ValueError("Anchors need distinct host timestamps")
    scale = sum((host - host_mean) * (replay - replay_mean) for host, replay in anchors) / variance if variance else 1.0
    if scale <= 0:
        raise ValueError("Alignment scale must be positive")
    offset = replay_mean - scale * host_mean
    residuals = [replay - (scale * host + offset) for host, replay in anchors]
    return scale, offset, residuals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--anchors", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    scale, offset, residuals = fit_anchors(args.anchors)
    metadata, frames = replay_frames(args.replay)
    profile = json.loads(args.profile.read_text(encoding="utf-8")) if args.profile else None
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "replay.csv").open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(["replay_ms", "x", "y", "keys"])
        writer.writerows(frames)
    report_count = 0
    with args.capture.open(encoding="utf-8") as source, (args.out / "samples.csv").open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(["host_ms", "replay_ms", "report_id", "key", "raw", "value", "unit", "hex"])
        for line in source:
            record = json.loads(line)
            if record.get("type") != "report":
                continue
            report_count += 1
            host = float(record["host_ms"])
            aligned = host * scale + offset
            payload = bytes.fromhex(record["hex"])
            fields = profile["fields"] if profile and record["report_id"] == profile["report_id"] else []
            if not fields:
                writer.writerow([host, aligned, record["report_id"], "", "", "", "", record["hex"]])
            for field in fields:
                start, size = field["offset"], field["size"]
                if start < 0 or size < 1 or start + size > len(payload):
                    raise ValueError("Profile field exceeds report payload")
                raw = int.from_bytes(payload[start:start + size], field.get("byteorder", "little"), signed=field.get("signed", False))
                value = raw * field.get("scale", 1) + field.get("bias", 0)
                writer.writerow([host, aligned, record["report_id"], field["key"], raw, value, field.get("unit", "raw"), record["hex"]])
    result = {"replay": metadata, "reports": report_count, "alignment": {"scale": scale, "offset_ms": offset, "residuals_ms": residuals}, "depth_verified": False}
    (args.out / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

"""High-rate Hall (analog) key level capture for the SayoDevice O3C.

The official web configurator reads key depth by polling the 1023-byte Col03
report channel (report id 0x22) with a fixed request frame:

    request   12 3c 12 05 00 15 <key> 00 ... (1023 bytes)
response  12 <dev counter> 12 07 00 15 <index> <lv0> <lv1> <lv2> ...

`lv0..lv2` are the live positions of the three magnetic switches, each on the same
raw 0..79 scale. One answer therefore reports every switch, and `--key` only sets
the request index. See tools/level_protocol.py for the layout evidence.

The physical unit of the level is not calibrated yet; keep it raw.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hid_capture import devices, identify
from level_protocol import (
    COMMAND_LEVEL,
    LEVEL_COUNT,
    READ_SIZE,
    USAGE_PAGE_ANALOG,
    build_level_report,
    parse_level_frame,
)


def select_device(args, found):
    if args.index is not None:
        if args.index < 0 or args.index >= len(found):
            raise SystemExit("Index is outside the --list result.")
        return found[args.index]
    if args.path:
        for device in found:
            if str(device.get("path")) == args.path:
                return device
        raise SystemExit("No HID interface matches --path.")
    candidates = [
        device
        for device in found
        if device.get("vendor_id") == 0x8089 and device.get("usage_page") == USAGE_PAGE_ANALOG
    ]
    if not candidates:
        candidates = [
            device
            for device in found
            if device.get("vendor_id") == 0x8089 and "Col03" in str(device.get("path", ""))
        ]
    if not candidates:
        raise SystemExit(
            "No analog (usage_page 0xFF12 / Col03) interface found; run with --list and pick --index."
        )
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description="Capture SayoDevice analog key levels at high rate")
    parser.add_argument("--list", action="store_true", help="list SayoDevice HID interfaces and exit")
    parser.add_argument("--index", type=int, help="index from --list")
    parser.add_argument("--path", help="exact HID path")
    parser.add_argument(
        "--key",
        type=int,
        default=0,
        help="request index for the level command; the answer carries every switch anyway",
    )
    parser.add_argument("--rate", type=float, default=0.0, help="target polls per second; 0 means free run")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds")
    parser.add_argument("--limit", type=int, default=0, help="stop after N samples")
    parser.add_argument("--timeout-ms", type=int, default=20)
    parser.add_argument("--raw", action="store_true", help="store the full response frame per sample")
    parser.add_argument("--out", type=Path, default=Path("hall-capture.jsonl"))
    args = parser.parse_args()

    found = devices()
    if args.list:
        for index, device in enumerate(found):
            print(json.dumps({"index": index, **identify(device)}, ensure_ascii=False))
        return
    selected = select_device(args, found)
    import hid

    handle = hid.device()
    handle.open_path(selected["path"])
    handle.set_nonblocking(False)
    outbound = build_level_report(args.key)
    period = 1.0 / args.rate if args.rate > 0 else 0.0
    started = time.perf_counter_ns()
    metadata = {
        "type": "metadata",
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "timestamp_source": "native_perf_counter_ns",
        "device": identify(selected),
        "request_index": args.key,
        "level_count": LEVEL_COUNT,
        "command": COMMAND_LEVEL,
        "frame_head": outbound[:12].hex(),
        "target_rate_hz": args.rate,
        "level_unit": "raw",
    }
    count = 0
    timeouts = 0
    parser_errors = 0
    extremes = [(None, None) for _ in range(LEVEL_COUNT)]
    latencies = []
    next_deadline = time.perf_counter()
    with args.out.open("w", encoding="utf-8", newline="") as target:
        target.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        print(f"Polling {selected.get('product_string')} -> {args.out}")
        try:
            while True:
                if args.limit and count >= args.limit:
                    break
                if args.duration and (time.perf_counter_ns() - started) / 1e9 >= args.duration:
                    break
                if period:
                    next_deadline += period
                    delay = next_deadline - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_deadline = time.perf_counter()
                wrote = time.perf_counter_ns()
                handle.write(outbound)
                raw = bytes(handle.read(READ_SIZE, args.timeout_ms))
                if not raw:
                    timeouts += 1
                    continue
                parsed = parse_level_frame(raw, expected_index=args.key)
                if parsed is None:
                    parser_errors += 1
                    continue
                read_at = time.perf_counter_ns()
                record = {
                    "type": "levels",
                    "host_ns": read_at - started,
                    "latency_ns": read_at - wrote,
                    "levels": parsed["levels"],
                }
                if args.raw:
                    record["hex"] = raw.hex()
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
                target.flush()
                count += 1
                for position, level in enumerate(parsed["levels"]):
                    low, high = extremes[position]
                    extremes[position] = (
                        level if low is None else min(low, level),
                        level if high is None else max(high, level),
                    )
                latencies.append(read_at - wrote)
                if count % 200 == 0:
                    elapsed = (read_at - started) / 1e9
                    medium = sorted(latencies[-200:])[len(latencies[-200:]) // 2]
                    print(
                        f"  {count} samples, {count / elapsed:.1f} Hz, "
                        f"median round trip {medium / 1e6:.2f} ms, levels {parsed['levels']}",
                        file=sys.stderr,
                    )
        except KeyboardInterrupt:
            pass
        finally:
            elapsed = (time.perf_counter_ns() - started) / 1e9
            summary = {
                "type": "end",
                "samples": count,
                "rate_hz": count / elapsed if elapsed else 0,
                "timeouts": timeouts,
                "unparsed": parser_errors,
                "level_min": [low for low, _ in extremes],
                "level_max": [high for _, high in extremes],
            }
            target.write(json.dumps(summary, ensure_ascii=False) + "\n")
            handle.close()
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

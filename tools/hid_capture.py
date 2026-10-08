import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def devices():
    import hid

    return hid.enumerate()


def identify(device):
    return {
        "vendor_id": device.get("vendor_id"),
        "product_id": device.get("product_id"),
        "path": device.get("path", b"").decode(errors="replace") if isinstance(device.get("path"), bytes) else device.get("path"),
        "serial_number": device.get("serial_number"),
        "manufacturer_string": device.get("manufacturer_string"),
        "product_string": device.get("product_string"),
        "release_number": device.get("release_number"),
        "interface_number": device.get("interface_number"),
        "usage_page": device.get("usage_page"),
        "usage": device.get("usage"),
    }


def main():
    parser = argparse.ArgumentParser(description="Capture raw reports from a SayoDevice HID interface")
    parser.add_argument("--list", action="store_true", help="list HID interfaces and exit")
    parser.add_argument("--vid", type=lambda value: int(value, 0), default=0x8089)
    parser.add_argument("--pid", type=lambda value: int(value, 0), default=0x0009)
    parser.add_argument("--path", help="exact HID path; overrides VID/PID selection")
    parser.add_argument("--index", type=int, help="index from --list; overrides VID/PID and path selection")
    parser.add_argument("--out", type=Path, default=Path("capture-native.jsonl"))
    parser.add_argument("--timeout-ms", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0, help="stop after N reports; zero means unlimited")
    parser.add_argument("--send-hex", help="send one raw report before capture; requires explicit --index or --path")
    args = parser.parse_args()
    found = devices()
    if args.list:
        for index, device in enumerate(found):
            print(json.dumps({"index": index, **identify(device)}, ensure_ascii=False))
        return
    if args.index is not None:
        if args.index < 0 or args.index >= len(found):
            raise SystemExit("Index is outside the --list result.")
        candidates = [found[args.index]]
    else:
        candidates = [device for device in found if (args.path and str(device.get("path")) == args.path) or (not args.path and device.get("vendor_id") == args.vid and device.get("product_id") == args.pid)]
    if not candidates:
        raise SystemExit("No matching HID interface. Run with --list.")
    if len(candidates) > 1:
        print("Multiple interfaces found; selecting the first. Use --list and --path to select another.", file=sys.stderr)
    selected = candidates[0]
    import hid

    handle = hid.device()
    handle.open_path(selected["path"])
    handle.set_nonblocking(False)
    if args.send_hex:
        if args.index is None and not args.path:
            raise SystemExit("Refusing --send-hex without explicit --index or --path")
        outbound = bytes.fromhex(args.send_hex)
        handle.write(outbound)
        print(f"Sent {len(outbound)} bytes to report interface.")
    started = time.perf_counter_ns()
    metadata = {"type": "metadata", "schema_version": 2, "created_utc": datetime.now(timezone.utc).isoformat(), "timestamp_source": "native_perf_counter_ns", "vid": selected.get("vendor_id"), "pid": selected.get("product_id"), "device": identify(selected), "timeout_ms": args.timeout_ms, "payload_excludes_report_id": False}
    count = 0
    with args.out.open("w", encoding="utf-8", newline="") as target:
        target.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        print(f"Capturing {selected.get('product_string') or 'HID device'} to {args.out}. Press Ctrl+C to stop.")
        try:
            while args.limit <= 0 or count < args.limit:
                payload = handle.read(4096, args.timeout_ms)
                if not payload:
                    continue
                raw = bytes(payload)
                record = {"type": "report", "host_ns": time.perf_counter_ns() - started, "report_id": raw[0], "hex": raw.hex(), "length": len(raw)}
                target.write(json.dumps(record) + "\n")
                target.flush()
                count += 1
        except KeyboardInterrupt:
            pass
        finally:
            target.write(json.dumps({"type": "end", "reports": count}) + "\n")
            handle.close()
    print(f"Captured {count} reports.")


if __name__ == "__main__":
    main()

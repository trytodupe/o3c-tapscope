"""Probe several device read commands on one clock while recording host keys.

``cmd 0x15`` already answers with the live level of all three magnetic switches in a
single frame, so ordinary captures do not need this tool; ``tools/tap_capture.py``
is enough. This one exists to sweep the other read commands: it polls a configurable
probe set round-robin and writes every raw answer next to the ``WH_KEYBOARD_LL``
stream, so a later pass can attribute each moving byte to the physical key that
produced it. That is how the three level bytes in the ``0x15`` answer were matched
to Z, X and C.

Response layout used for decoding:

    12 <counter u16> <len> <flags> <cmd> <index> <data ...>
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from hid_capture import devices, identify
from tap_capture import KeyboardHook, Recorder

REPORT_ID_ANALOG = 0x22
FRAME_SIZE = 1023
READ_SIZE_ANALOG = 1024
USAGE_PAGE_ANALOG = 0xFF12
DATA_BYTES = 40

# name -> (request header byte 1, report kind, flags, command)
PROBE_TYPES = {
    "level": (0x3C, 0x05, 0x00, 0x15),
    "info": (0x3A, 0x04, 0x00, 0x14),
    "status": (0x3F, 0x04, 0x0C, 0x19),
}


def build_frame(header, kind, flags, command, index):
    frame = bytearray(FRAME_SIZE)
    frame[0] = 0x12
    frame[1] = header
    frame[2] = 0x12 + flags + index
    frame[3] = kind
    frame[4] = flags
    frame[5] = command
    frame[6] = index
    return bytes([REPORT_ID_ANALOG]) + bytes(frame)


def parse_spec(spec):
    name, _, index_text = spec.partition(":")
    if name not in PROBE_TYPES:
        raise SystemExit(f"Unknown probe type {name!r}; use one of {sorted(PROBE_TYPES)}.")
    header, kind, flags, command = PROBE_TYPES[name]
    index = int(index_text) if index_text else 0
    return {
        "label": f"{name}:{index}",
        "command": command,
        "index": index,
        "frame": build_frame(header, kind, flags, command, index),
    }


def probe_loop(handle, recorder, stop, started, probes):
    timeouts = 0
    mismatches = 0
    sequence = 0
    try:
        while not stop.is_set():
            probe = probes[sequence % len(probes)]
            sequence += 1
            handle.write(probe["frame"])
            raw = bytes(handle.read(READ_SIZE_ANALOG, 20))
            if not raw:
                timeouts += 1
                continue
            payload = raw[1:]
            if (
                len(payload) < 7
                or payload[0] != 0x12
                or payload[5] != probe["command"]
                or payload[6] != probe["index"]
            ):
                mismatches += 1
                continue
            recorder.write(
                {
                    "type": "probe",
                    "host_ns": time.perf_counter_ns() - started,
                    "probe": probe["label"],
                    "echo": payload[6],
                    "data": payload[7 : 7 + DATA_BYTES].hex(),
                }
            )
    except OSError as error:
        recorder.write({"type": "note", "message": f"probe stream stopped: {error!r}"})
    recorder.write({"type": "probe_end", "timeouts": timeouts, "mismatches": mismatches})


def main():
    parser = argparse.ArgumentParser(description="Round-robin device probes plus host key events")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--probe", action="append", default=[], help="e.g. level:0, info:0, status:3")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--out", type=Path, default=Path("probe-capture.jsonl"))
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()

    found = devices()
    if args.list:
        for index, device in enumerate(found):
            print(json.dumps({"index": index, **identify(device)}, ensure_ascii=False))
        return

    analog = [d for d in found if d.get("vendor_id") == 0x8089 and d.get("usage_page") == USAGE_PAGE_ANALOG]
    if not analog:
        raise SystemExit("No analog interface (usage_page 0xFF12) found; run --list.")

    specs = args.probe or ["level:0", "info:0", "info:1", "info:2"]
    probes = [parse_spec(spec) for spec in specs]

    import hid

    started = time.perf_counter_ns()
    stop = threading.Event()
    recorder = Recorder(args.out)
    recorder.write(
        {
            "type": "metadata",
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "timestamp_source": "native_perf_counter_ns",
            "analog": identify(analog[0]),
            "probes": [probe["label"] for probe in probes],
            "keyboard_source": "WH_KEYBOARD_LL",
        }
    )
    analog_handle = hid.device()
    analog_handle.open_path(analog[0]["path"])
    analog_handle.set_nonblocking(False)
    threads = [
        threading.Thread(
            target=probe_loop,
            args=(analog_handle, recorder, stop, started, probes),
            daemon=True,
        )
    ]
    hook = KeyboardHook(recorder, stop, started)
    threads.append(hook)
    for thread in threads:
        thread.start()
    hook.installed.wait(timeout=2.0)
    print(f"{len(threads)} streams -> {args.out}", file=sys.stderr)
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
    elapsed = (time.perf_counter_ns() - started) / 1e9
    recorder.write({"type": "end", "seconds": elapsed, "counts": recorder.counts})
    recorder.close()

    print(json.dumps({"seconds": round(elapsed, 2), **recorder.counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()

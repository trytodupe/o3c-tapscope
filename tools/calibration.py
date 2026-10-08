"""Raw level to millimetre conversion for the O3C magnetic keys.

The position scale is linear at 50 um per raw count, anchored at the top of travel:
raw 0 is 0 mm, raw 79 is 3.95 mm and raw 80 is 4.00 mm (confirmed against the device
readout), so the conversion is simply

    um(level) = 50 * level

``cmd 0x14`` still answers with a per-key 80 byte factory table (offset 24); it is
kept in the calibration file for reference but is not the position scale.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

import json

LEVEL_MAX = 80      # raw counts; raw 79 = 3.95 mm, raw 80 = 4.00 mm
STEP_UM = 50
COMMAND_INFO = 0x14
INFO_KIND = 0x04
TABLE_OFFSET = 24
TABLE_LENGTH = 80

KEY_NAMES = {0: "Z", 1: "X", 2: "C"}


def _looks_like_table(window, following):
    """Recognise the calibration table by shape and by the zero padding after it.

    The fit is noisy by a count or two, so small dips are allowed; a large dip means
    the window straddles the header fields instead.
    """
    if len(window) != TABLE_LENGTH:
        return False
    if not 0 < window[0] <= 40 or window[-1] < 100:
        return False
    drops = [previous - current for previous, current in zip(window, window[1:]) if current < previous]
    if any(drop > 3 for drop in drops) or sum(drops) > 8:
        return False
    return all(byte == 0 for byte in following[:8])


def find_table(payload):
    """Locate the 80 entry calibration table inside a ``cmd 0x14`` payload."""
    for offset in range(8, 49):
        window = list(payload[offset:offset + TABLE_LENGTH])
        following = payload[offset + TABLE_LENGTH:offset + TABLE_LENGTH + 8]
        if _looks_like_table(window, following):
            return offset, window
    return None, None


def parse_info(payload, expected_index=None):
    """Decode a per-key info answer: echoed index, live level and calibration table."""
    if len(payload) < TABLE_OFFSET + TABLE_LENGTH:
        raise ValueError(f"Info frame too short: {len(payload)} bytes")
    index = payload[7]
    if expected_index is not None and index != expected_index:
        raise ValueError(f"Info frame index {index} != requested {expected_index}")
    offset, table = find_table(payload)
    if table is None:
        raise ValueError("No calibration table found in the info frame")
    return {"index": index, "level": payload[8], "table_offset": offset, "table": table}


def level_to_um(level, table=None, step_um=STEP_UM):
    """Travel in micrometres at a raw level.

    ``table`` is accepted so earlier call sites keep working, but the position scale is
    the linear 50 um per count above, not the ``cmd 0x14`` factory table.
    """
    return max(0, min(LEVEL_MAX, int(level))) * step_um


def um_to_level(um, table=None, step_um=STEP_UM):
    """Smallest raw level whose travel reaches ``um`` (the inverse of level_to_um)."""
    return max(0, int(-(-float(um) // step_um)))


def travel_um(table=None, level=LEVEL_MAX, step_um=STEP_UM):
    """Travel the switch reaches at ``level`` raw counts."""
    return level_to_um(level, table, step_um)


def level_lut(step_um=STEP_UM, count=LEVEL_MAX):
    """Millimetres per raw level (index = raw level) for the viewers."""
    return [level * step_um / 1000.0 for level in range(count + 1)]


def read_calibration(indices=(0, 1, 2), timeout_ms=300):
    """Read the calibration straight from the device (read-only, ``cmd 0x14``)."""
    import hid

    from hid_capture import devices
    from level_protocol import READ_SIZE, USAGE_PAGE_ANALOG, build_command_report

    target = next((item for item in devices() if item.get("usage_page") == USAGE_PAGE_ANALOG), None)
    if target is None:
        raise SystemExit("No O3C analog interface found; pass --calibration instead")
    handle = hid.device()
    handle.open_path(target["path"])
    keys = []
    try:
        for index in indices:
            handle.write(build_command_report(COMMAND_INFO, index, kind=INFO_KIND))
            payload = bytes(handle.read(READ_SIZE + 3072, timeout_ms))
            info = parse_info(payload, expected_index=index)
            info["name"] = KEY_NAMES.get(index, str(index))
            keys.append(info)
    finally:
        handle.close()
    return {
        "source": "device",
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "step_um": STEP_UM,
        "keys": keys,
    }


def save_calibration(path, calibration):
    Path(path).write_text(json.dumps(calibration, indent=2), encoding="utf-8")


def load_calibration(path):
    """Read the key names, step and RT bounds. The factory tables are optional."""
    calibration = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in calibration["keys"]:
        key.setdefault("name", KEY_NAMES.get(key.get("index", -1), "?"))
    calibration.setdefault("step_um", STEP_UM)
    calibration.setdefault("source", "file")
    return calibration


def main():
    parser = argparse.ArgumentParser(description="Read the per-key millimetre calibration from the device")
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("calibration.json"))
    parser.add_argument("--indices", default="0,1,2", help="request indices to read")
    args = parser.parse_args()
    indices = tuple(int(part) for part in args.indices.split(",") if part.strip())

    calibration = read_calibration(indices)
    save_calibration(args.out, calibration)
    for key in calibration["keys"]:
        travel = travel_um(step_um=calibration["step_um"]) / 1000.0
        print(f"{key['name']:>3}  index {key['index']}  travel {travel:.2f} mm  "
              f"table at payload offset {key['table_offset']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

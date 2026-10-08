"""Wire format of the O3C analog-key channel (Col03, report id 0x22).

Observed frames:

    request   12 3c <0x12 + flags + index> 05 00 15 <index> 00 ...
    response  12 <counter u16> 12 07 00 15 <index> <lv0> <lv1> <lv2> ...

`lv0`, `lv1` and `lv2` are the live positions of the three magnetic switches and
each covers the same raw 0..79 range. An earlier note read `<lv0> <lv1>` as a
little-endian u16 level and `<lv2>` as a "state"; that was wrong. Holding the
first switch moves only lv0, the second only lv1 and the third only lv2, which a
per-switch sweep of a host-key-annotated capture confirmed. One request therefore
already returns every switch, and no per-index round-robin is needed.

The physical unit is still uncalibrated, so levels stay raw.
"""

REPORT_ID = 0x22
FRAME_SIZE = 1023
READ_SIZE = 1024
COMMAND_LEVEL = 0x15
USAGE_PAGE_ANALOG = 0xFF12
LEVEL_COUNT = 3


def build_level_report(index=0):
    """Build the read-level report, including the leading report id."""
    return build_command_report(COMMAND_LEVEL, index, kind=0x05, header=0x3C)


def build_command_report(command, index=0, kind=0x04, flags=0x00, header=None):
    """Build a read/config report for `command`, including the leading report id.

    Observed header rules: ``[1] = 0x26 + command`` normally, ``[3] = kind``
    (0x04 reads configuration, 0x05 reads live values) and ``[2] = 0x12 + flags +
    index``. The live level command is the recorded exception and passes an explicit
    header, so callers can copy a known-good official frame instead of deriving it.
    """
    if header is None:
        header = 0x26 + command
    frame = bytearray(FRAME_SIZE)
    frame[0] = 0x12
    frame[1] = header
    frame[2] = 0x12 + flags + index
    frame[3] = kind
    frame[4] = flags
    frame[5] = command
    frame[6] = index & 0xFF
    return bytes([REPORT_ID]) + bytes(frame)


def parse_level_frame(raw, expected_index=None):
    """Decode a read-level answer.

    Returns ``{"index": .., "levels": [lv0, lv1, lv2]}`` or ``None`` when the
    device answered with an error frame (first payload byte is not 0x12) or when
    the frame does not belong to this command.
    """
    if len(raw) < 1 + 7 + LEVEL_COUNT or raw[0] != REPORT_ID:
        return None
    payload = raw[1:]
    if payload[0] != 0x12 or payload[2] != 0x12 or payload[5] != COMMAND_LEVEL:
        return None
    if expected_index is not None and payload[6] != expected_index:
        return None
    return {
        "index": payload[6],
        "levels": [payload[7 + offset] for offset in range(LEVEL_COUNT)],
    }

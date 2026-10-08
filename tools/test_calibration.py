import unittest

from calibration import (
    LEVEL_MAX,
    STEP_UM,
    TABLE_LENGTH,
    TABLE_OFFSET,
    find_table,
    level_to_um,
    parse_info,
    travel_um,
    um_to_level,
)

# A monotonic table like the device returns: 30 counts at rest, +1 per 50 um step.
TABLE = [30 + index for index in range(TABLE_LENGTH)]


def payload(index=0, level=37, table=TABLE, offset=TABLE_OFFSET, size=200):
    frame = bytearray(size)
    frame[7] = index
    frame[8] = level
    frame[offset:offset + TABLE_LENGTH] = bytes(table)
    return bytes(frame)


class FindTableTests(unittest.TestCase):
    def test_table_is_found_at_its_offset(self):
        offset, table = find_table(payload())
        self.assertEqual(offset, TABLE_OFFSET)
        self.assertEqual(table, TABLE)

    def test_flat_payload_has_no_table(self):
        self.assertEqual(find_table(bytes(200)), (None, None))

    def test_window_that_dips_is_rejected(self):
        # A real fit is noisy by a count or two; a deep dip means the window covers
        # the wrong bytes, so the search must reject it instead of locking on.
        frame = bytearray(payload())
        frame[TABLE_OFFSET + 10] = 0
        self.assertEqual(find_table(bytes(frame)), (None, None))

    def test_table_without_zero_padding_is_rejected(self):
        frame = bytearray(payload())
        frame[TABLE_OFFSET + TABLE_LENGTH] = 9
        self.assertEqual(find_table(bytes(frame)), (None, None))


class LevelConversionTests(unittest.TestCase):
    def test_linear_scale(self):
        # 50 um per raw count, anchored at the top: raw 79 = 3.95 mm, raw 80 = 4.00 mm.
        self.assertEqual(level_to_um(0), 0)
        self.assertEqual(level_to_um(79), 3950)
        self.assertEqual(level_to_um(80), 4000)
        self.assertEqual(level_to_um(300), 4000)  # clamped to the raw range

    def test_conversion_is_monotonic(self):
        values = [level_to_um(level) for level in range(LEVEL_MAX + 1)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(travel_um(), STEP_UM * LEVEL_MAX)

    def test_um_to_level_inverts_the_conversion(self):
        for level in (1, 41, LEVEL_MAX):
            self.assertEqual(um_to_level(level_to_um(level)), level)

    def test_um_to_level_clamps_at_both_ends(self):
        self.assertEqual(um_to_level(0), 0)
        self.assertEqual(um_to_level(-1), 0)
        self.assertGreater(um_to_level(1e9), LEVEL_MAX)


class ParseInfoTests(unittest.TestCase):
    def test_fields_are_decoded(self):
        info = parse_info(payload(index=2, level=12), expected_index=2)
        self.assertEqual(info["index"], 2)
        self.assertEqual(info["level"], 12)
        self.assertEqual(info["table"], TABLE)

    def test_index_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_info(payload(index=2), expected_index=0)

    def test_short_frame_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_info(bytes(TABLE_OFFSET))

    def test_missing_table_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_info(bytes(200))


if __name__ == "__main__":
    unittest.main()

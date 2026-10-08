import unittest

from level_protocol import REPORT_ID, build_level_report, parse_level_frame


def frame(payload):
    return bytes([REPORT_ID]) + bytes(payload) + bytes(1023 - len(payload))


class BuildLevelReportTests(unittest.TestCase):
    def test_index_zero_header(self):
        self.assertEqual(build_level_report(0)[:8].hex(), "22123c1205001500")

    def test_index_is_encoded_in_byte_two_and_six(self):
        self.assertEqual(build_level_report(2)[:8].hex(), "22123c1405001502")

    def test_report_shape(self):
        report = build_level_report(0)
        self.assertEqual(len(report), 1024)
        self.assertEqual(report[0], REPORT_ID)


class ParseLevelFrameTests(unittest.TestCase):
    def test_three_levels_are_independent(self):
        parsed = parse_level_frame(frame([0x12, 0x40, 0x12, 0x07, 0x00, 0x15, 0x00, 0x17, 0x00, 0x01]))
        self.assertEqual(parsed["levels"], [0x17, 0x00, 0x01])
        self.assertEqual(parsed["index"], 0)

    def test_second_switch_only_moves_lv1(self):
        parsed = parse_level_frame(frame([0x12, 0x41, 0x12, 0x07, 0x00, 0x15, 0x00, 0x00, 0x4F, 0x02]))
        self.assertEqual(parsed["levels"], [0x00, 0x4F, 0x02])

    def test_error_frame_is_rejected(self):
        self.assertIsNone(
            parse_level_frame(frame([0x00, 0x26, 0xF0, 0x04, 0xF0, 0x00, 0x00]))
        )

    def test_other_command_is_rejected(self):
        self.assertIsNone(
            parse_level_frame(frame([0x12, 0x83, 0x6A, 0x3C, 0x00, 0x10, 0x00, 0x01, 0x00, 0x00]))
        )

    def test_expected_index_is_enforced(self):
        raw = frame([0x12, 0x41, 0x12, 0x07, 0x00, 0x15, 0x03, 0x01, 0x00, 0x00])
        self.assertIsNotNone(parse_level_frame(raw, expected_index=3))
        self.assertIsNone(parse_level_frame(raw, expected_index=0))

    def test_truncated_frame_is_rejected(self):
        self.assertIsNone(parse_level_frame(bytes([REPORT_ID]) + bytes(5)))

    def test_empty_read_is_rejected(self):
        self.assertIsNone(parse_level_frame(b""))


if __name__ == "__main__":
    unittest.main()

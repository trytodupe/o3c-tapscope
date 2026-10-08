import json
import tempfile
import unittest
from pathlib import Path

from osu_align import (
    align,
    align_with_anchor,
    double_clicks,
    load_capture,
    make_level_lookup,
    match_count,
)


class MatchCountTests(unittest.TestCase):
    def test_exact_match(self):
        host = [100.0, 300.0, 500.0]
        replay = [1000.0, 1200.0, 1400.0]
        matched, residuals = match_count(host, replay, 900.0, 1.0)
        self.assertEqual(matched, 3)
        self.assertEqual(residuals, [0.0, 0.0, 0.0])

    def test_single_press_is_matched_only_once(self):
        matched, _ = match_count([100.0, 101.0, 102.0], [1000.0], 900.0, 5.0)
        self.assertEqual(matched, 1)

    def test_outside_tolerance_is_rejected(self):
        matched, _ = match_count([100.0], [1000.0], 800.0, 5.0)
        self.assertEqual(matched, 0)


class AlignTests(unittest.TestCase):
    def test_recovers_known_offset(self):
        replay = [500.0 + 137.0 * index for index in range(60)]
        host = [value - 4321.5 for value in replay]
        result = align(host, replay, 6.0)
        self.assertAlmostEqual(result["offset_ms"], 4321.5, places=2)
        self.assertEqual(result["matched"], len(host))
        self.assertLess(result["max_residual_ms"], 0.01)

    def test_jitter_within_tolerance_still_matches(self):
        replay = [500.0 + 91.0 * index for index in range(40)]
        host = [value - 1000.0 + (1.5 if index % 2 else -1.5) for index, value in enumerate(replay)]
        result = align(host, replay, 6.0)
        self.assertEqual(result["matched"], len(host))
        self.assertLess(result["max_residual_ms"], 6.0)

    def test_empty_input_returns_none(self):
        self.assertIsNone(align([], [1.0], 6.0))
        self.assertIsNone(align([1.0], [], 6.0))


HOST = [0.0, 211.0, 407.0, 618.0, 833.0, 1021.0, 1234.0, 1400.0, 1622.0, 1805.0]
TRUE_OFFSET = 10000.0
REPLAY = [value + TRUE_OFFSET for value in HOST] + [4000.0, 8000.0]


class AlignWithAnchorTests(unittest.TestCase):
    def test_without_an_anchor_it_is_the_plain_search(self):
        result, source = align_with_anchor(HOST, REPLAY, 1.0)
        self.assertEqual(source, "unrestricted")
        self.assertEqual(result, align(HOST, REPLAY, 1.0))

    def test_a_good_anchor_wins_a_tie(self):
        result, source = align_with_anchor(HOST, REPLAY, 1.0, TRUE_OFFSET + 2.0, 250.0)
        self.assertEqual(source, "anchor")
        self.assertAlmostEqual(result["offset_ms"], TRUE_OFFSET, places=2)
        self.assertEqual(result["matched"], len(HOST))

    def test_a_bad_anchor_loses_to_a_better_match(self):
        # The window only holds shifted matches, and they match fewer presses, so
        # the anchor must not be allowed to decide.
        result, source = align_with_anchor(HOST, REPLAY, 1.0, TRUE_OFFSET + 400.0, 100.0)
        self.assertTrue(source.startswith("unrestricted"), source)
        self.assertAlmostEqual(result["offset_ms"], TRUE_OFFSET, places=2)
        self.assertEqual(result["matched"], len(HOST))

    def test_an_impossible_anchor_falls_back(self):
        result, source = align_with_anchor(HOST, REPLAY, 1.0, 60000.0, 250.0)
        self.assertTrue(source.startswith("unrestricted"), source)
        self.assertAlmostEqual(result["offset_ms"], TRUE_OFFSET, places=2)

    def test_empty_input_is_still_none(self):
        self.assertEqual(align_with_anchor([], [], 1.0), (None, "unrestricted"))


class LoadCaptureTests(unittest.TestCase):
    def test_state_records_are_ignored(self):
        rows = [
            {"type": "state", "host_ns": 1_000_000, "map_time": 12.0, "playing": True},
            {"type": "levels", "host_ns": 2_000_000, "levels": [1, 2, 3]},
            {"type": "keyboard", "host_ns": 3_000_000, "vk": 90, "down": True, "injected": False},
            {"type": "keyboard", "host_ns": 4_000_000, "vk": 90, "down": True, "injected": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            edges, levels = load_capture(path)
        self.assertEqual(len(edges), 1)
        self.assertEqual(levels, [(2.0, [1, 2, 3])])


class DoubleClickTests(unittest.TestCase):
    def test_release_then_quick_press_is_flagged(self):
        edges = [
            {"host_ms": 1000.0, "vk": 90, "down": True},
            {"host_ms": 1050.0, "vk": 90, "down": False},
            {"host_ms": 1070.0, "vk": 90, "down": True},
            {"host_ms": 1200.0, "vk": 90, "down": False},
        ]
        findings = double_clicks(edges, [90], 60.0)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0]["gap_ms"], 20.0)

    def test_slow_repeat_is_not_flagged(self):
        edges = [
            {"host_ms": 1000.0, "vk": 90, "down": True},
            {"host_ms": 1050.0, "vk": 90, "down": False},
            {"host_ms": 1200.0, "vk": 90, "down": True},
            {"host_ms": 1300.0, "vk": 90, "down": False},
        ]
        self.assertEqual(double_clicks(edges, [90], 60.0), [])

    def test_other_keys_are_ignored(self):
        edges = [
            {"host_ms": 1000.0, "vk": 88, "down": True},
            {"host_ms": 1010.0, "vk": 88, "down": False},
            {"host_ms": 1015.0, "vk": 88, "down": True},
        ]
        self.assertEqual(double_clicks(edges, [90], 60.0), [])


class LevelLookupTests(unittest.TestCase):
    def test_returns_nearest_sample(self):
        levels = [(1.0, [0, 0, 0]), (2.0, [10, 20, 30]), (3.0, [40, 50, 60])]
        lookup = make_level_lookup(levels)
        self.assertEqual(lookup(2.1, 1), 20)
        self.assertEqual(lookup(2.9, 2), 60)
        self.assertEqual(lookup(0.5, 0), 0)


if __name__ == "__main__":
    unittest.main()

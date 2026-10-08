import unittest

from calibration import LEVEL_MAX
from replay_view import (
    build_calibration_payload,
    capture_levels,
    decimate_levels,
    level_lut,
    mod_names,
    parse_mm_list,
    played_until,
    replay_state_samples,
    split_missed,
    stream_edges,
)


def frames(rows):
    """Rows of (time, keys); the viewer only reads the time and the key byte."""
    return [(time, 0, 0, keys) for time, keys in rows]


class StreamEdgeTests(unittest.TestCase):
    def test_each_lane_reacts_to_its_own_bit(self):
        edges, _ = stream_edges(frames([(0, 0), (10, 0x01), (20, 0), (30, 0x02), (40, 0)]))
        self.assertEqual(edges["K1"], [[10.0, True], [20.0, False]])
        self.assertEqual(edges["K2"], [[30.0, True], [40.0, False]])

    def test_mouse_and_keyboard_bits_are_the_same_lane(self):
        # osu! stable sets both bits for one physical press and the mouse bit alone
        # when the player clicks, so a lane is the union.
        edges, _ = stream_edges(frames([(0, 0x04), (10, 0x01 | 0x04), (20, 0x02 | 0x08), (30, 0)]))
        self.assertEqual(edges["K1"], [[0.0, True], [20.0, False]])
        self.assertEqual(edges["K2"], [[20.0, True], [30.0, False]])

    def test_edges_are_not_emitted_without_a_change(self):
        edges, _ = stream_edges(frames([(0, 0x01), (10, 0x01), (20, 0x01)]))
        self.assertEqual(edges["K1"], [[0.0, True]])

    def test_interval_is_the_median_gap(self):
        _, interval = stream_edges(frames([(0, 0), (16, 0), (32, 0), (48, 0), (400, 0)]))
        self.assertEqual(interval, 16.0)

    def test_empty_input_is_safe(self):
        edges, interval = stream_edges([])
        self.assertEqual(edges["K1"], [])
        self.assertEqual(interval, 0.0)


class ReplayStateSampleTests(unittest.TestCase):
    def test_state_holds_until_the_next_edge(self):
        edges = {"K1": [[10.0, True], [30.0, False]], "K2": [[20.0, True]]}
        samples = replay_state_samples(edges, 40)
        self.assertEqual(samples[0], [10.0, 1.0, 0.0])
        self.assertEqual(samples[1], [20.0, 1.0, 1.0])
        self.assertEqual(samples[2], [30.0, 0.0, 1.0])
        self.assertEqual(samples[-1], [40.0, 0.0, 1.0])


class DecimateTests(unittest.TestCase):
    def test_short_input_is_returned_unchanged(self):
        samples = [[0, 0, 0], [1, 1, 1]]
        self.assertIs(decimate_levels(samples, 100), samples)

    def test_spikes_survive(self):
        samples = [[index, 0, 0] for index in range(4000)]
        samples[1500][1] = 79
        kept = decimate_levels(samples, 400)
        self.assertLess(len(kept), len(samples))
        self.assertIn(79, [sample[1] for sample in kept])
        self.assertEqual(kept[0], samples[0])
        self.assertEqual(kept[-1], samples[-1])

    def test_timestamps_stay_sorted(self):
        samples = [[index, index % 7, index % 5] for index in range(5000)]
        kept = decimate_levels(samples, 500)
        self.assertEqual([sample[0] for sample in kept], sorted(sample[0] for sample in kept))


class LevelLutTests(unittest.TestCase):
    def test_lut_is_monotonic_and_millimetres(self):
        lut = level_lut()
        self.assertEqual(len(lut), LEVEL_MAX + 1)
        self.assertEqual(lut[0], 0.0)
        self.assertEqual(lut[79], 3.95)
        self.assertEqual(lut[80], 4.0)
        self.assertEqual(lut, sorted(lut))


class CalibrationPayloadTests(unittest.TestCase):
    def test_names_and_travel_are_carried_over(self):
        calibration = {
            "source": "device",
            "step_um": 50,
            "keys": [
                {"index": 0, "name": "Z", "table": [30 + index for index in range(80)]},
                {"index": 1, "name": "X", "table": [20 + index for index in range(80)]},
            ],
        }
        payload = build_calibration_payload(calibration)
        self.assertEqual([key["name"] for key in payload["keys"]], ["Z", "X"])
        # The linear scale gives every key the same 0..4.00 mm range.
        self.assertEqual(payload["keys"][0]["travel_mm"], 4.0)
        self.assertEqual(payload["keys"][1]["travel_mm"], 4.0)

    def test_rt_bounds_are_per_key(self):
        calibration = {
            "source": "settings",
            "step_um": 50,
            "keys": [
                {"index": 0, "name": "Z", "rt_range_mm": [1.0, 3.6]},
                {"index": 1, "name": "X"},
            ],
        }
        payload = build_calibration_payload(calibration)
        self.assertEqual(payload["keys"][0]["rt_range_mm"], [1.0, 3.6])
        self.assertEqual(payload["keys"][1]["rt_range_mm"], [None, None])

    def test_broadcast_rt_fills_keys_without_their_own(self):
        calibration = {"step_um": 50,
                       "keys": [{"index": 0, "name": "Z"}, {"index": 1, "name": "X"}]}
        payload = build_calibration_payload(calibration, [0.5, 2.5])
        self.assertEqual(payload["keys"][0]["rt_range_mm"], [0.5, 2.5])


class ParseMmListTests(unittest.TestCase):
    def test_missing_value_is_none_per_lane(self):
        self.assertEqual(parse_mm_list(None, 2), [None, None])
        self.assertEqual(parse_mm_list("", 2), [None, None])

    def test_single_value_broadcasts(self):
        self.assertEqual(parse_mm_list("1.8", 2), [1.8, 1.8])

    def test_values_map_per_lane(self):
        self.assertEqual(parse_mm_list("1.8,2.4", 2), [1.8, 2.4])

    def test_last_value_repeats_for_extra_lanes(self):
        self.assertEqual(parse_mm_list("1.8", 3), [1.8, 1.8, 1.8])
        self.assertEqual(parse_mm_list("1.8,2.4", 3), [1.8, 2.4, 2.4])

    def test_semicolons_and_spaces_are_accepted(self):
        self.assertEqual(parse_mm_list("1.8 ; 2.4", 2), [1.8, 2.4])


class ModNameTests(unittest.TestCase):
    def test_no_mods(self):
        self.assertEqual(mod_names(0), "NM")

    def test_known_mods(self):
        self.assertEqual(mod_names(8 | 64), "HDDT")


class IncompleteReplayTests(unittest.TestCase):
    """A replay exported after a fail holds frames only up to that moment."""

    def test_frames_end_at_the_last_recorded_frame(self):
        self.assertEqual(played_until(frames([(0, 0), (1500, 0x01)])), 1500.0)
        self.assertEqual(played_until([]), 0.0)

    def test_a_cleared_play_has_nothing_unplayed(self):
        # Real capture: frames run to 144110 ms while the last note is at 141295 ms.
        missed = [{"time": 141295.0}, {"time": 140100.0}]
        missed, unplayed = split_missed(missed, 144110.0, 120.0)
        self.assertEqual([note["time"] for note in missed], [141295.0, 140100.0])
        self.assertEqual(unplayed, [])

    def test_notes_past_the_fail_are_unplayed_not_missed(self):
        missed = [{"time": 1000.0}, {"time": 5000.0}, {"time": 9000.0}]
        missed, unplayed = split_missed(missed, 1000.0, 120.0)
        self.assertEqual([note["time"] for note in missed], [1000.0])
        self.assertEqual([note["time"] for note in unplayed], [5000.0, 9000.0])

    def test_the_boundary_note_is_still_reachable(self):
        # A press at the last frame can claim a note one judgement window later.
        missed, unplayed = split_missed([{"time": 1120.0}], 1000.0, 120.0)
        self.assertEqual(len(missed), 1)
        self.assertEqual(unplayed, [])

    def test_an_empty_frame_stream_is_not_a_fail(self):
        missed, unplayed = split_missed([{"time": 1.0}], 0.0, 120.0)
        self.assertEqual(len(missed), 1)
        self.assertEqual(unplayed, [])


class CaptureLevelTests(unittest.TestCase):
    def test_only_real_key_down_events_count_as_presses(self):
        import json
        import tempfile
        from pathlib import Path

        rows = [
            {"type": "keyboard", "host_ns": 5_000_000, "down": True, "injected": False},
            {"type": "keyboard", "host_ns": 9_000_000, "down": True, "injected": True},
            {"type": "keyboard", "host_ns": 7_000_000, "down": False, "injected": False},
            {"type": "levels", "host_ns": 6_000_000, "levels": [3, 4, 5]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            presses, samples = capture_levels(path)
        self.assertEqual(presses, [5.0])
        self.assertEqual(samples, [[6.0, 3, 4, 5]])


if __name__ == "__main__":
    unittest.main()

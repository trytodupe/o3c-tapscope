import unittest

from plot_depth import find_episodes, parse_thresholds


class ParseThresholdTests(unittest.TestCase):
    def test_values_map_to_switch_index(self):
        self.assertEqual(parse_thresholds("6,4,8", 3), [6, 4, 8])

    def test_last_value_repeats_for_extra_switches(self):
        self.assertEqual(parse_thresholds("6,4", 4), [6, 4, 4, 4])

    def test_single_value_applies_to_every_switch(self):
        self.assertEqual(parse_thresholds(" 5 ", 2), [5, 5])

    def test_blank_input_is_rejected(self):
        with self.assertRaises(SystemExit):
            parse_thresholds(" , ", 3)


class PerSwitchEpisodeTests(unittest.TestCase):
    def test_resting_c_key_only_activates_at_its_own_threshold(self):
        # C idles at 1..4 while Z idles at 0..1, so a shared threshold of 4 reports a
        # phantom activation for the whole idle stretch of the third switch.
        idle = [(float(index), 4, 0) for index in range(10)]
        self.assertEqual(len(find_episodes(idle, 4)), 1)
        self.assertEqual(len(find_episodes(idle, 8)), 0)


if __name__ == "__main__":
    unittest.main()

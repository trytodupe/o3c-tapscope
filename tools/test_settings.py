import tempfile
import unittest
from pathlib import Path

from settings import calibration_data, coerce, defaults, load, save, subdir


class CoerceTests(unittest.TestCase):
    def test_blank_rt_bounds_stay_unset(self):
        settings = coerce({"keys": [{"name": "Z", "rt_low": "", "rt_high": None}]})
        self.assertIsNone(settings["keys"][0]["rt_low"])
        self.assertIsNone(settings["keys"][0]["rt_high"])

    def test_numbers_are_parsed_and_an_out_of_range_port_falls_back(self):
        settings = coerce({
            "port": "99999",
            "window_min": "2.5",
            "keys": [{"name": "Z", "rt_low": "1.2", "rt_high": "3.6"}],
        })
        self.assertEqual(settings["port"], 8770)
        self.assertEqual(settings["window_min"], 2.5)
        self.assertEqual(settings["keys"][0]["rt_low"], 1.2)

    def test_missing_keys_fall_back_to_defaults(self):
        settings = coerce({})
        self.assertEqual([key["name"] for key in settings["keys"]], ["Z", "X", "C"])


class PersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            saved = save(path, {"osu_root": "D:/osu!",
                                "keys": [{"name": "A", "rt_low": 1, "rt_high": 2}]})
            self.assertEqual(saved["osu_root"], "D:/osu!")
            self.assertEqual(load(path)["keys"][0]["name"], "A")

    def test_a_missing_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load(Path(directory) / "absent.json")
            self.assertEqual(settings["keys"][0]["name"], "Z")


class DerivationTests(unittest.TestCase):
    def test_paths_are_derived_from_the_osu_root(self):
        settings = defaults()
        settings["osu_root"] = r"D:\osu!"
        self.assertEqual(str(subdir(settings, "Songs")), r"D:\osu!\Songs")
        self.assertEqual(str(subdir(settings, "Replays")), r"D:\osu!\Replays")

    def test_no_root_means_no_subdir(self):
        self.assertIsNone(subdir(defaults(), "Songs"))

    def test_calibration_carries_per_key_rt(self):
        settings = defaults()
        settings["keys"] = [
            {"name": "Z", "rt_low": 1.0, "rt_high": 3.6},
            {"name": "X", "rt_low": None, "rt_high": 2.0},
            {"name": "C", "rt_low": None, "rt_high": None},
        ]
        calibration = calibration_data(settings)
        self.assertEqual(calibration["keys"][0]["rt_range_mm"], [1.0, 3.6])
        self.assertEqual(calibration["keys"][1]["rt_range_mm"], [None, 2.0])


if __name__ == "__main__":
    unittest.main()

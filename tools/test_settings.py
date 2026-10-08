import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from settings import (
    calibration_data,
    coerce,
    defaults,
    detect_lazer_root,
    lazer_subdir,
    load,
    save,
    subdir,
)


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

    def test_lazer_root_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save(path, {"lazer_root": "D:/osulazer"})
            self.assertEqual(load(path)["lazer_root"], "D:/osulazer")

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

    def test_lazer_paths_are_derived_from_the_lazer_root(self):
        settings = defaults()
        settings["lazer_root"] = r"D:\osulazer"
        self.assertEqual(str(lazer_subdir(settings, "exports")), r"D:\osulazer\exports")
        self.assertEqual(str(lazer_subdir(settings, "files")), r"D:\osulazer\files")

    def test_no_lazer_root_means_no_subdir(self):
        self.assertIsNone(lazer_subdir(defaults(), "files"))


class LazerDetectionTests(unittest.TestCase):
    r"""A machine with only lazer still has its default data under %APPDATA%\osu."""

    def test_the_default_lazer_data_folder_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            (appdata / "osu").mkdir()
            (appdata / "osu" / "client.realm").write_bytes(b"")
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                self.assertEqual(detect_lazer_root(), str(appdata / "osu"))

    def test_a_folder_without_lazer_files_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            (appdata / "osu").mkdir()
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                self.assertEqual(detect_lazer_root(), "")

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

import hashlib
import tempfile
import unittest
from pathlib import Path

from beatmap import (
    file_hash,
    filename_terms,
    find_beatmap,
    hit_windows,
    judgement,
    pair_presses,
    parse_beatmap,
)

BEATMAP = """osu file format v14

[General]
AudioFilename: audio.mp3
Mode: 0

[Metadata]
Title:Hanabie Ressha
TitleUnicode:\u82b1\u51b7\u5217\u8eca
Artist:Sangatsu no Phantasia
ArtistUnicode:\u4e09\u6708\u306e\u30d1\u30f3\u30bf\u30b8\u30fc
Creator:Houshou Hari
Version:Hatsune Miku's Insane

[Difficulty]
CircleSize:4
OverallDifficulty:8
ApproachRate:9

[TimingPoints]
1000,437.956204379562,4,2,1,60,1,0

[HitObjects]
64,192,1500,1,0,0:0:0:0:
256,192,1938,1,0,0:0:0:0:
100,100,2376,2,0,B|200:200,1,70
256,192,5000,12,0,0:0:0:0:
"""


class FindBeatmapTests(unittest.TestCase):
    def test_a_full_scan_is_the_fallback_when_hints_match_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "1234 Artist - Title"
            folder.mkdir()
            chart = folder / "map.osu"
            chart.write_text(BEATMAP, encoding="utf-8")
            found = find_beatmap(file_hash(chart), root, terms=["nothing-matches-here"])
        self.assertEqual(found, chart)

    def test_punctuation_differences_do_not_hide_the_folder(self):
        # The replay says "vs." while the song folder says "vs"; matching whole phrases
        # used to score 0 for every folder and fall through to a capped full scan.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decoy = root / "0000 Aaa - Nothing Here"
            decoy.mkdir()
            (decoy / "map.osu").write_text(BEATMAP, encoding="utf-8")
            folder = root / "1889111 -45 - yoshikawa45 vs siesta45 Battle of HongKong"
            folder.mkdir()
            chart = folder / "-45 - yoshikawa45 vs. siesta45 Battle of HongKong (Dada) [Para Bellum].osu"
            chart.write_text(BEATMAP, encoding="utf-8")
            terms = filename_terms(
                "[SHK]trytodupe - -45 - yoshikawa45 vs. siesta45 Battle of HongKong "
                "[Para Bellum] (2026-10-08) Osu.osr"
            )
            found = find_beatmap(file_hash(chart), root, terms=terms, max_folders=1)
        self.assertEqual(found, chart)


class ReplayNameTests(unittest.TestCase):
    def test_terms_keep_artist_and_title(self):
        terms = filename_terms(
            "[Player] - Sangatsu no Phantasia - Hanabie Ressha "
            "[Hatsune Miku's Insane] (2026-10-08) Osu.osr"
        )
        self.assertIn("sangatsu no phantasia", terms)
        self.assertIn("hanabie ressha", terms)
        self.assertNotIn("hatsune miku's insane", terms)

    def test_short_fragments_are_dropped(self):
        self.assertEqual(filename_terms("[AB]x - Hey [Hard] (2020-01-01) Osu.osr"), [])


class ParseBeatmapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "map.osu"
        self.path.write_text(BEATMAP, encoding="utf-8")
        self.beatmap = parse_beatmap(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_metadata_prefers_the_unicode_fields(self):
        self.assertEqual(self.beatmap["artist"], "\u4e09\u6708\u306e\u30d1\u30f3\u30bf\u30b8\u30fc")
        self.assertEqual(self.beatmap["title"], "\u82b1\u51b7\u5217\u8eca")
        self.assertEqual(self.beatmap["version"], "Hatsune Miku's Insane")

    def test_difficulty_and_bpm(self):
        self.assertEqual(self.beatmap["od"], 8)
        self.assertEqual(self.beatmap["ar"], 9)
        self.assertAlmostEqual(self.beatmap["bpm"], 137.0, places=1)

    def test_objects_are_typed_and_sorted(self):
        kinds = [note["kind"] for note in self.beatmap["notes"]]
        self.assertEqual(kinds, ["circle", "circle", "slider", "spinner"])
        self.assertEqual(self.beatmap["length_ms"], 5000)

    def test_hash_matches_the_raw_bytes(self):
        expected = hashlib.md5(self.path.read_bytes()).hexdigest()
        self.assertEqual(file_hash(self.path), expected)
        self.assertEqual(len(expected), 32)


class HitWindowTests(unittest.TestCase):
    def test_od8_windows(self):
        windows = hit_windows(8)
        self.assertEqual(windows["300"], 32.0)
        self.assertEqual(windows["100"], 76.0)
        self.assertEqual(windows["50"], 120.0)

    def test_od_is_clamped(self):
        self.assertEqual(hit_windows(-5), hit_windows(0))
        self.assertEqual(hit_windows(99), hit_windows(10))

    def test_judgement_boundaries(self):
        windows = hit_windows(8)
        self.assertEqual(judgement(32.0, windows), "300")
        self.assertEqual(judgement(32.1, windows), "100")
        self.assertEqual(judgement(120.0, windows), "50")
        self.assertIsNone(judgement(120.1, windows))


class PairPressTests(unittest.TestCase):
    def setUp(self):
        self.windows = hit_windows(8)
        self.notes = [{"time": 1000}, {"time": 2000}]

    def test_close_presses_pair_with_the_nearest_note(self):
        pairs, stray, missed = pair_presses(
            self.notes, {"K1": [1010.0], "K2": [2050.0]}, self.windows
        )
        self.assertEqual([pair["note"] for pair in pairs], [1000, 2000])
        self.assertEqual([pair["stream"] for pair in pairs], ["K1", "K2"])
        # 10 ms early is a 300 on OD8, 50 ms late is outside the 32 ms window.
        self.assertEqual([pair["judgement"] for pair in pairs], ["300", "100"])
        self.assertEqual([pair["delta"] for pair in pairs], [10.0, 50.0])
        self.assertEqual(stray, [])
        self.assertEqual(missed, [])

    def test_one_note_claims_a_single_press(self):
        pairs, stray, _ = pair_presses(self.notes, {"K1": [1000.0, 1010.0]}, self.windows)
        self.assertEqual(len(pairs), 1)
        self.assertEqual([entry["time"] for entry in stray], [1010.0])

    def test_press_outside_the_widest_window_is_stray(self):
        pairs, stray, missed = pair_presses(
            self.notes, {"K1": [1300.0], "K2": [2050.0]}, self.windows
        )
        self.assertEqual([pair["note"] for pair in pairs], [2000])
        self.assertEqual([entry["time"] for entry in stray], [1300.0])
        self.assertEqual([note["time"] for note in missed], [1000])

    def test_double_tap_lands_on_two_notes(self):
        notes = [{"time": 1000}, {"time": 1040}]
        pairs, stray, missed = pair_presses(
            notes, {"K1": [1002.0, 1042.0]}, self.windows
        )
        self.assertEqual(sorted(pair["note"] for pair in pairs), [1000, 1040])
        self.assertEqual(stray, [])
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()

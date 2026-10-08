import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio import (
    MAX_WINDOW_MIN,
    PlayRuns,
    ReplayStore,
    ReplayWatcher,
    Studio,
    StudioRecorder,
    stage_command,
    window_seconds,
)


class RecorderTests(unittest.TestCase):
    def test_live_snapshot_updates_even_when_not_armed(self):
        recorder = StudioRecorder()
        recorder.write({"type": "levels", "levels": [1, 2, 3]})
        self.assertEqual(recorder.snapshot()["levels"], [1, 2, 3])
        self.assertEqual(recorder.snapshot()["count"], 1)
        self.assertFalse(recorder.is_armed())

    def test_a_recovery_note_clears_the_error(self):
        recorder = StudioRecorder()
        recorder.write({"type": "note", "message": "stalled"})
        self.assertEqual(recorder.snapshot()["error"], "stalled")
        recorder.write({"type": "note", "message": "", "recovered": True})
        self.assertEqual(recorder.snapshot()["error"], "")

    def test_arming_writes_and_disarming_stops(self):
        recorder = StudioRecorder()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            recorder.write({"type": "levels", "levels": [9, 9, 9]})  # dropped
            recorder.open(path)
            self.assertTrue(recorder.is_armed())
            recorder.write({"type": "levels", "levels": [4, 5, 6]})
            recorder.write({"type": "keyboard", "vk": 90, "down": True})
            recorder.close()
            self.assertFalse(recorder.is_armed())
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["levels"], [4, 5, 6])
        self.assertEqual(recorder.counts["levels"], 1)
        self.assertEqual(recorder.counts["keyboard"], 1)


class WindowSecondsTests(unittest.TestCase):
    def test_the_page_value_is_clamped_and_zero_disables_the_window(self):
        self.assertIsNone(window_seconds(None))
        self.assertIsNone(window_seconds(""))
        self.assertIsNone(window_seconds("abc"))
        self.assertEqual(window_seconds("0"), 0.0)
        self.assertEqual(window_seconds("10"), 600.0)
        self.assertEqual(window_seconds("600"), MAX_WINDOW_MIN * 60.0)


class PlayRunTests(unittest.TestCase):
    class FakeRecorder:
        def __init__(self):
            self.pins = []
            self.unpins = 0

        def pin(self, lo_ns):
            self.pins.append(lo_ns)

        def unpin(self):
            self.unpins += 1

    def test_a_finished_play_is_pinned_and_the_next_run_releases_it(self):
        recorder = self.FakeRecorder()
        runs = PlayRuns(recorder, 50.0)
        runs.update({"playing": False}, 1_000_000_000)
        self.assertEqual(recorder.pins, [])
        runs.update({"playing": True}, 2_000_000_000)
        self.assertEqual(recorder.unpins, 1)
        self.assertEqual(recorder.pins, [])
        runs.update({"playing": True}, 3_000_000_000)
        self.assertEqual(recorder.unpins, 1)
        runs.update({"playing": False}, 4_000_000_000)
        # The run start is back-dated by one poll period so the first sample is inside.
        self.assertEqual(recorder.pins, [1_950_000_000])


class WatcherTests(unittest.TestCase):
    def test_existing_files_are_ignored_after_priming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "old.osr").write_bytes(b"old")
            watcher = ReplayWatcher(root)
            watcher.prime()
            self.assertEqual(watcher.scan_once(), [])

    def test_a_new_file_needs_two_identical_sightings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = ReplayWatcher(root)
            watcher.prime()
            (root / "new.osr").write_bytes(b"new")
            self.assertEqual(watcher.scan_once(), [])
            ready = watcher.scan_once()
            self.assertEqual([path.name for path in ready], ["new.osr"])
            self.assertEqual(watcher.scan_once(), [])

    def test_a_growing_file_waits_for_the_size_to_settle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = ReplayWatcher(root)
            watcher.prime()
            path = root / "grow.osr"
            path.write_bytes(b"a")
            self.assertEqual(watcher.scan_once(), [])
            path.write_bytes(b"ab")
            self.assertEqual(watcher.scan_once(), [])
            self.assertEqual([item.name for item in watcher.scan_once()], ["grow.osr"])

    def test_several_folders_can_be_watched_at_once(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            stable, lazer = Path(first), Path(second)
            watcher = ReplayWatcher([stable, lazer])
            watcher.prime()
            (lazer / "new.osr").write_bytes(b"x")
            self.assertEqual(watcher.scan_once(), [])
            ready = watcher.scan_once()
        self.assertEqual([path.name for path in ready], ["new.osr"])


class StudioTests(unittest.TestCase):
    def _studio(self, render):
        directory = tempfile.mkdtemp()
        # addCleanup is LIFO: the recorder is closed before the folder is removed, so
        # Windows can delete the still-open session file.
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        studio = Studio(StudioRecorder(), Path(directory), render, {"vendor_id": 1},
                        "http://127.0.0.1:1", 50.0)
        self.addCleanup(studio.disarm)
        return studio

    def test_arm_writes_metadata_and_disarm_writes_the_end(self):
        studio = self._studio(lambda *_: None)
        studio.arm()
        self.assertTrue(studio.recorder.is_armed())
        capture = Path(studio.status()["capture_path"])
        self.assertTrue(capture.is_file())
        studio.disarm()
        self.assertFalse(studio.recorder.is_armed())
        text = capture.read_text(encoding="utf-8")
        self.assertIn('"type": "metadata"', text)
        self.assertIn('"type": "end"', text)

    def test_arm_records_the_window_the_page_asked_for(self):
        studio = self._studio(lambda *_: None)
        studio.arm(window_s=120.0)
        self.assertEqual(studio.status()["window_s"], 120.0)
        capture = Path(studio.status()["capture_path"])
        studio.disarm()
        rows = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["window_s"], 120.0)
        self.assertEqual(rows[-1]["window_s"], 120.0)

    def test_a_replay_runs_the_renderer_and_is_listed(self):
        calls = []

        def render(osr, capture, slug):
            calls.append((osr, capture, slug))
            return {"id": slug, "name": osr.name}

        studio = self._studio(render)
        studio.arm()
        osr = studio.captures_dir / "play.osr"
        osr.write_bytes(b"")
        studio.handle_replay(osr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], osr)
        self.assertEqual(calls[0][1], Path(studio.status()["capture_path"]))
        page = studio.status()["pages"][0]
        self.assertEqual(page["name"], "play.osr")
        self.assertIn("id=" + calls[0][2], page["url"])

    def test_a_replay_without_a_capture_is_reported(self):
        studio = self._studio(lambda *_: None)
        studio.handle_replay(studio.captures_dir / "play.osr")
        self.assertIn("no capture", studio.status()["watch"]["status"])

    def test_a_render_failure_does_not_raise(self):
        def render(*_):
            raise RuntimeError("boom")

        studio = self._studio(render)
        studio.arm()
        osr = studio.captures_dir / "play.osr"
        osr.write_bytes(b"")
        studio.handle_replay(osr)
        self.assertIn("boom", studio.status()["watch"]["status"])
        self.assertEqual(studio.status()["pages"], [])

    def test_manual_view_aligns_a_picked_replay_and_capture(self):
        calls = []

        def render(osr, capture, slug):
            calls.append((Path(osr).name, Path(capture).name))
            return {"id": slug, "name": Path(osr).name}

        studio = self._studio(render)
        studio.watch_dir = str(studio.captures_dir)
        (studio.captures_dir / "a.osr").write_bytes(b"")
        (studio.captures_dir / "tap.jsonl").write_text("", encoding="utf-8")
        url = studio.view("a.osr", "tap.jsonl")
        self.assertEqual(calls, [("a.osr", "tap.jsonl")])
        self.assertIn("id=", url)
        self.assertEqual(studio.status()["pages"][0]["name"], "a.osr")

    def test_manual_view_rejects_a_path_outside_the_folder(self):
        studio = self._studio(lambda *_: None)
        studio.watch_dir = str(studio.captures_dir)
        with self.assertRaises(ValueError):
            studio.view("../secret.osr", "tap.jsonl")

    def test_sources_list_replays_and_captures_newest_first(self):
        studio = self._studio(lambda *_: None)
        studio.watch_dir = str(studio.captures_dir)
        (studio.captures_dir / "a.osr").write_bytes(b"")
        (studio.captures_dir / "tap.jsonl").write_text("", encoding="utf-8")
        sources = studio.sources()
        self.assertEqual([item["name"] for item in sources["replays"]], ["a.osr"])
        self.assertEqual([item["name"] for item in sources["captures"]], ["tap.jsonl"])

    def test_sources_tag_which_folder_a_replay_came_from(self):
        studio = self._studio(lambda *_: None)
        studio.watch_dir = str(studio.captures_dir)
        lazer = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, lazer, ignore_errors=True)
        studio.lazer_exports = lazer
        (studio.captures_dir / "a.osr").write_bytes(b"")
        (lazer / "b.osr").write_bytes(b"")
        tagged = {item["name"]: item["source"] for item in studio.sources()["replays"]}
        self.assertEqual(tagged, {"a.osr": "stable", "b.osr": "lazer"})

    def test_the_displayed_watch_dir_falls_back_to_lazer(self):
        studio = self._studio(lambda *_: None)
        lazer = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, lazer, ignore_errors=True)
        studio.watch_dir = str(Path(tempfile.mkdtemp()) / "no-replays")
        studio.lazer_exports = lazer
        self.assertEqual(studio.status()["watch"]["dir"], str(lazer))

    def test_manual_view_reaches_the_lazer_exports_folder(self):
        calls = []

        def render(osr, capture, slug):
            calls.append(Path(osr).name)
            return {"id": slug, "name": Path(osr).name}

        studio = self._studio(render)
        lazer = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, lazer, ignore_errors=True)
        studio.lazer_exports = lazer
        (lazer / "b.osr").write_bytes(b"")
        (studio.captures_dir / "tap.jsonl").write_text("", encoding="utf-8")
        url = studio.view("b.osr", "tap.jsonl", "lazer")
        self.assertEqual(calls, ["b.osr"])
        self.assertIn("id=", url)

    def test_manual_view_accepts_an_absolute_path_from_the_file_dialog(self):
        calls = []

        def render(osr, capture, slug):
            calls.append(Path(osr).name)
            return {"id": slug, "name": Path(osr).name}

        studio = self._studio(render)
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        osr = outside / "picked.osr"
        osr.write_bytes(b"")
        (studio.captures_dir / "tap.jsonl").write_text("", encoding="utf-8")
        url = studio.view(str(osr), "tap.jsonl")
        self.assertEqual(calls, ["picked.osr"])
        self.assertIn("id=", url)

    def test_manual_view_rejects_an_absolute_path_that_is_not_a_replay(self):
        studio = self._studio(lambda *_: None)
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        other = outside / "notes.txt"
        other.write_text("", encoding="utf-8")
        (studio.captures_dir / "tap.jsonl").write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            studio.view(str(other), "tap.jsonl")


class StageCommandTests(unittest.TestCase):
    """A machine with only osu!lazer has no Songs folder; staging must still work."""

    def test_a_lazer_only_setup_reaches_the_renderer(self):
        captured = {}

        def fake_stage(spec, out):
            captured["spec"] = spec
            return {"id": out.name, "name": spec.replay.name}

        with mock.patch("studio.stage_replay", fake_stage):
            stage_command(None, Path("D:/lazer/files"), Path("out"), {},
                          Path("a.osr"), None, "slug")
        self.assertIsNone(captured["spec"].songs)
        self.assertEqual(captured["spec"].lazer_files, Path("D:/lazer/files"))

    def test_neither_root_is_a_clear_error(self):
        with self.assertRaises(RuntimeError):
            stage_command(None, None, Path("out"), {}, Path("a.osr"), None, "slug")


class ReplayStoreTests(unittest.TestCase):
    def test_entries_survive_a_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replays.json"
            store = ReplayStore(path)
            store.add({"id": "a", "name": "a.osr"})
            store.add({"id": "b", "name": "b.osr"})
            self.assertEqual([item["id"] for item in ReplayStore(path).list()], ["b", "a"])

    def test_adding_an_id_replaces_the_old_entry(self):
        store = ReplayStore(None)
        store.add({"id": "a", "name": "old"})
        store.add({"id": "a", "name": "new"})
        self.assertEqual(len(store.list()), 1)
        self.assertEqual(store.list()[0]["name"], "new")


if __name__ == "__main__":
    unittest.main()

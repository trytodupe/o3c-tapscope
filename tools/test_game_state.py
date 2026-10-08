import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from game_state import (
    StatePoller,
    coarse_offset,
    extract_state,
    last_play_run,
    load_state_samples,
    monotone_runs,
    play_runs,
    probe,
)


def v2_payload(map_time=10500, state=2, name="play"):
    return {
        "game": {"focused": True, "paused": False},
        "state": {"number": state, "name": name},
        "beatmap": {
            "time": {"live": map_time, "firstObject": 1500, "lastObject": 200000},
            "checksum": "E5A3CEFDC824AB2835E57F1CD16FA1E1",
            "artistUnicode": "\u4e09\u6708\u306e\u30d1\u30f3\u30bf\u30b8\u30fc",
            "title": "Hanabie Ressha",
            "version": "Hatsune Miku's Insane",
        },
        "directPath": {"beatmapFile": r"D:\osu!\Songs\map.osu"},
    }


def v1_payload(map_time=10500, state=2, name="play"):
    return {
        "menu": {"state": {"number": state, "name": name}},
        "gameplay": {"time": {"live": map_time, "firstObject": 1500}},
    }


class ExtractStateTests(unittest.TestCase):
    def test_v2_schema(self):
        state = extract_state(v2_payload())
        self.assertEqual(state["map_time"], 10500.0)
        self.assertTrue(state["playing"])
        self.assertEqual(state["checksum"], "e5a3cefdc824ab2835e57f1cd16fa1e1")
        self.assertEqual(state["version"], "Hatsune Miku's Insane")
        self.assertTrue(state["beatmap_file"].endswith("map.osu"))

    def test_v1_schema_is_accepted(self):
        state = extract_state(v1_payload())
        self.assertEqual(state["map_time"], 10500.0)
        self.assertTrue(state["playing"])

    def test_state_number_wins_over_the_name(self):
        state = extract_state(v2_payload(state=0, name="playing"))
        self.assertFalse(state["playing"])

    def test_name_is_used_when_there_is_no_number(self):
        payload = v2_payload()
        payload["state"] = {"name": "play"}
        self.assertTrue(extract_state(payload)["playing"])

    def test_missing_time_is_not_an_error(self):
        payload = v2_payload()
        payload["beatmap"]["time"] = {"live": None}
        state = extract_state(payload)
        self.assertIsNone(state["map_time"])
        self.assertTrue(state["playing"])

    def test_non_object_is_rejected(self):
        self.assertIsNone(extract_state(["nope"]))
        self.assertIsNone(extract_state(None))


class FakeTosu:
    """A stand-in for the state reader, so the HTTP path is really exercised."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                body = outer.answers.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTosu({"/json/v2": v2_payload(), "/json": v1_payload()})
        self.addCleanup(self.fake.close)

    def test_probe_prefers_v2(self):
        answer = probe(self.fake.url)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["schema"], "v2")
        self.assertEqual(answer["map_time"], 10500.0)

    def test_probe_is_not_ok_when_nothing_answers(self):
        answer = probe("http://127.0.0.1:1", timeout=0.5)
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["errors"])


class V1OnlyTests(unittest.TestCase):
    def test_probe_falls_back_to_the_v1_path(self):
        fake = FakeTosu({"/json": v1_payload()})
        self.addCleanup(fake.close)
        answer = probe(fake.url)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["schema"], "v1")
        self.assertEqual(fake.requests, ["/json/v2", "/json"])


class PollerTests(unittest.TestCase):
    def test_poller_writes_stamped_samples(self):
        fake = FakeTosu({"/json/v2": v2_payload()})
        self.addCleanup(fake.close)
        seen = []
        stop = threading.Event()
        poller = StatePoller(seen.append, url=fake.url, poll_ms=10.0, stop=stop)
        poller.start()
        self.assertTrue(poller.ready.wait(timeout=5.0), "poller never answered")
        stop.set()
        poller.join(timeout=5.0)
        self.assertGreaterEqual(poller.samples, 1)
        self.assertEqual(poller.errors, 0)
        self.assertEqual(seen[0]["map_time"], 10500.0)
        self.assertGreater(seen[0]["host_ms"], 0.0)
        self.assertEqual(seen[0]["schema"], "v2")

    def test_v1_server_is_detected(self):
        fake = FakeTosu({"/json": v1_payload()})
        self.addCleanup(fake.close)
        seen = []
        stop = threading.Event()
        poller = StatePoller(seen.append, url=fake.url, poll_ms=10.0, stop=stop)
        poller.start()
        self.assertTrue(poller.ready.wait(timeout=5.0))
        stop.set()
        poller.join(timeout=5.0)
        self.assertEqual(seen[0]["schema"], "v1")


def simulate(true_offset, host_start=1_000_000.0, seconds=60.0, poll_ms=50.0, reader_ms=150.0):
    """Samples the way a poller sees them: the reader refreshes on its own loop."""
    reads = []
    instant = host_start
    while instant < host_start + seconds * 1000.0:
        reads.append(instant)
        instant += reader_ms
    pairs = []
    host = host_start
    index = 0
    while host < host_start + seconds * 1000.0:
        while index + 1 < len(reads) and reads[index + 1] <= host:
            index += 1
        pairs.append((host, reads[index] + true_offset))
        host += poll_ms
    return pairs


class CoarseOffsetTests(unittest.TestCase):
    def test_uniform_staleness_is_recovered(self):
        pairs = simulate(-12345.0)
        summary = coarse_offset(pairs)
        self.assertLess(abs(summary["offset_ms"] - (-12345.0)), 15.0)
        self.assertLessEqual(summary["stale_ms"], 51.0)

    def test_polling_slower_than_the_reader_still_works(self):
        pairs = simulate(4200.0, poll_ms=200.0)
        summary = coarse_offset(pairs)
        self.assertLess(abs(summary["offset_ms"] - 4200.0), 60.0)

    def test_a_restart_splits_the_run(self):
        restart = [(100.0 + index * 200.0, 90000.0 + index * 200.0) for index in range(10)]
        played = [(5000.0 + index * 200.0, 100.0 + index * 200.0) for index in range(60)]
        summary = coarse_offset(restart + played)
        self.assertAlmostEqual(summary["offset_ms"], -4900.0, places=6)
        self.assertEqual(summary["run"], 60)

    def test_one_fast_sample_does_not_drag_the_anchor(self):
        pairs = [(1000.0 + index * 200.0, 1000.0 + index * 200.0 - 5000.0) for index in range(100)]
        pairs[7] = (pairs[7][0], pairs[7][0] - 4900.0)
        summary = coarse_offset(pairs)
        self.assertAlmostEqual(summary["offset_ms"], -5000.0, places=6)

    def test_clock_rate_is_reported(self):
        pairs = [(index * 100.0, index * 100.0 - 1000.0) for index in range(50)]
        self.assertAlmostEqual(coarse_offset(pairs)["rate"], 1.0, places=6)

    def test_too_few_samples(self):
        self.assertIsNone(coarse_offset([]))
        self.assertIsNone(coarse_offset([(0.0, 0.0), (10.0, 10.0)]))
        self.assertEqual(monotone_runs([(0.0, 5.0), (1.0, 4.0)]), [[(0.0, 5.0)], [(1.0, 4.0)]])


class PlayRunTests(unittest.TestCase):
    def _play(self, start, count=20):
        return [(start + index * 200.0, 100.0 + index * 200.0) for index in range(count)]

    def test_last_run_is_the_most_recent_play(self):
        run = last_play_run(self._play(1000.0) + self._play(20000.0))
        self.assertEqual(run[0][0], 20000.0)
        self.assertEqual(len(run), 20)

    def test_short_runs_are_ignored(self):
        tiny = [(0.0, 5000.0), (10.0, 5010.0)]
        run = last_play_run(tiny + self._play(1000.0, 10))
        self.assertEqual(len(run), 10)

    def test_runs_split_on_a_map_time_reset(self):
        self.assertEqual(len(play_runs(self._play(0.0) + self._play(9000.0))), 2)

    def test_empty_input(self):
        self.assertEqual(last_play_run([]), [])
        self.assertEqual(play_runs([]), [])


class LoadStateSamplesTests(unittest.TestCase):
    def test_only_playing_records_are_kept(self):
        rows = [
            {"type": "metadata", "schema_version": 1},
            {"type": "state", "host_ns": 1_000_000_000, "map_time": 500.0, "playing": True,
             "checksum": "ABC"},
            {"type": "state", "host_ns": 2_000_000_000, "map_time": 0.0, "playing": False,
             "checksum": ""},
            {"type": "keyboard", "host_ns": 3_000_000_000, "vk": 90},
            {"type": "state", "host_ns": 4_000_000_000, "map_time": None, "playing": True,
             "checksum": ""},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            samples, checksum = load_state_samples(path)
        self.assertEqual(samples, [(1000.0, 500.0)])
        self.assertEqual(checksum, "abc")


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ndjson_writer import NdjsonWriter, host_ns_of

SECOND = 1_000_000_000


def read(path):
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def level(step, seconds=None):
    return {
        "type": "levels",
        "host_ns": (step * SECOND if seconds is None else seconds),
        "levels": [step, 0, 0],
    }


class HostNsTests(unittest.TestCase):
    def test_only_stamped_lines_carry_a_stamp(self):
        self.assertEqual(host_ns_of('{"type": "levels", "host_ns": 12, "levels": []}\n'), 12)
        self.assertIsNone(host_ns_of('{"type": "metadata", "schema_version": 1}\n'))


class BatchingTests(unittest.TestCase):
    def test_records_are_not_flushed_one_by_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            # A minute-long flush interval keeps the count threshold in charge.
            writer = NdjsonWriter(path, flush_ms=60000.0)
            for step in range(40):
                writer.write(level(step))
            writer.close()
            self.assertEqual(len(read(path)), 40)
            self.assertEqual(writer.written, 40)
            # 256 records per flush by default, so 40 records are a single batch; the
            # bound only has to be far below the per-record behaviour.
            self.assertGreaterEqual(writer.flushes, 1)
            self.assertLessEqual(writer.flushes, 2)


class WindowTests(unittest.TestCase):
    def _writer(self, path, **kwargs):
        options = {"flush_ms": 60000.0, "compact_bytes": 1 << 30}
        options.update(kwargs)
        return NdjsonWriter(path, **options)

    def test_the_window_drops_the_head_and_keeps_the_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = self._writer(path, window_s=10.0)
            writer.write({"type": "metadata", "schema_version": 1})
            for step in range(41):
                writer.write(level(step))
            writer.close()
            rows = read(path)
            self.assertEqual(rows[0]["type"], "metadata")
            stamps = [row["host_ns"] for row in rows[1:]]
            self.assertEqual(max(stamps), 40 * SECOND)
            self.assertGreaterEqual(min(stamps), 30 * SECOND)
            self.assertGreater(writer.dropped_window, 0)

    def test_notes_survive_a_compaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = self._writer(path, window_s=10.0)
            writer.write({"type": "metadata", "schema_version": 1})
            writer.write({"type": "note", "message": "stalled"})
            for step in range(41):
                writer.write(level(step))
            writer.close()
            messages = [row.get("message") for row in read(path)]
            self.assertIn("stalled", messages)

    def test_a_capture_without_a_header_is_still_trimmed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = self._writer(path, window_s=10.0)
            for step in range(41):
                writer.write(level(step))
            writer.close()
            stamps = [row["host_ns"] for row in read(path)]
            self.assertEqual(min(stamps), 30 * SECOND)

    def test_a_pin_holds_a_finished_play_past_the_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = self._writer(path, window_s=10.0, seal_s=60.0)
            writer.write({"type": "metadata", "schema_version": 1})
            for step in range(5):
                writer.write(level(step))
            writer.pin(1 * SECOND)
            for step in range(5, 41):
                writer.write(level(step))
            writer.close()
            stamps = [row["host_ns"] for row in read(path)[1:]]
            self.assertGreaterEqual(min(stamps), 1 * SECOND)

    def test_an_expired_pin_stops_holding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = self._writer(path, window_s=10.0, seal_s=0.0)
            writer.write({"type": "metadata", "schema_version": 1})
            writer.pin(1 * SECOND)
            for step in range(41):
                writer.write(level(step))
            writer.close()
            stamps = [row["host_ns"] for row in read(path)[1:]]
            self.assertGreaterEqual(min(stamps), 30 * SECOND)

    def test_compact_reports_the_trim_before_close(self):
        # The caller writes its `end` record before close(), so the totals it reports
        # have to be up to date at that point - the final compaction must not be the
        # one that does the dropping.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = self._writer(path, window_s=10.0)
            writer.write({"type": "metadata", "schema_version": 1})
            for step in range(41):
                writer.write(level(step))
            writer.compact()
            self.assertGreaterEqual(writer.dropped_window, 30)
            stamps = [row["host_ns"] for row in read(path)[1:]]
            self.assertEqual(min(stamps), 30 * SECOND)
            writer.close()
            self.assertEqual(writer.compaction_errors, 0)

    def test_no_window_keeps_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = self._writer(path)
            writer.write({"type": "metadata", "schema_version": 1})
            for step in range(41):
                writer.write(level(step))
            writer.close()
            self.assertEqual(len(read(path)), 42)
            self.assertEqual(writer.compactions, 0)


class CompactionErrorTests(unittest.TestCase):
    def test_a_failed_compaction_is_not_fatal(self):
        # Windows refuses os.replace while an alignment is reading the capture; the
        # writer has to survive that and keep the untrimmed file.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = NdjsonWriter(path, window_s=10.0, flush_ms=60000.0)
            writer.write({"type": "metadata", "schema_version": 1})
            for step in range(41):
                writer.write(level(step))
            with mock.patch("ndjson_writer.os.replace", side_effect=OSError("sharing violation")):
                writer.close()
            self.assertEqual(writer.compaction_errors, 1)
            self.assertTrue(writer.error.startswith("compaction skipped"))
            rows = read(path)
            self.assertEqual(len(rows), 42)
            self.assertEqual(min(row["host_ns"] for row in rows[1:]), 0)


class OverflowTests(unittest.TestCase):
    def test_overflow_is_counted_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tap.jsonl"
            writer = NdjsonWriter(path, queue_size=1, flush_ms=60000.0)
            for step in range(200):
                writer.write(level(step))
            writer.close()
            self.assertGreater(writer.dropped_overflow, 0)
            messages = [row.get("message", "") for row in read(path)]
            self.assertTrue(any("overflowed" in message for message in messages), messages)


if __name__ == "__main__":
    unittest.main()

import csv
import lzma
import struct
import tempfile
import unittest
from pathlib import Path

from analyze import fit_anchors, replay_frames


class AnalysisTests(unittest.TestCase):
    def test_alignment_with_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "anchors.csv"
            with path.open("w", newline="") as target:
                writer = csv.writer(target)
                writer.writerows([("host_ms", "replay_ms"), (0, 50), (1000, 1051), (2000, 2052)])
            scale, offset, residuals = fit_anchors(path)
            self.assertAlmostEqual(scale, 1.001)
            self.assertAlmostEqual(offset, 50)
            self.assertTrue(all(abs(value) < 1e-9 for value in residuals))

    def test_frames_and_seed(self):
        compressed = lzma.compress(b"0|0|0|0,10|1|2|5,5|1|2|0,-12345|0|0|42,")
        header = b"\x00" + struct.pack("<i", 20200101) + b"\x00" * 3
        header += b"\x00" * (12 + 4 + 2 + 1 + 4 + 1 + 8)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "play.osr"
            path.write_bytes(header + struct.pack("<i", len(compressed)) + compressed)
            metadata, frames = replay_frames(path)
            self.assertEqual(metadata["mode"], 0)
            self.assertEqual([frame[0] for frame in frames], [0, 10, 15])
            self.assertEqual(frames[1][3], 5)

    def test_truncated_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.osr"
            path.write_bytes(b"\x00")
            with self.assertRaises(ValueError):
                replay_frames(path)


if __name__ == "__main__":
    unittest.main()

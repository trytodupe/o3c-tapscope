import unittest

from hid_capture import identify


class HidCaptureTests(unittest.TestCase):
    def test_identify_decodes_binary_path(self):
        result = identify({"path": b"path", "vendor_id": 1, "product_id": 2})
        self.assertEqual(result["path"], "path")
        self.assertEqual(result["vendor_id"], 1)


if __name__ == "__main__":
    unittest.main()

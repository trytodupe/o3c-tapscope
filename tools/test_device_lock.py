import sys
import unittest

from device_lock import DeviceBusy, DeviceLock

NAME = "Local\\rapid_trigger_test_device_lock"


@unittest.skipUnless(sys.platform == "win32", "named mutexes are Windows-only")
class DeviceLockTests(unittest.TestCase):
    def test_a_second_owner_is_refused_until_the_first_releases(self):
        first = DeviceLock(NAME)
        try:
            with self.assertRaises(DeviceBusy):
                DeviceLock(NAME)
        finally:
            first.close()
        again = DeviceLock(NAME)   # the first handle is gone, so this must succeed
        again.close()


if __name__ == "__main__":
    unittest.main()

"""Ensure only one process reads the O3C analog channel at a time.

Windows lets several processes open the same HID path, and hidapi does so happily. Two
pollers then split the device's input reports, so each one's ``read`` keeps returning
another reader's answer, the reply header never matches, and the poll loop silently
retries forever without producing samples. A named mutex makes the second owner fail
fast instead.
"""

import ctypes
import sys

ERROR_ALREADY_EXISTS = 183

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
else:  # the device only exists on Windows; keep the module importable elsewhere
    _kernel32 = None


class DeviceBusy(RuntimeError):
    pass


class DeviceLock:
    def __init__(self, name="Local\\rapid_trigger_o3c_analog"):
        self.handle = None
        if _kernel32 is None:
            return
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise DeviceBusy("CreateMutexW failed")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            raise DeviceBusy("another O3C reader is already running")
        self.handle = handle

    def close(self):
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None

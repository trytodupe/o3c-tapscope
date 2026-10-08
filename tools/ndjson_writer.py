"""Batched NDJSON writer with a rolling window and a pin for a finished play.

The capture producers - the analog poll loop, the low-level keyboard hook and the
state poller - must never touch the filesystem: a flush that hits an antivirus
filter driver takes milliseconds, and for the keyboard hook that means a dropped
hook and jittered host timestamps. So a producer only hands the record to a queue
and a single writer thread owns the file, flushes in batches and compacts the file
down to the configured window.

The window keeps the most recent ``window_s`` seconds of records, measured on the
``host_ns`` field every stream shares, so an unattended session cannot grow without
bound. ``pin()`` extends retention for a play that just ended, which is what makes a
late replay export still alignable.

Records without ``host_ns`` - the metadata header, notes and the end record - are
always kept. Every stream stamps its records on one clock as it produces them, so
the file is in near-monotonic order and compaction only has to find one cut point.
"""

import json
import os
import queue
import re
import threading
import time
from pathlib import Path

DEFAULT_QUEUE_SIZE = 1 << 16
DEFAULT_FLUSH_RECORDS = 256
DEFAULT_FLUSH_MS = 100.0
DEFAULT_COMPACT_S = 60.0
DEFAULT_COMPACT_BYTES = 8 << 20

HOST_NS = re.compile(r'"host_ns":\s*(\d+)')


class _CompactionRequest:
    """Marker queued to make the writer thread trim the file and report back."""

    __slots__ = ("event",)

    def __init__(self):
        self.event = threading.Event()


def host_ns_of(line):
    """Stamp of one record line, or None for a header or note line."""
    match = HOST_NS.search(line)
    return int(match.group(1)) if match else None


class NdjsonWriter:
    """Own one NDJSON file: one writer thread, batched flushes, rolling window."""

    def __init__(
        self,
        path,
        window_s=0.0,
        seal_s=None,
        queue_size=DEFAULT_QUEUE_SIZE,
        flush_records=DEFAULT_FLUSH_RECORDS,
        flush_ms=DEFAULT_FLUSH_MS,
        compact_s=DEFAULT_COMPACT_S,
        compact_bytes=DEFAULT_COMPACT_BYTES,
    ):
        self.path = Path(path)
        self.window_ns = int(max(0.0, float(window_s)) * 1e9)
        # How long a pin may hold data past the window. One window by default, which
        # bounds the file at window + (window + longest play).
        self.seal_ns = int(max(0.0, float(window_s if seal_s is None else seal_s)) * 1e9)
        self.flush_records = flush_records
        self.flush_ms = flush_ms
        self.compact_s = compact_s
        self.compact_bytes = compact_bytes

        self.counts = {}
        self.written = 0
        self.flushes = 0
        self.compactions = 0
        self.compaction_errors = 0
        self.dropped_window = 0
        self.dropped_overflow = 0
        self.error = ""

        self._queue = queue.Queue(maxsize=queue_size)
        self._counts_lock = threading.Lock()
        self._pin_lock = threading.Lock()
        self._pin_lo_ns = None
        self._pin_deadline = 0.0
        self._latest_ns = None
        self._pending = 0
        self._written_bytes = 0
        self._noted_overflow = 0
        self._closing = False
        now = time.monotonic()
        self._flushed_at = now
        self._compacted_at = now

        self._target = self.path.open("w", encoding="utf-8", newline="")
        self._thread = threading.Thread(target=self._run, name="ndjson-writer", daemon=True)
        self._thread.start()

    def write(self, record):
        """Queue one record. Never blocks; a full queue drops and is counted.

        The record must not be mutated afterwards: it is serialized on the writer
        thread so that a producer callback stays as short as possible.
        """
        kind = record.get("type", "")
        with self._counts_lock:
            self.counts[kind] = self.counts.get(kind, 0) + 1
        stamp = record.get("host_ns")
        if isinstance(stamp, int):
            self._latest_ns = stamp
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self.dropped_overflow += 1

    def pin(self, lo_ns):
        """Retain everything from ``lo_ns`` on past the window, for ``seal_ns``."""
        with self._pin_lock:
            self._pin_lo_ns = int(lo_ns)
            self._pin_deadline = time.monotonic() + self.seal_ns / 1e9

    def compact(self, timeout=30.0):
        """Trim the file to the window now and wait for it to finish.

        ``close()`` trims too, but that happens after the caller has already written
        the ``end`` record, so the totals in that record would miss the last trim.
        Calling this first makes ``dropped_window`` mean what it says.
        """
        if not self.window_ns:
            return
        request = _CompactionRequest()
        while self._thread.is_alive():
            try:
                self._queue.put(request, timeout=0.5)
                break
            except queue.Full:
                continue
        request.event.wait(timeout)

    def unpin(self):
        with self._pin_lock:
            self._pin_lo_ns = None

    def stats(self):
        return {
            "window_s": self.window_ns / 1e9,
            "written": self.written,
            "flushes": self.flushes,
            "compactions": self.compactions,
            "compaction_errors": self.compaction_errors,
            "dropped_window": self.dropped_window,
            "dropped_overflow": self.dropped_overflow,
        }

    def close(self):
        self._closing = True
        while self._thread.is_alive():
            try:
                self._queue.put(None, timeout=0.5)
                break
            except queue.Full:
                continue
        self._thread.join(timeout=60.0)

    def _floor_ns(self):
        """Oldest stamp worth keeping, or None when the window is disabled."""
        if not self.window_ns or self._latest_ns is None:
            return None
        floor = self._latest_ns - self.window_ns
        with self._pin_lock:
            pin, deadline = self._pin_lo_ns, self._pin_deadline
        if pin is not None and time.monotonic() < deadline:
            floor = min(floor, pin)
        return floor

    def _run(self):
        while True:
            self._report_overflow()
            try:
                record = self._queue.get(timeout=self.flush_ms / 1000.0)
            except queue.Empty:
                self._flush()
                self._maybe_compact()
                if self._closing and self._queue.empty():
                    break
                continue
            if record is None:
                break
            if isinstance(record, _CompactionRequest):
                self._compact(self._floor_ns())
                record.event.set()
                continue
            self._emit(record)
            self._pending += 1
            if (
                self._pending >= self.flush_records
                or (time.monotonic() - self._flushed_at) * 1000.0 >= self.flush_ms
            ):
                self._flush()
            self._maybe_compact()
        self._report_overflow()
        self._flush()
        floor_ns = self._floor_ns()
        if floor_ns is None:
            # No window: close without a rewrite, leaving the file as written.
            self._target.flush()
            self._target.close()
        else:
            self._compact(floor_ns, reopen=False)

    def _emit(self, record):
        line = json.dumps(record, ensure_ascii=False) + "\n"
        self._target.write(line)
        self._written_bytes += len(line)
        self.written += 1

    def _flush(self):
        if self._pending:
            self._target.flush()
            self.flushes += 1
            self._pending = 0
        self._flushed_at = time.monotonic()

    def _report_overflow(self):
        dropped = self.dropped_overflow
        if dropped == self._noted_overflow:
            return
        self._noted_overflow = dropped
        self._emit({
            "type": "note",
            "message": f"writer queue overflowed: {dropped} records dropped (the disk is not keeping up)",
        })

    def _maybe_compact(self):
        if not self.window_ns:
            return
        if (
            time.monotonic() - self._compacted_at < self.compact_s
            and self._written_bytes < self.compact_bytes
        ):
            return
        self._compact(self._floor_ns())

    def _compact(self, floor_ns, reopen=True):
        """Rewrite the file without the records older than ``floor_ns``.

        Runs on the writer thread, so it is the only writer of the file and the
        rewrite cannot race one. The temporary file plus ``os.replace`` keeps readers
        (and a crash) from ever seeing a half-written capture. A failure - most likely
        a sharing violation because an alignment is reading the capture right now - is
        never fatal: the layer is reopened as it was and the next interval retries.
        """
        self._compacted_at = time.monotonic()
        self._written_bytes = 0
        if floor_ns is None:
            return
        self._flush()
        self._target.close()
        temporary = self.path.with_name(self.path.name + ".compact")
        dropped = 0
        try:
            with self.path.open(encoding="utf-8") as source, temporary.open(
                "w", encoding="utf-8", newline=""
            ) as out:
                first = True
                for line in source:
                    stamp = host_ns_of(line)
                    if first:
                        first = False
                        if stamp is None:
                            # The header carries the session identity and cannot be
                            # measured against the window, so it is always kept.
                            out.write(line)
                            continue
                    if stamp is not None and stamp < floor_ns:
                        dropped += 1
                        continue
                    out.write(line)
                    # Everything after the first kept line is inside the window, so the
                    # tail is copied verbatim instead of being parsed line by line.
                    out.write(source.read())
                    break
            os.replace(temporary, self.path)
        except OSError as error:
            self.compaction_errors += 1
            self.error = f"compaction skipped: {error!r}"
            try:
                temporary.unlink()
            except OSError:
                pass
            if reopen:
                self._target = self.path.open("a", encoding="utf-8", newline="")
            return
        self.compactions += 1
        self.dropped_window += dropped
        if reopen:
            self._target = self.path.open("a", encoding="utf-8", newline="")

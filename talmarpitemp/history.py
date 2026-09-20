"""Efficient reads of the CSV history.

Rows are expected in chronological order. Readers tolerate moderate disorder
(for example after a clock change) by looking one extra block past the window.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .stats import WEEK, ChartData, HistorySummary, summarize_history, wall_seconds
from .storage import Reading, parse_row
from .temperature import MAX_PLAUSIBLE_C, MIN_PLAUSIBLE_C

BLOCK_SIZE = 256 * 1024
MAX_LINE_BYTES = 64 * 1024
FORWARD_SCAN_TOLERANCE = 5000
CACHE_MAX_AGE = timedelta(minutes=10)
FAST_PATH_MIN_ROWS = 50_000
APPROX_LINE_BYTES = 26
MIN_BLOCK_BYTES = 4096

_DATA_ROW = re.compile(rb"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,(-?\d{1,3}(?:\.\d+)?)\r?$", re.MULTILINE)
_STAMP = re.compile(rb"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),", re.MULTILINE)

Signature = Tuple[int, int]


def file_signature(path: Path) -> Signature:
    """Size and modification time, or (-1, 0) when the file is missing."""
    try:
        info = os.stat(path)
    except OSError:
        return (-1, 0)
    return (info.st_size, info.st_mtime_ns)


def _blocks_backwards(handle, size: int) -> Iterator[List[str]]:
    """Yield lists of whole lines, from the end of the file toward the start."""
    position = size
    carry = b""
    while position > 0:
        step = min(BLOCK_SIZE, position)
        position -= step
        handle.seek(position)
        data = handle.read(step) + carry
        if position > 0:
            head, newline, body = data.partition(b"\n")
            if not newline:
                carry = data if len(data) <= MAX_LINE_BYTES else b""
                continue
            carry, data = head, body
        else:
            carry = b""
        yield data.decode("utf-8", "replace").splitlines()


def read_since(path: Path, cutoff: datetime, until: Optional[datetime] = None) -> List[Reading]:
    """Readings with cutoff <= timestamp <= until, in file order.

    Scans from the end of the file and stops after a block holding only older rows.
    """
    kept: List[List[Reading]] = []
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            for lines in _blocks_backwards(handle, size):
                rows = [row for row in map(parse_row, lines) if row is not None]
                kept.append([row for row in rows if row[0] >= cutoff
                             and (until is None or row[0] <= until)])
                if rows and all(row[0] < cutoff for row in rows):
                    break
    except FileNotFoundError:
        return []
    return [row for block in reversed(kept) for row in block]


def read_last(path: Path) -> Optional[Reading]:
    """The most recently appended valid row."""
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            for lines in _blocks_backwards(handle, size):
                for line in reversed(lines):
                    row = parse_row(line)
                    if row is not None:
                        return row
    except FileNotFoundError:
        return None
    return None


def read_first(path: Path) -> Optional[Reading]:
    """The earliest valid row in file order."""
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                row = parse_row(raw.decode("utf-8", "replace"))
                if row is not None:
                    return row
    except FileNotFoundError:
        return None
    return None


def _first_row_at_or_after(handle, offset: int) -> Optional[Reading]:
    handle.seek(offset)
    if offset > 0:
        handle.readline()
    while True:
        raw = handle.readline()
        if not raw:
            return None
        row = parse_row(raw.decode("utf-8", "replace"))
        if row is not None:
            return row


def _find_start_offset(handle, size: int, start: datetime) -> int:
    """Binary search for an offset from which a forward scan cannot miss start."""
    low, high = 0, size
    while high - low > BLOCK_SIZE:
        middle = (low + high) // 2
        row = _first_row_at_or_after(handle, middle)
        if row is None or row[0] >= start:
            high = middle
        else:
            low = middle
    return max(0, low - BLOCK_SIZE)


def iter_range(path: Path, start: datetime, end: datetime) -> Iterator[Reading]:
    """Stream readings with start <= timestamp <= end without loading the file."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return
    with handle:
        size = handle.seek(0, os.SEEK_END)
        offset = _find_start_offset(handle, size, start)
        handle.seek(offset)
        if offset > 0:
            handle.readline()
        rows_past_end = 0
        for raw in handle:
            row = parse_row(raw.decode("utf-8", "replace"))
            if row is None:
                continue
            if row[0] > end:
                rows_past_end += 1
                if rows_past_end >= FORWARD_SCAN_TOLERANCE:
                    return
                continue
            rows_past_end = 0
            if row[0] >= start:
                yield row


def _offset_of_first_at_or_after(handle, size: int, moment: datetime) -> int:
    """Byte offset of the first row at or after moment, or the file size."""
    offset = _find_start_offset(handle, size, moment)
    handle.seek(offset)
    if offset > 0:
        handle.readline()
    while True:
        position = handle.tell()
        raw = handle.readline()
        if not raw:
            return size
        row = parse_row(raw.decode("utf-8", "replace"))
        if row is not None and row[0] >= moment:
            return position


def _stamp_seconds(raw: bytes) -> Optional[float]:
    try:
        return wall_seconds(datetime.fromisoformat(raw.decode("ascii")))
    except ValueError:
        return None


def aggregate_range(path: Path, start: datetime, end: datetime, max_points: int) -> Optional[ChartData]:
    """Chart data for a very large range, reading the file in big blocks.

    Every bucket holds a block of consecutive rows with an exact minimum, mean and
    maximum. Returns None when the range is small enough for the row by row path.
    """
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return None
    with handle:
        size = handle.seek(0, os.SEEK_END)
        low = _offset_of_first_at_or_after(handle, size, start)
        high = _offset_of_first_at_or_after(handle, size, end + timedelta(seconds=1))
        if (high - low) // APPROX_LINE_BYTES < FAST_PATH_MIN_ROWS:
            return None
        block = max((high - low) // max(max_points, 1), MIN_BLOCK_BYTES)
        data = ChartData()
        handle.seek(low)
        position = low
        while position < high:
            want = min(block, high - position)
            chunk = handle.read(want)
            if position + want < high and not chunk.endswith(b"\n"):
                chunk += handle.readline()
            position = handle.tell()
            _add_block(data, chunk)
    if data.points:
        data.bucket_seconds = max((data.points[-1][0] - data.points[0][0]) / len(data.points), 1.0)
    return data


def _add_block(data: ChartData, chunk: bytes) -> None:
    values = list(map(float, _DATA_ROW.findall(chunk)))
    first = _STAMP.search(chunk)
    if not values or first is None:
        return
    low, high = min(values), max(values)
    if low < MIN_PLAUSIBLE_C or high > MAX_PLAUSIBLE_C:
        values = [v for v in values if MIN_PLAUSIBLE_C <= v <= MAX_PLAUSIBLE_C]
        if not values:
            return
        low, high = min(values), max(values)
    last = _STAMP.match(chunk, chunk.rfind(b"\n", 0, len(chunk) - 1) + 1) or first
    t_first, t_last = _stamp_seconds(first.group(1)), _stamp_seconds(last.group(1))
    if t_first is None or t_last is None:
        return
    count = len(values)
    steps = max(count - 1, 1)
    if data.minimum is None or low < data.minimum[1]:
        data.minimum = (t_first + (t_last - t_first) * values.index(low) / steps, low)
    if data.maximum is None or high > data.maximum[1]:
        data.maximum = (t_first + (t_last - t_first) * values.index(high) / steps, high)
    data.count += count
    data.points.append(((t_first + t_last) / 2, sum(values) / count, low, high, count))


class WeekCache:
    """Keeps the last 7 days in memory and reloads only when the file changes.

    refresh() does the file I/O. summary() and rows() never touch the disk, so
    the live display can call them without waiting on a reload.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reload_guard = threading.Lock()
        self._path: Optional[Path] = None
        self._signature: Optional[Signature] = None
        self._loaded_at: Optional[datetime] = None
        self._rows: List[Reading] = []
        self._summary: Optional[HistorySummary] = None
        self._summary_key = None

    def _needs_reload(self, path: Path, now: datetime) -> bool:
        return (path != self._path or self._loaded_at is None
                or abs(now - self._loaded_at) > CACHE_MAX_AGE
                or file_signature(path) != self._signature)

    def refresh(self, path: Path, now: datetime) -> None:
        with self._lock:
            if not self._needs_reload(path, now):
                return
        if not self._reload_guard.acquire(blocking=False):
            return
        try:
            signature = file_signature(path)
            rows = read_since(path, now - WEEK, now)
            with self._lock:
                self._path, self._signature, self._loaded_at = path, signature, now
                self._rows, self._summary_key = rows, None
        finally:
            self._reload_guard.release()

    def note_append(self, path: Path, moment: datetime, celsius: float, appended: int) -> None:
        """Add a row this process just wrote, unless the file also changed elsewhere."""
        with self._lock:
            if path != self._path or self._signature is None:
                return
            current = file_signature(path)
            if current[0] == self._signature[0] + appended:
                self._rows.append((moment, celsius))
                self._signature = current
            else:
                self._signature = None
            self._summary_key = None

    def rows_covering(self, path: Path, start: datetime) -> Optional[List[Reading]]:
        """Cached rows, or None when the cache cannot answer a query starting at start."""
        with self._lock:
            if path != self._path or self._loaded_at is None or start < self._loaded_at - WEEK:
                return None
            return list(self._rows)

    def summary(self, path: Path, now: datetime) -> Optional[HistorySummary]:
        with self._lock:
            if path != self._path:
                return None
            key = (self._signature, now.replace(second=0, microsecond=0))
            if self._summary_key != key:
                self._summary = summarize_history(self._rows, now)
                self._summary_key = key
            return self._summary

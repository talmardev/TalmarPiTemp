"""Window statistics and chart downsampling. Values stay in Celsius."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from .storage import Reading

WEEK = timedelta(days=7)
DAY = timedelta(days=1)
WALL_EPOCH = datetime(1970, 1, 1)

# time, average, minimum, maximum, readings in the bucket
ChartPoint = Tuple[float, float, float, float, int]


def wall_seconds(moment: datetime) -> float:
    """Seconds since 1970 on the wall clock, ignoring time zones."""
    return (moment - WALL_EPOCH).total_seconds()


@dataclass(frozen=True)
class HistorySummary:
    count_24h: int
    count_7d: int
    maximum: Optional[float]
    minimum: Optional[float]
    average: Optional[float]


def in_window(readings: Iterable[Reading], start: datetime, end: datetime) -> List[Reading]:
    return [row for row in readings if start <= row[0] <= end]


def summarize_history(readings: Iterable[Reading], now: datetime) -> HistorySummary:
    """Count and aggregate readings from the previous 7 days, up to now."""
    week_start = now - WEEK
    day_start = now - DAY
    count_7d = count_24h = 0
    total = 0.0
    maximum: Optional[float] = None
    minimum: Optional[float] = None
    for moment, celsius in readings:
        if not week_start <= moment <= now:
            continue
        count_7d += 1
        if moment >= day_start:
            count_24h += 1
        total += celsius
        if maximum is None or celsius > maximum:
            maximum = celsius
        if minimum is None or celsius < minimum:
            minimum = celsius
    average = total / count_7d if count_7d else None
    return HistorySummary(count_24h, count_7d, maximum, minimum, average)


@dataclass
class ChartData:
    points: List[ChartPoint] = field(default_factory=list)
    count: int = 0
    minimum: Optional[Tuple[float, float]] = None
    maximum: Optional[Tuple[float, float]] = None
    bucket_seconds: float = 1.0


def _downsample_sorted(rows: List[Reading], origin: float, width: float,
                       data: ChartData) -> bool:
    """Fast path for time ordered lists using C level sum, min and max per bucket."""
    times = [(row[0] - WALL_EPOCH).total_seconds() for row in rows]
    if any(a > b for a, b in zip(times, times[1:])):
        return False
    values = [row[1] for row in rows]
    total = len(times)
    data.count = total
    if not total:
        return True
    low, high = min(values), max(values)
    data.minimum = (times[values.index(low)], low)
    data.maximum = (times[values.index(high)], high)
    first = 0
    while first < total:
        index = int((times[first] - origin) // width)
        last = max(bisect_left(times, origin + (index + 1) * width, first), first + 1)
        chunk = values[first:last]
        count = len(chunk)
        data.points.append((sum(times[first:last]) / count, sum(chunk) / count,
                            min(chunk), max(chunk), count))
        first = last
    return True


def downsample(readings: Iterable[Reading], start: datetime, end: datetime,
               max_points: int) -> ChartData:
    """Bucket readings into at most about max_points points, keeping min and max."""
    span = max((end - start).total_seconds(), 1.0)
    width = max(span / max(max_points, 1), 1.0)
    origin = wall_seconds(start)
    data = ChartData(bucket_seconds=width)
    if isinstance(readings, list) and _downsample_sorted(readings, origin, width, data):
        return data
    data = ChartData(bucket_seconds=width)
    buckets: Dict[int, List[float]] = {}
    for moment, celsius in readings:
        seconds = wall_seconds(moment)
        data.count += 1
        if data.minimum is None or celsius < data.minimum[1]:
            data.minimum = (seconds, celsius)
        if data.maximum is None or celsius > data.maximum[1]:
            data.maximum = (seconds, celsius)
        index = int((seconds - origin) // width)
        bucket = buckets.get(index)
        if bucket is None:
            buckets[index] = [1, seconds, celsius, celsius, celsius]
            continue
        bucket[0] += 1
        bucket[1] += seconds
        bucket[2] += celsius
        bucket[3] = min(bucket[3], celsius)
        bucket[4] = max(bucket[4], celsius)
    for index in sorted(buckets):
        count, sum_time, sum_value, low, high = buckets[index]
        data.points.append((sum_time / count, sum_value / count, low, high, int(count)))
    return data

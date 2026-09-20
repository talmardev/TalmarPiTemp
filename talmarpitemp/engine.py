"""Monitoring engine: samples the sensor, records to CSV and exposes state.

Three daemon threads run independently, so a slow disk never freezes the live
reading and a slow history reload never delays a recording:
  sampler   reads the sensor about once per second
  recorder  appends the latest reading to the CSV every recording interval
  history   keeps the 7 day cache fresh
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from . import config, storage
from .history import (CACHE_MAX_AGE, WeekCache, aggregate_range, iter_range, read_first, read_last,
                      read_since)
from .stats import WEEK, ChartData, HistorySummary, downsample, in_window
from .temperature import TemperatureReadError, TemperatureSource

SAMPLE_PERIOD = 1.0
FAILURES_BEFORE_ERROR = 3
MAX_READING_AGE = 5.0
WRITE_RETRY_SECONDS = 5.0
JOIN_TIMEOUT = 3.0


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    running: bool
    celsius: Optional[float]
    sensor_error: Optional[str]
    record_error: Optional[str]
    history_error: Optional[str]
    unit: str
    interval: int
    csv_path: Path
    last_recorded: Optional[datetime]
    uptime_seconds: float
    source: str
    hostname: str
    web_url: Optional[str]
    summary: Optional[HistorySummary]

    @property
    def status(self) -> str:
        if self.celsius is not None:
            return "Running"
        return "Sensor error" if self.sensor_error else "Starting"


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, storage.CsvPathError) else str(getattr(exc, "strerror", None) or exc)


class Engine:
    def __init__(self, settings: config.Settings, source: TemperatureSource,
                 config_path: Optional[Path] = None, sample_period: float = SAMPLE_PERIOD,
                 clock: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._source = source
        self._config_path = config_path
        self._sample_period = sample_period
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._csv_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._history = WeekCache()
        self._threads: List[threading.Thread] = []
        self._started = monotonic()
        self._hostname = socket.gethostname()
        self.web_url: Optional[str] = None
        self._celsius: Optional[float] = None
        self._sampled_wall: Optional[datetime] = None
        self._sampled_mono: Optional[float] = None
        self._failures = 0
        self._sensor_error: Optional[str] = None
        self._last_record_mono: Optional[float] = None
        self._last_recorded: Optional[datetime] = None
        self._record_error: Optional[str] = None
        self._history_error: Optional[str] = None

    @property
    def settings(self) -> config.Settings:
        with self._lock:
            return self._settings

    def start(self) -> None:
        last = read_last(self.settings.csv_path)
        with self._lock:
            self._last_recorded = last[0] if last else None
        for name, target in (("sampler", self._sampler_loop), ("recorder", self._recorder_loop),
                             ("history", self._history_loop)):
            thread = threading.Thread(target=target, name=f"talmarpitemp-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(JOIN_TIMEOUT)
        self._threads.clear()

    def sample_once(self) -> None:
        try:
            celsius = self._source.read_celsius()
        except TemperatureReadError as exc:
            self._note_sensor_failure(str(exc))
        except Exception as exc:
            self._note_sensor_failure(f"Unexpected sensor error: {exc}")
        else:
            with self._lock:
                self._celsius = celsius
                self._sampled_wall = self._clock()
                self._sampled_mono = self._monotonic()
                self._failures = 0
                self._sensor_error = None

    def _note_sensor_failure(self, message: str) -> None:
        with self._lock:
            self._failures += 1
            self._sensor_error = message
            if self._failures >= FAILURES_BEFORE_ERROR:
                self._celsius = None

    def record_if_due(self) -> float:
        """Write a row when the interval has elapsed. Returns seconds to wait before the next check."""
        now = self._monotonic()
        with self._lock:
            interval = self._settings.interval
            last = self._last_record_mono
            fresh = (self._celsius is not None and self._sampled_mono is not None
                     and now - self._sampled_mono <= MAX_READING_AGE)
            reading = (self._celsius, self._sampled_wall) if fresh else None
        due = 0.0 if last is None else last + interval
        if now < due:
            return min(due - now, 1.0)
        if reading is None:
            return 1.0
        celsius, moment = reading
        path = None
        try:
            with self._csv_lock:
                path = self._settings.csv_path
                if not path.is_file() or path.stat().st_size == 0:
                    storage.prepare_csv(path)
                storage.append_reading(path, moment, celsius)
        except (OSError, storage.CsvPathError) as exc:
            with self._lock:
                self._record_error = f"Cannot write {path}: {_describe(exc)}"
                self._last_record_mono = now + min(WRITE_RETRY_SECONDS, interval) - interval
            return 1.0
        self._history.note_append(path, moment, celsius,
                                  len(storage.format_reading(moment, celsius)) + 1)
        with self._lock:
            self._last_recorded = moment
            self._record_error = None
            on_time = last is not None and now - due < interval
            self._last_record_mono = due if on_time else now
        return min(interval, 1.0)

    def _sampler_loop(self) -> None:
        next_at = self._monotonic()
        while not self._stop.is_set():
            self.sample_once()
            next_at += self._sample_period
            delay = next_at - self._monotonic()
            if delay < -self._sample_period:
                next_at, delay = self._monotonic(), 0.0
            self._stop.wait(max(delay, 0.0))

    def _recorder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.record_if_due()
            except Exception as exc:
                with self._lock:
                    self._record_error = f"Unexpected recording error: {exc}"
                delay = 1.0
            self._wake.wait(delay)
            self._wake.clear()

    def _history_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._history.refresh(self.settings.csv_path, self._clock())
                error = None
            except Exception as exc:
                error = _describe(exc)
            with self._lock:
                self._history_error = error
            self._stop.wait(1.0)

    def snapshot(self) -> Snapshot:
        now = self._clock()
        with self._lock:
            settings = self._settings
            celsius = self._celsius
            values = (self._sensor_error, self._record_error, self._history_error,
                      self._last_recorded)
        summary = self._history.summary(settings.csv_path, now)
        return Snapshot(
            now=now, running=not self._stop.is_set(), celsius=celsius,
            sensor_error=values[0], record_error=values[1], history_error=values[2],
            unit=settings.unit, interval=settings.interval, csv_path=settings.csv_path,
            last_recorded=values[3], uptime_seconds=self._monotonic() - self._started,
            source=self._source.description, hostname=self._hostname,
            web_url=self.web_url, summary=summary)

    def apply_changes(self, changes: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate every change first, then apply. Raises ValueError with a readable message."""
        interval = config.validate_interval(changes["interval"]) if "interval" in changes else None
        unit = config.validate_unit(changes["unit"]) if "unit" in changes else None
        new_csv = storage.check_csv_path(str(changes["csv_path"])) if "csv_path" in changes else None
        saved: Dict[str, Any] = {}
        if new_csv is not None and self._switch_csv(new_csv, bool(changes.get("copy_history"))):
            saved["csv_path"] = new_csv
        with self._lock:
            if interval is not None:
                self._settings = replace(self._settings, interval=interval)
                saved["interval"] = interval
            if unit is not None:
                self._settings = replace(self._settings, unit=unit)
                saved["unit"] = unit
        self._wake.set()
        warnings: List[str] = []
        if saved and self._config_path is not None:
            try:
                config.save_values(self._config_path, saved)
            except OSError as exc:
                warnings.append(f"Applied, but the settings file could not be saved: {_describe(exc)}")
        return {"warnings": warnings}

    def _switch_csv(self, new_path: Path, copy_history: bool) -> bool:
        with self._csv_lock:
            old_path = self._settings.csv_path
            if new_path == old_path:
                return False
            if copy_history:
                storage.copy_history(old_path, new_path)
            storage.prepare_csv(new_path)
            with self._lock:
                self._settings = replace(self._settings, csv_path=new_path)
                self._record_error = None
        last = read_last(new_path)
        with self._lock:
            self._last_recorded = last[0] if last else None
        return True

    def now(self) -> datetime:
        return self._clock()

    def first_reading_time(self) -> Optional[datetime]:
        first = read_first(self.settings.csv_path)
        return first[0] if first else None

    def chart_data(self, start: datetime, end: datetime, max_points: int) -> ChartData:
        path = self.settings.csv_path
        now = self._clock()
        readings: Iterable[storage.Reading]
        rows = self._history.rows_covering(path, start)
        if rows is None and start >= now - WEEK - CACHE_MAX_AGE:
            rows = read_since(path, start, now)
        if rows is not None:
            readings = in_window(rows, start, end)
        else:
            data = aggregate_range(path, start, end, max_points)
            if data is not None:
                return data
            readings = iter_range(path, start, end)
        return downsample(readings, start, end, max_points)

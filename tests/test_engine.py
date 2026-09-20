import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from talmarpitemp import config, engine, storage
from talmarpitemp.temperature import TemperatureReadError

START = datetime(2026, 9, 20, 12, 0, 0)


class FakeSource:
    description = "fake sensor"

    def __init__(self, *values):
        self.values = list(values)
        self.last = None

    def read_celsius(self):
        if self.values:
            self.last = self.values.pop(0)
        if isinstance(self.last, Exception):
            raise self.last
        return self.last


class FakeTime:
    def __init__(self):
        self.wall = START
        self.mono = 1000.0

    def clock(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds=1.0):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class EngineCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.csv = self.root / "data" / "t.csv"
        storage.prepare_csv(self.csv)
        self.time = FakeTime()
        self.config_path = self.root / "conf.json"

    def make(self, source=None, **settings):
        settings.setdefault("csv_path", self.csv)
        return engine.Engine(config.Settings(**settings), source or FakeSource(50.0),
                             config_path=self.config_path, clock=self.time.clock,
                             monotonic=self.time.monotonic)

    def rows(self, path=None):
        lines = (path or self.csv).read_text().splitlines()
        return [storage.parse_row(line) for line in lines[1:]]

    def run_seconds(self, eng, seconds):
        for _ in range(seconds):
            eng.sample_once()
            eng.record_if_due()
            self.time.advance(1.0)


class SamplingTests(EngineCase):
    def test_live_reading_updates_every_sample_but_csv_only_every_interval(self):
        source = FakeSource(*[40.0 + i for i in range(10)])
        eng = self.make(source, interval=3)
        readings = []
        for _ in range(10):
            eng.sample_once()
            readings.append(eng.snapshot().celsius)
            eng.record_if_due()
            self.time.advance(1.0)
        self.assertEqual(readings, [40.0 + i for i in range(10)])
        rows = self.rows()
        self.assertEqual([r[1] for r in rows], [40.0, 43.0, 46.0, 49.0])
        self.assertEqual([r[0] for r in rows],
                         [START + timedelta(seconds=s) for s in (0, 3, 6, 9)])

    def test_csv_stores_celsius_while_the_unit_is_fahrenheit(self):
        eng = self.make(FakeSource(50.0), unit="F")
        self.run_seconds(eng, 1)
        self.assertEqual(eng.snapshot().unit, "F")
        self.assertEqual(self.rows()[0][1], 50.0)

    def test_transient_failures_keep_the_last_value_then_report_an_error(self):
        error = TemperatureReadError("boom")
        eng = self.make(FakeSource(50.0, error, error, error, 51.0))
        eng.sample_once()
        eng.sample_once()
        snap = eng.snapshot()
        self.assertEqual((snap.celsius, snap.sensor_error), (50.0, "boom"))
        eng.sample_once()
        eng.sample_once()
        snap = eng.snapshot()
        self.assertEqual((snap.celsius, snap.sensor_error), (None, "boom"))
        eng.sample_once()
        snap = eng.snapshot()
        self.assertEqual((snap.celsius, snap.sensor_error), (51.0, None))

    def test_unexpected_source_exceptions_do_not_escape(self):
        eng = self.make(FakeSource(RuntimeError("weird")))
        eng.sample_once()
        self.assertIn("weird", eng.snapshot().sensor_error)

    def test_nothing_is_recorded_without_a_valid_reading(self):
        eng = self.make(FakeSource(TemperatureReadError("no sensor")))
        self.run_seconds(eng, 5)
        self.assertEqual(self.rows(), [])
        self.assertIsNone(eng.snapshot().last_recorded)

    def test_stale_readings_are_not_recorded(self):
        eng = self.make(FakeSource(50.0), interval=1)
        eng.sample_once()
        self.time.advance(engine.MAX_READING_AGE + 1)
        eng.record_if_due()
        self.assertEqual(self.rows(), [])


class RecordingTests(EngineCase):
    def test_write_failure_is_reported_and_retried_without_touching_the_live_reading(self):
        eng = self.make(FakeSource(50.0), interval=10)
        eng.sample_once()
        with mock.patch.object(storage, "append_reading", side_effect=OSError(5, "I/O error")):
            eng.record_if_due()
        snap = eng.snapshot()
        self.assertIn("Cannot write", snap.record_error)
        self.assertEqual(snap.celsius, 50.0)
        self.time.advance(engine.WRITE_RETRY_SECONDS)
        eng.sample_once()
        eng.record_if_due()
        self.assertIsNone(eng.snapshot().record_error)
        self.assertEqual(len(self.rows()), 1)

    def test_deleted_csv_is_recreated_with_its_header(self):
        eng = self.make(FakeSource(50.0), interval=1)
        self.run_seconds(eng, 1)
        self.csv.unlink()
        self.run_seconds(eng, 1)
        self.assertEqual(self.csv.read_text().splitlines()[0], storage.HEADER)
        self.assertEqual(len(self.rows()), 1)

    def test_shorter_interval_takes_effect_immediately(self):
        eng = self.make(FakeSource(50.0), interval=60)
        self.run_seconds(eng, 3)
        self.assertEqual(len(self.rows()), 1)
        eng.apply_changes({"interval": 2})
        self.run_seconds(eng, 3)
        self.assertGreaterEqual(len(self.rows()), 2)

    def test_last_recorded_starts_from_the_file(self):
        storage.append_reading(self.csv, START - timedelta(hours=1), 44.0)
        eng = self.make()
        eng.start()
        self.addCleanup(eng.stop)
        self.assertEqual(eng.snapshot().last_recorded, START - timedelta(hours=1))


class HistoryFromCsvTests(EngineCase):
    def test_summary_comes_from_the_file_not_from_the_session(self):
        with open(self.csv, "a") as handle:
            for days, celsius in ((9, 90.0), (5, 40.0), (2, 60.0), (0.5, 50.0)):
                moment = START - timedelta(days=days)
                handle.write(storage.format_reading(moment, celsius) + "\n")
        eng = self.make(FakeSource(45.0))
        eng._history.refresh(self.csv, START)
        summary = eng.snapshot().summary
        self.assertEqual((summary.count_7d, summary.count_24h), (3, 1))
        self.assertEqual((summary.maximum, summary.minimum), (60.0, 40.0))
        self.assertAlmostEqual(summary.average, 50.0)

    def test_recording_updates_the_summary(self):
        eng = self.make(FakeSource(45.0), interval=1)
        eng._history.refresh(self.csv, START)
        self.assertEqual(eng.snapshot().summary.count_7d, 0)
        self.run_seconds(eng, 1)
        self.assertEqual(eng.snapshot().summary.maximum, 45.0)

    def test_chart_data_reads_the_csv(self):
        with open(self.csv, "a") as handle:
            for minutes in (120, 60, 10):
                moment = START - timedelta(minutes=minutes)
                handle.write(storage.format_reading(moment, 40.0 + minutes / 10) + "\n")
        eng = self.make()
        data = eng.chart_data(START - timedelta(hours=24), START, 1000)
        self.assertEqual([round(p[1], 1) for p in data.points], [52.0, 46.0, 41.0])
        old = eng.chart_data(START - timedelta(days=30), START - timedelta(days=20), 1000)
        self.assertEqual(old.points, [])

    def test_week_chart_keeps_full_resolution_even_when_the_clock_moves_between_calls(self):
        with open(self.csv, "a") as handle:
            for i in range(60_000):
                moment = START - timedelta(days=6, hours=23) + timedelta(seconds=10 * i)
                handle.write(f"{moment.isoformat(' ')},{40.0 + (i % 50) / 10:.2f}\n")
        ticking = iter(START + timedelta(milliseconds=ms) for ms in range(0, 10_000_000, 3))
        eng = engine.Engine(config.Settings(csv_path=self.csv), FakeSource(50.0),
                            clock=lambda: next(ticking), monotonic=self.time.monotonic)
        for cached in (False, True):
            if cached:
                eng._history.refresh(self.csv, eng.now())
            start = eng.now() - timedelta(days=7)
            data = eng.chart_data(start, eng.now(), 10_000)
            self.assertEqual(data.count, 60_000)
            self.assertGreater(len(data.points), 9000)


class ApplyChangesTests(EngineCase):
    def test_unit_and_interval_are_applied_and_saved(self):
        eng = self.make()
        eng.apply_changes({"unit": "f", "interval": "30"})
        snap = eng.snapshot()
        self.assertEqual((snap.unit, snap.interval), ("F", 30))
        self.assertEqual(json.loads(self.config_path.read_text()), {"unit": "F", "interval": 30})

    def test_invalid_changes_apply_nothing(self):
        eng = self.make()
        for changes in ({"interval": 0}, {"interval": -5}, {"unit": "K"},
                        {"interval": 20, "csv_path": str(self.root / "x.txt")},
                        {"unit": "F", "interval": "abc"}):
            with self.assertRaises(ValueError):
                eng.apply_changes(changes)
        snap = eng.snapshot()
        self.assertEqual((snap.unit, snap.interval, snap.csv_path), ("C", 10, self.csv))
        self.assertFalse(self.config_path.exists())

    def test_switching_csv_keeps_the_old_file_and_starts_a_new_history(self):
        storage.append_reading(self.csv, START, 44.0)
        eng = self.make(FakeSource(50.0), interval=1)
        new = self.root / "other" / "n.csv"
        eng.apply_changes({"csv_path": str(new)})
        self.assertEqual(eng.snapshot().csv_path, new)
        self.assertIsNone(eng.snapshot().last_recorded)
        self.run_seconds(eng, 1)
        self.assertEqual(len(self.rows(new)), 1)
        self.assertEqual(len(self.rows(self.csv)), 1)
        self.assertEqual(json.loads(self.config_path.read_text())["csv_path"], str(new))

    def test_copy_history_is_only_done_when_requested(self):
        storage.append_reading(self.csv, START, 44.0)
        eng = self.make()
        plain = self.root / "plain.csv"
        eng.apply_changes({"csv_path": str(plain)})
        self.assertEqual(self.rows(plain), [])
        eng.apply_changes({"csv_path": str(self.csv)})
        copied = self.root / "copied.csv"
        eng.apply_changes({"csv_path": str(copied), "copy_history": True})
        self.assertEqual(self.rows(copied), [(START, 44.0)])
        self.assertEqual(self.rows(self.csv), [(START, 44.0)])

    def test_switch_to_a_foreign_file_is_refused(self):
        foreign = self.root / "foreign.csv"
        foreign.write_text("a,b\n1,2\n")
        eng = self.make()
        with self.assertRaises(ValueError):
            eng.apply_changes({"csv_path": str(foreign)})
        self.assertEqual(eng.snapshot().csv_path, self.csv)
        self.assertEqual(foreign.read_text(), "a,b\n1,2\n")

    def test_unwritable_settings_file_only_warns(self):
        eng = engine.Engine(config.Settings(csv_path=self.csv), FakeSource(50.0),
                            config_path=self.root, clock=self.time.clock,
                            monotonic=self.time.monotonic)
        result = eng.apply_changes({"unit": "F"})
        self.assertEqual(eng.snapshot().unit, "F")
        self.assertEqual(len(result["warnings"]), 1)


class ThreadTests(EngineCase):
    def test_threads_sample_independently_of_recording_and_stop_cleanly(self):
        eng = engine.Engine(config.Settings(csv_path=self.csv, interval=1), FakeSource(50.0),
                            sample_period=0.02)
        seen = set()
        eng.start()
        try:
            deadline = time.time() + 2.5
            while time.time() < deadline:
                seen.add(eng.snapshot().now)
                time.sleep(0.02)
        finally:
            eng.stop()
        self.assertGreater(len(seen), 50)
        self.assertTrue(2 <= len(self.rows()) <= 4)
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("talmarpitemp")], [])


if __name__ == "__main__":
    unittest.main()

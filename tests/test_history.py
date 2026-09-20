import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from talmarpitemp import history, stats, storage

NOW = datetime(2026, 9, 20, 12, 0, 0)


def csv_text(rows):
    lines = [storage.HEADER]
    lines += [f"{moment.isoformat(' ')},{celsius:.2f}" for moment, celsius in rows]
    return "\n".join(lines) + "\n"


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "t.csv"

    def write(self, text):
        self.path.write_text(text)


class ReadSinceTests(HistoryCase):
    def test_missing_empty_and_header_only_files(self):
        self.assertEqual(history.read_since(self.path, NOW - timedelta(days=7)), [])
        self.write("")
        self.assertEqual(history.read_since(self.path, NOW - timedelta(days=7)), [])
        self.write(storage.HEADER + "\n")
        self.assertEqual(history.read_since(self.path, NOW - timedelta(days=7)), [])

    def test_filters_old_rows_and_skips_invalid_ones(self):
        self.write(storage.HEADER + "\n"
                   "2026-09-01 10:00:00,44.0\n"
                   "not,a,row\n"
                   "2026-09-18 10:00:00,45.0\n"
                   "2026-09-19 10:00:00,abc\n"
                   "2026-09-20 10:00:00,46.0\n"
                   "2026-09-20 11:00:00,47.0")
        rows = history.read_since(self.path, NOW - timedelta(days=7), NOW)
        self.assertEqual([r[1] for r in rows], [45.0, 46.0, 47.0])

    def test_excludes_rows_after_until(self):
        self.write(csv_text([(NOW - timedelta(hours=1), 40.0), (NOW + timedelta(hours=1), 99.0)]))
        rows = history.read_since(self.path, NOW - timedelta(days=7), NOW)
        self.assertEqual([r[1] for r in rows], [40.0])

    def test_large_file_only_reads_the_tail(self):
        start = NOW - timedelta(seconds=10 * 200_000)
        self.write(csv_text([(start + timedelta(seconds=10 * i), 40.0 + (i % 20) / 10)
                             for i in range(200_000)]))
        with mock.patch.object(history, "parse_row", wraps=storage.parse_row) as parser:
            rows = history.read_since(self.path, NOW - timedelta(days=7), NOW)
        self.assertEqual(len(rows), 7 * 24 * 360)
        self.assertLess(parser.call_count, 100_000)
        self.assertEqual(rows, sorted(rows))

    def test_tolerates_a_backward_clock_jump_inside_the_window(self):
        rows = [(NOW - timedelta(days=6, seconds=10 * (2000 - i)), 40.0) for i in range(2000)]
        jump = [(rows[-1][0] - timedelta(hours=1) + timedelta(seconds=10 * i), 41.0)
                for i in range(2000)]
        old = [(NOW - timedelta(days=30, seconds=10 * i), 10.0) for i in range(30_000)]
        self.write(csv_text(sorted(old) + rows + jump))
        found = history.read_since(self.path, NOW - timedelta(days=7), NOW)
        self.assertEqual(len(found), 4000)


class ReadLastTests(HistoryCase):
    def test_missing_and_empty(self):
        self.assertIsNone(history.read_last(self.path))
        self.write(storage.HEADER + "\n")
        self.assertIsNone(history.read_last(self.path))

    def test_skips_trailing_garbage_and_torn_line(self):
        self.write(storage.HEADER + "\n2026-09-20 11:00:00,47.0\nnot a row\n2026-09-20 11:00:1")
        self.assertEqual(history.read_last(self.path)[1], 47.0)


class IterRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.path = Path(cls._dir.name) / "t.csv"
        cls.start = NOW - timedelta(days=30)
        cls.rows = [(cls.start + timedelta(seconds=10 * i), 40.0 + (i % 100) / 10)
                    for i in range(30 * 8640)]
        cls.path.write_text(csv_text(cls.rows))

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def expected(self, low, high):
        return [r for r in self.rows if low <= r[0] <= high]

    def test_middle_range_matches_a_full_scan(self):
        low, high = NOW - timedelta(days=20), NOW - timedelta(days=19, hours=12)
        found = list(history.iter_range(self.path, low, high))
        self.assertEqual(len(found), len(self.expected(low, high)))
        self.assertEqual(found[0][0], low)

    def test_ranges_outside_the_data(self):
        self.assertEqual(list(history.iter_range(self.path, NOW + timedelta(days=1),
                                                 NOW + timedelta(days=2))), [])
        self.assertEqual(list(history.iter_range(self.path, self.start - timedelta(days=9),
                                                 self.start - timedelta(days=8))), [])

    def test_range_covering_everything(self):
        found = list(history.iter_range(self.path, self.start - timedelta(days=1), NOW))
        self.assertEqual(len(found), len(self.rows))

    def test_missing_file(self):
        self.assertEqual(list(history.iter_range(self.path.with_name("nope.csv"), self.start, NOW)), [])

    def test_block_aggregation_matches_the_row_by_row_path(self):
        low, high = NOW - timedelta(days=25), NOW - timedelta(days=2)
        exact = stats.downsample(list(history.iter_range(self.path, low, high)), low, high, 10000)
        fast = history.aggregate_range(self.path, low, high, 10000)
        self.assertIsNotNone(fast)
        self.assertEqual(fast.count, exact.count)
        self.assertEqual((fast.minimum[1], fast.maximum[1]), (exact.minimum[1], exact.maximum[1]))
        self.assertAlmostEqual(sum(p[1] * p[4] for p in fast.points) / fast.count,
                               sum(p[1] * p[4] for p in exact.points) / exact.count, places=6)
        self.assertLessEqual(len(fast.points), 10001)
        self.assertGreater(len(fast.points), 1000)
        times = [p[0] for p in fast.points]
        self.assertEqual(times, sorted(times))
        self.assertLessEqual(abs(fast.minimum[0] - exact.minimum[0]), 2 * fast.bucket_seconds)
        self.assertGreaterEqual(fast.points[0][0], stats.wall_seconds(low))
        self.assertLessEqual(fast.points[-1][0], stats.wall_seconds(high))

    def test_block_aggregation_is_skipped_for_small_ranges_and_missing_files(self):
        self.assertIsNone(history.aggregate_range(self.path, NOW - timedelta(days=2), NOW, 10000))
        self.assertIsNone(history.aggregate_range(self.path.with_name("nope.csv"), self.start, NOW, 100))


class AggregateRobustnessTests(HistoryCase):
    def test_garbage_and_implausible_rows_do_not_poison_the_blocks(self):
        start = NOW - timedelta(days=10)
        lines = [storage.HEADER]
        for i in range(70_000):
            lines.append(f"{(start + timedelta(seconds=10 * i)).isoformat(' ')},{45 + (i % 7):.2f}")
            if i == 30_000:
                lines += ["garbage line", storage.HEADER, "2026-09-10 10:00:00,9999.00",
                          "2026-09-10 10:00:01,nan", "2026-09-10 10:00:02,-99.00", "2026-09-10 10:00:03,"]
        self.write("\n".join(lines) + "\n")
        data = history.aggregate_range(self.path, start, NOW, 500)
        self.assertEqual(data.count, 70_000)
        self.assertEqual((data.minimum[1], data.maximum[1]), (45.0, 51.0))
        self.assertTrue(all(45.0 <= p[2] <= p[3] <= 51.0 for p in data.points))


class WeekCacheTests(HistoryCase):
    def test_summary_before_refresh_is_none(self):
        self.assertIsNone(history.WeekCache().summary(self.path, NOW))

    def test_refresh_then_summary(self):
        self.write(csv_text([(NOW - timedelta(hours=2), 40.0), (NOW - timedelta(days=9), 90.0),
                             (NOW - timedelta(hours=1), 60.0)]))
        cache = history.WeekCache()
        cache.refresh(self.path, NOW)
        summary = cache.summary(self.path, NOW)
        self.assertEqual((summary.count_7d, summary.maximum, summary.minimum), (2, 60.0, 40.0))

    def test_missing_file_gives_an_empty_summary(self):
        cache = history.WeekCache()
        cache.refresh(self.path, NOW)
        self.assertEqual(cache.summary(self.path, NOW).count_7d, 0)

    def test_note_append_updates_without_a_reload(self):
        storage.prepare_csv(self.path)
        cache = history.WeekCache()
        cache.refresh(self.path, NOW)
        moment = NOW - timedelta(seconds=5)
        storage.append_reading(self.path, moment, 55.0)
        cache.note_append(self.path, moment, 55.0, len(storage.format_reading(moment, 55.0)) + 1)
        with mock.patch.object(history, "read_since") as reader:
            cache.refresh(self.path, NOW)
            self.assertEqual(cache.summary(self.path, NOW).maximum, 55.0)
        reader.assert_not_called()

    def test_outside_edit_triggers_a_reload(self):
        self.write(csv_text([(NOW - timedelta(hours=1), 40.0)]))
        cache = history.WeekCache()
        cache.refresh(self.path, NOW)
        self.write(csv_text([(NOW - timedelta(hours=1), 40.0), (NOW - timedelta(hours=2), 70.0)]))
        cache.refresh(self.path, NOW)
        self.assertEqual(cache.summary(self.path, NOW).maximum, 70.0)

    def test_switching_path_drops_old_data(self):
        self.write(csv_text([(NOW - timedelta(hours=1), 40.0)]))
        other = self.path.with_name("other.csv")
        cache = history.WeekCache()
        cache.refresh(self.path, NOW)
        self.assertIsNone(cache.summary(other, NOW))
        cache.refresh(other, NOW)
        self.assertEqual(cache.summary(other, NOW).count_7d, 0)


if __name__ == "__main__":
    unittest.main()

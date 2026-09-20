import unittest
from datetime import datetime, timedelta

from talmarpitemp import stats

NOW = datetime(2026, 9, 20, 12, 0, 0)


def row(delta, celsius):
    return (NOW - delta, celsius)


class SummarizeHistoryTests(unittest.TestCase):
    def test_no_readings_means_no_data(self):
        summary = stats.summarize_history([], NOW)
        self.assertEqual((summary.count_7d, summary.count_24h), (0, 0))
        self.assertIsNone(summary.maximum)
        self.assertIsNone(summary.minimum)
        self.assertIsNone(summary.average)

    def test_only_old_readings_means_no_data(self):
        summary = stats.summarize_history([row(timedelta(days=8), 50.0)], NOW)
        self.assertEqual(summary.count_7d, 0)
        self.assertIsNone(summary.maximum)

    def test_max_min_average(self):
        readings = [row(timedelta(hours=1), 40.0), row(timedelta(hours=2), 60.0),
                    row(timedelta(days=3), 50.0)]
        summary = stats.summarize_history(readings, NOW)
        self.assertEqual(summary.maximum, 60.0)
        self.assertEqual(summary.minimum, 40.0)
        self.assertAlmostEqual(summary.average, 50.0)

    def test_seven_day_filter_excludes_old_and_future_rows(self):
        readings = [row(timedelta(days=7, seconds=1), 99.0),
                    row(timedelta(days=7), 41.0),
                    row(timedelta(days=6), 42.0),
                    row(timedelta(0), 43.0),
                    row(-timedelta(seconds=1), 100.0)]
        summary = stats.summarize_history(readings, NOW)
        self.assertEqual(summary.count_7d, 3)
        self.assertEqual(summary.maximum, 43.0)
        self.assertEqual(summary.minimum, 41.0)

    def test_counts_last_24_hours_separately(self):
        readings = [row(timedelta(hours=23), 1.0), row(timedelta(hours=25), 2.0),
                    row(timedelta(days=2), 3.0)]
        summary = stats.summarize_history(readings, NOW)
        self.assertEqual((summary.count_24h, summary.count_7d), (1, 3))

    def test_unordered_input_gives_the_same_answer(self):
        readings = [row(timedelta(hours=h), 40.0 + h) for h in range(1, 30)]
        forward = stats.summarize_history(readings, NOW)
        backward = stats.summarize_history(list(reversed(readings)), NOW)
        self.assertEqual(forward, backward)

    def test_in_window_is_inclusive(self):
        readings = [row(timedelta(hours=3), 1.0), row(timedelta(hours=2), 2.0),
                    row(timedelta(hours=1), 3.0)]
        kept = stats.in_window(readings, NOW - timedelta(hours=2), NOW - timedelta(hours=1))
        self.assertEqual([r[1] for r in kept], [2.0, 3.0])


class DownsampleTests(unittest.TestCase):
    START = NOW - timedelta(hours=1)

    def test_empty(self):
        data = stats.downsample([], self.START, NOW, 100)
        self.assertEqual((data.points, data.count), ([], 0))
        self.assertIsNone(data.minimum)
        self.assertIsNone(data.maximum)

    def test_sparse_readings_stay_exact(self):
        readings = [(self.START + timedelta(minutes=m), 40.0 + m) for m in range(0, 60, 10)]
        data = stats.downsample(readings, self.START, NOW, 3600)
        self.assertEqual(len(data.points), 6)
        for (_, avg, low, high, count), (_, celsius) in zip(data.points, readings):
            self.assertEqual((avg, low, high, count), (celsius, celsius, celsius, 1))

    def test_dense_readings_are_bucketed_keeping_extremes(self):
        readings = [(self.START + timedelta(seconds=s), 40.0 + (s % 10)) for s in range(3600)]
        data = stats.downsample(readings, self.START, NOW, 60)
        self.assertLessEqual(len(data.points), 61)
        self.assertEqual(sum(p[4] for p in data.points), 3600)
        self.assertEqual(min(p[2] for p in data.points), 40.0)
        self.assertEqual(max(p[3] for p in data.points), 49.0)

    def test_extremes_carry_their_time(self):
        readings = [(self.START + timedelta(minutes=1), 50.0),
                    (self.START + timedelta(minutes=2), 30.0),
                    (self.START + timedelta(minutes=3), 70.0)]
        data = stats.downsample(readings, self.START, NOW, 100)
        self.assertEqual(data.minimum, (stats.wall_seconds(self.START + timedelta(minutes=2)), 30.0))
        self.assertEqual(data.maximum, (stats.wall_seconds(self.START + timedelta(minutes=3)), 70.0))

    def test_list_fast_path_and_streaming_path_agree(self):
        readings = [(self.START + timedelta(seconds=7 * i), 40.0 + (i * 37 % 100) / 10) for i in range(500)]
        fast = stats.downsample(readings, self.START, NOW, 60)
        slow = stats.downsample(iter(readings), self.START, NOW, 60)
        self.assertEqual((fast.count, fast.minimum, fast.maximum), (slow.count, slow.minimum, slow.maximum))
        self.assertEqual(len(fast.points), len(slow.points))
        for a, b in zip(fast.points, slow.points):
            self.assertEqual(a[4], b[4])
            for x, y in zip(a[:4], b[:4]):
                self.assertAlmostEqual(x, y, places=6)

    def test_unordered_lists_still_work(self):
        readings = [(self.START + timedelta(minutes=m), 40.0 + m) for m in (30, 10, 20)]
        data = stats.downsample(readings, self.START, NOW, 100)
        self.assertEqual([p[1] for p in data.points], [50.0, 60.0, 70.0])
        self.assertEqual(data.count, 3)

    def test_points_are_sorted_by_time(self):
        readings = [(self.START + timedelta(minutes=m), 40.0) for m in (30, 10, 20)]
        times = [p[0] for p in stats.downsample(readings, self.START, NOW, 100).points]
        self.assertEqual(times, sorted(times))


if __name__ == "__main__":
    unittest.main()

import io
import os
import re
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from talmarpitemp import display
from talmarpitemp.engine import Snapshot
from talmarpitemp.stats import HistorySummary

NOW = datetime(2026, 9, 20, 12, 35, 0)


def make_snapshot(**overrides):
    values = dict(
        now=NOW, running=True, celsius=48.7, sensor_error=None, record_error=None,
        history_error=None, unit="C", interval=10, csv_path=Path("/home/pi/temperature.csv"),
        last_recorded=datetime(2026, 9, 20, 12, 34, 50), uptime_seconds=5.0,
        source="/sys/class/thermal/thermal_zone0/temp", hostname="pi",
        web_url="http://127.0.0.1:5000",
        summary=HistorySummary(count_24h=10, count_7d=20, maximum=67.4, minimum=39.2, average=51.8))
    values.update(overrides)
    return Snapshot(**values)


class RenderTests(unittest.TestCase):
    def text(self, **overrides):
        return "\n".join(display.render(make_snapshot(**overrides)))

    def test_celsius_dashboard_matches_the_brief(self):
        text = self.text()
        for expected in ("Status: Running", "Unit: Celsius", "Current Temperature: 48.7 °C",
                         "Last 7 Days", "Maximum: 67.4 °C", "Minimum: 39.2 °C",
                         "Average: 51.8 °C", "Recording Interval: 10 seconds",
                         "CSV Location: " + str(Path("/home/pi/temperature.csv")),
                         "Last Recorded: 12:34:50", "Web Interface: http://127.0.0.1:5000",
                         "Press Ctrl+C to stop"):
            self.assertIn(expected, text)

    def test_fahrenheit_values_are_converted(self):
        text = self.text(unit="F")
        self.assertIn("Unit: Fahrenheit", text)
        self.assertIn("Current Temperature: 119.7 °F", text)
        self.assertIn("Maximum: 153.3 °F", text)
        self.assertIn("Minimum: 102.6 °F", text)

    def test_no_data_in_the_last_seven_days(self):
        text = self.text(summary=HistorySummary(0, 0, None, None, None))
        for label in ("Maximum", "Minimum", "Average"):
            self.assertIn(f"{label}: No data", text)

    def test_history_not_loaded_yet(self):
        self.assertIn("Maximum: Loading...", self.text(summary=None))

    def test_sensor_error_and_other_problems_are_visible(self):
        text = self.text(celsius=None, sensor_error="Cannot read x", record_error="Cannot write y")
        self.assertIn("Status: Sensor error", text)
        self.assertIn("Current Temperature: Unavailable", text)
        self.assertIn("Sensor problem: Cannot read x", text)
        self.assertIn("Recording problem: Cannot write y", text)

    def test_starting_state(self):
        text = self.text(celsius=None)
        self.assertIn("Status: Starting", text)

    def test_interval_wording_and_disabled_web(self):
        self.assertIn("Recording Interval: 1 second\n", self.text(interval=1) + "\n")
        self.assertIn("Web Interface: Disabled", self.text(web_url=None))

    def test_last_recorded_formats(self):
        self.assertEqual(display.format_last_recorded(None, NOW), "Nothing recorded yet")
        self.assertEqual(display.format_last_recorded(datetime(2026, 9, 19, 8, 0, 1), NOW),
                         "2026-09-19 08:00:01")

    def test_long_lines_are_shortened_to_the_terminal_width(self):
        self.assertEqual(display._fit("x" * 100, 40), "x" * 36 + "...")
        self.assertEqual(display._fit("short", 40), "short")


class MiniTerminal:
    """Just enough of a terminal to check that frames overwrite each other."""

    CSI = re.compile(r"\x1b\[(\??)(\d*)([A-Za-z])")

    def __init__(self, width=200, height=60):
        self.width, self.height = width, height
        self.screen = self._blank()
        self.main_screen = None
        self.row = self.col = self.scrolled = 0
        self.cursor_visible = True

    def _blank(self):
        return [[" "] * self.width for _ in range(self.height)]

    def feed(self, data):
        i = 0
        while i < len(data):
            char = data[i]
            match = self.CSI.match(data, i) if char == "\x1b" else None
            if match:
                self._csi(*match.groups())
                i = match.end()
                continue
            i += 1
            if char == "\n":
                self.row, self.col = self.row + 1, 0
                if self.row >= self.height:
                    self.screen.pop(0)
                    self.screen.append([" "] * self.width)
                    self.row, self.scrolled = self.height - 1, self.scrolled + 1
            elif self.col < self.width:
                self.screen[self.row][self.col] = char
                self.col += 1

    def _csi(self, private, number, final):
        if private and number == "1049":
            if final == "h":
                self.main_screen, self.screen = self.screen, self._blank()
            elif self.main_screen is not None:
                self.screen, self.main_screen = self.main_screen, None
            self.row = self.col = 0
        elif private and number == "25":
            self.cursor_visible = final == "h"
        elif final == "H":
            self.row = self.col = 0
        elif final == "K":
            self.screen[self.row][self.col:] = [" "] * (self.width - self.col)
        elif final == "J":
            self.screen[self.row][self.col:] = [" "] * (self.width - self.col)
            for row in range(self.row + 1, self.height):
                self.screen[row] = [" "] * self.width

    def lines(self):
        rows = ["".join(row).rstrip() for row in self.screen]
        while rows and not rows[-1]:
            rows.pop()
        return rows


class FakeEngine:
    def __init__(self, **overrides):
        self.overrides = overrides
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return make_snapshot(celsius=40.0 + self.calls, **self.overrides)


class DashboardTests(unittest.TestCase):
    def test_redraws_in_place_and_restores_the_terminal(self):
        stream = io.StringIO()
        engine = FakeEngine()
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        display.Dashboard(engine, stream, refresh=0.05).run(stop)
        output = stream.getvalue()
        self.assertGreaterEqual(engine.calls, 3)
        self.assertEqual(output.count(display.CURSOR_HOME), engine.calls)
        self.assertTrue(output.startswith(display.ENTER_SCREEN))
        self.assertTrue(output.endswith(display.LEAVE_SCREEN))
        self.assertIn("41.0", output)
        self.assertIn("42.0", output)

    def test_frames_overwrite_each_other_instead_of_scrolling(self):
        stream = io.StringIO()
        engine = FakeEngine()
        stop = threading.Event()
        threading.Timer(0.4, stop.set).start()
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((200, 60))):
            display.Dashboard(engine, stream, refresh=0.05).run(stop)
        output = stream.getvalue()
        terminal = MiniTerminal()
        terminal.feed(output[:-len(display.LEAVE_SCREEN)])
        expected = display.render(make_snapshot(celsius=40.0 + engine.calls))
        self.assertGreater(engine.calls, 3)
        self.assertEqual(terminal.lines(), [line.rstrip() for line in expected])
        self.assertEqual(terminal.scrolled, 0)
        self.assertFalse(terminal.cursor_visible)
        terminal.feed(display.LEAVE_SCREEN)
        self.assertTrue(terminal.cursor_visible)
        self.assertEqual(terminal.lines(), [])

    def test_a_shorter_frame_leaves_no_stale_lines(self):
        terminal = MiniTerminal()
        long_frame = display.render(make_snapshot(sensor_error="Cannot read x"))
        short_frame = display.render(make_snapshot())
        stream = io.StringIO()
        dash = display.Dashboard(FakeEngine(), stream)
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((200, 60))):
            dash._draw(long_frame)
            dash._draw(short_frame)
        terminal.feed(stream.getvalue())
        self.assertEqual(terminal.lines(), [line.rstrip() for line in short_frame])

    def test_short_terminals_drop_blank_lines_instead_of_scrolling(self):
        stream = io.StringIO()
        dash = display.Dashboard(FakeEngine(), stream)
        frame = display.render(make_snapshot())
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 20))):
            dash._draw(frame)
        terminal = MiniTerminal(width=80, height=20)
        terminal.feed(stream.getvalue())
        self.assertEqual(terminal.scrolled, 0)
        self.assertEqual(terminal.lines()[0], "TalmarPiTemp")
        self.assertIn("Press Ctrl+C to stop", terminal.lines())

    def test_headless_logs_only_problem_changes(self):
        stream = io.StringIO()
        states = [make_snapshot(), make_snapshot(sensor_error="boom"), make_snapshot(sensor_error="boom"),
                  make_snapshot()]
        stop = threading.Event()

        class Engine:
            def snapshot(self):
                if not states:
                    stop.set()
                    return make_snapshot()
                return states.pop(0)

        display.run_headless(Engine(), stop, stream, refresh=0.0)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Sensor problem: boom", lines[0])
        self.assertIn("All problems cleared", lines[1])


if __name__ == "__main__":
    unittest.main()

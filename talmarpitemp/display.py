"""Terminal output: the live in-place dashboard and the headless log."""

from __future__ import annotations

import os
import shutil
import sys
import threading
from datetime import datetime
from typing import List, Optional, TextIO

from . import APP_NAME, APP_TAGLINE
from .engine import Engine, Snapshot
from .temperature import UNIT_NAMES, from_celsius, unit_symbol

ENTER_SCREEN = "\x1b[?1049h\x1b[?25l"
LEAVE_SCREEN = "\x1b[?25h\x1b[?1049l"
CURSOR_HOME = "\x1b[H"
CLEAR_LINE_END = "\x1b[K"
CLEAR_BELOW = "\x1b[J"


def format_temperature(celsius: Optional[float], unit: str) -> str:
    if celsius is None:
        return "No data"
    return f"{from_celsius(celsius, unit):.1f} {unit_symbol(unit)}"


def format_interval(seconds: int) -> str:
    return f"{seconds} second" + ("" if seconds == 1 else "s")


def format_last_recorded(moment: Optional[datetime], now: datetime) -> str:
    if moment is None:
        return "Nothing recorded yet"
    if moment.date() == now.date():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def render(snapshot: Snapshot) -> List[str]:
    """Build the dashboard lines. Pure, so it can be tested without a terminal."""
    unit = snapshot.unit
    summary = snapshot.summary
    if summary is None:
        stats = ["Loading..."] * 3
    else:
        stats = [format_temperature(summary.maximum, unit), format_temperature(summary.minimum, unit),
                 format_temperature(summary.average, unit)]
    if snapshot.celsius is not None:
        current = format_temperature(snapshot.celsius, unit)
    elif snapshot.sensor_error:
        current = "Unavailable"
    else:
        current = "Waiting for the first reading"
    lines = [APP_NAME, APP_TAGLINE, "", f"Status: {snapshot.status}", f"Unit: {UNIT_NAMES[unit]}"]
    for label, message in (("Sensor", snapshot.sensor_error), ("Recording", snapshot.record_error),
                           ("History", snapshot.history_error)):
        if message:
            lines.append(f"{label} problem: {message}")
    lines += [
        "", f"Current Temperature: {current}", "",
        "Last 7 Days", f"Maximum: {stats[0]}", f"Minimum: {stats[1]}", f"Average: {stats[2]}", "",
        f"Recording Interval: {format_interval(snapshot.interval)}",
        f"CSV Location: {snapshot.csv_path}",
        f"Last Recorded: {format_last_recorded(snapshot.last_recorded, snapshot.now)}", "",
        f"Web Interface: {snapshot.web_url or 'Disabled'}", "",
        "Press Ctrl+C to stop",
    ]
    return lines


def _fit(line: str, width: int) -> str:
    return line if len(line) < width else line[:max(width - 4, 1)] + "..."


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class Dashboard:
    """Redraws the dashboard in place on the alternate screen about once per second."""

    def __init__(self, engine: Engine, stream: TextIO = sys.stdout, refresh: float = 1.0) -> None:
        self._engine = engine
        self._stream = stream
        self._refresh = refresh

    def _draw(self, lines: List[str]) -> None:
        size = shutil.get_terminal_size((80, 24))
        if len(lines) >= size.lines:
            lines = [line for line in lines if line][:max(size.lines - 1, 1)]
        body = "".join(_fit(line, size.columns) + CLEAR_LINE_END + "\n" for line in lines)
        self._stream.write(CURSOR_HOME + body + CLEAR_BELOW)
        self._stream.flush()

    def run(self, stop: threading.Event) -> None:
        _enable_windows_ansi()
        if hasattr(self._stream, "reconfigure"):
            self._stream.reconfigure(errors="replace")
        self._stream.write(ENTER_SCREEN)
        try:
            while not stop.is_set():
                self._draw(render(self._engine.snapshot()))
                stop.wait(self._refresh)
        finally:
            self._stream.write(LEAVE_SCREEN)
            self._stream.flush()


def run_headless(engine: Engine, stop: threading.Event, stream: TextIO = sys.stdout,
                 refresh: float = 1.0) -> None:
    """Without a terminal, print one line at startup and one line per problem change."""
    previous = None
    while not stop.is_set():
        snap = engine.snapshot()
        problems = (snap.sensor_error, snap.record_error, snap.history_error)
        if problems != previous:
            stamp = snap.now.strftime("%Y-%m-%d %H:%M:%S")
            for index, label in enumerate(("Sensor", "Recording", "History")):
                message = problems[index]
                if message and (previous is None or message != previous[index]):
                    stream.write(f"{stamp} {label} problem: {message}\n")
            if previous is not None and not any(problems):
                stream.write(f"{stamp} All problems cleared\n")
            stream.flush()
            previous = problems
        stop.wait(refresh)

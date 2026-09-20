"""Command line entry point: wires settings, engine, dashboard and web server."""

from __future__ import annotations

import signal
import sys
import threading
from dataclasses import replace
from typing import List, Optional

from . import APP_NAME, __version__, config, storage
from .display import Dashboard, run_headless
from .engine import Engine
from .temperature import TemperatureSource
from .web import WebServer


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _configure(args, saved, settings, config_path, interactive: bool, parser):
    """Run the setup questions when needed. Returns the settings to use."""
    if args.setup:
        if not interactive:
            parser.error("--setup needs an interactive terminal")
        updated, copy = config.run_setup(settings)
        if copy and updated.csv_path != settings.csv_path:
            storage.copy_history(settings.csv_path, updated.csv_path)
        _save(config_path, {"csv_path": updated.csv_path, "interval": updated.interval,
                            "unit": updated.unit, "port": updated.port})
        return updated
    if args.csv is None and "csv_path" not in saved and interactive:
        chosen = config.ask_csv_path(settings.csv_path)
        _save(config_path, {"csv_path": chosen})
        return replace(settings, csv_path=chosen)
    return settings


def _save(config_path, values) -> None:
    try:
        config.save_values(config_path, values)
    except OSError as exc:
        _warn(f"Warning: could not save settings to {config_path}: {exc.strerror or exc}")


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = config.build_parser()
    args = parser.parse_args(argv)
    config_path = config.config_file_path(args.config)
    saved, warnings = config.load_saved(config_path)
    for warning in warnings:
        _warn(f"Warning: {warning}")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    try:
        settings = config.resolve_settings(args, saved)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        settings = _configure(args, saved, settings, config_path, interactive, parser)
        storage.prepare_csv(settings.csv_path)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.", file=sys.stderr)
        return 130
    except ValueError as exc:
        _warn(f"Error: {exc}")
        _warn("Choose another location with --csv, or run with --setup.")
        return 1

    engine = Engine(settings, TemperatureSource(settings.sensor), config_path)
    try:
        server = WebServer(engine, settings.port)
    except OSError as exc:
        _warn(f"Error: cannot start the web interface on port {settings.port}: {exc.strerror or exc}")
        _warn("Choose another port with --port.")
        return 1

    stop = threading.Event()
    previous_handler = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    engine.web_url = server.url
    show_dashboard = interactive and not args.no_display
    server.start()
    engine.start()
    try:
        if show_dashboard:
            Dashboard(engine).run(stop)
        else:
            print(f"{APP_NAME} {__version__} is running. CSV: {settings.csv_path}. "
                  f"Interval: {settings.interval} s. Web: {server.url}. Press Ctrl+C to stop.", flush=True)
            run_headless(engine, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.stop()
        engine.stop()
        signal.signal(signal.SIGTERM, previous_handler)
    print("Stopped.")
    return 0

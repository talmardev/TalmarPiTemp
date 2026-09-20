"""Settings: defaults, validation, saved config, command line and setup questions."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import __version__
from .storage import DEFAULT_CSV_NAME, check_csv_path, is_blank_csv, resolve_csv_path
from .temperature import UNIT_CELSIUS, normalize_unit

DEFAULT_INTERVAL = 10
MIN_INTERVAL = 1
MAX_INTERVAL = 86400
DEFAULT_PORT = 5000
SAVED_KEYS = ("csv_path", "interval", "unit", "port")
APP_DIR = Path(__file__).resolve().parent.parent


class SettingsError(ValueError):
    """A setting is invalid. The message is safe to show to users."""


def validate_interval(value: Any) -> int:
    if isinstance(value, bool):
        raise SettingsError("Recording interval must be a whole number of seconds")
    try:
        number = float(str(value).strip())
    except ValueError:
        raise SettingsError("Recording interval must be a whole number of seconds") from None
    if not math.isfinite(number) or number != int(number):
        raise SettingsError("Recording interval must be a whole number of seconds")
    seconds = int(number)
    if seconds < MIN_INTERVAL:
        raise SettingsError(f"Recording interval must be at least {MIN_INTERVAL} second")
    if seconds > MAX_INTERVAL:
        raise SettingsError(f"Recording interval must be at most {MAX_INTERVAL} seconds")
    return seconds


def validate_port(value: Any) -> int:
    try:
        number = float(str(value).strip())
    except ValueError:
        raise SettingsError("Web server port must be a whole number") from None
    if isinstance(value, bool) or not math.isfinite(number) or number != int(number):
        raise SettingsError("Web server port must be a whole number")
    port = int(number)
    if not 1 <= port <= 65535:
        raise SettingsError("Web server port must be between 1 and 65535")
    return port


def validate_unit(value: Any) -> str:
    try:
        return normalize_unit(str(value))
    except ValueError as exc:
        raise SettingsError(str(exc)) from None


def default_csv_path() -> Path:
    return APP_DIR / DEFAULT_CSV_NAME


def config_file_path(explicit: Optional[str] = None,
                     environ: Optional[Mapping[str, str]] = None) -> Path:
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "talmarpitemp" / "config.json"


@dataclass(frozen=True)
class Settings:
    csv_path: Path
    interval: int = DEFAULT_INTERVAL
    unit: str = UNIT_CELSIUS
    port: int = DEFAULT_PORT
    sensor: Optional[str] = None


def _clean_saved(raw: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    checks: Dict[str, Callable[[Any], Any]] = {
        "csv_path": lambda v: str(resolve_csv_path(str(v))),
        "interval": validate_interval,
        "unit": validate_unit,
        "port": validate_port,
    }
    values: Dict[str, Any] = {}
    warnings: List[str] = []
    for key, check in checks.items():
        if key not in raw:
            continue
        try:
            values[key] = check(raw[key])
        except ValueError as exc:
            warnings.append(f"Ignoring saved {key}: {exc}")
    return values, warnings


def load_saved(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """Read the saved config. Problems become warnings, never exceptions."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as exc:
        return {}, [f"Ignoring unreadable config file {path}: {exc}"]
    if not isinstance(raw, dict):
        return {}, [f"Ignoring config file {path}: expected a JSON object"]
    return _clean_saved(raw)


def save_values(path: Path, changes: Mapping[str, Any]) -> None:
    """Merge changes into the saved config with an atomic replace."""
    current, _ = load_saved(path)
    current.update({key: changes[key] for key in SAVED_KEYS if key in changes})
    if "csv_path" in current:
        current["csv_path"] = str(current["csv_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False)
    try:
        with handle:
            json.dump(current, handle, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
    except OSError:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _argument(validator: Callable[[str], Any]) -> Callable[[str], Any]:
    def convert(text: str) -> Any:
        try:
            return validator(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    convert.__name__ = validator.__name__
    return convert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Monitor the Raspberry Pi CPU temperature, record it to a CSV file "
                    "and serve a local web dashboard.")
    parser.add_argument("--csv", metavar="PATH", help="CSV file to record to and read history from")
    parser.add_argument("--interval", metavar="SECONDS", type=_argument(validate_interval),
                        help=f"seconds between recorded readings (default {DEFAULT_INTERVAL})")
    parser.add_argument("--unit", metavar="C|F", type=_argument(validate_unit),
                        help="display unit, Celsius or Fahrenheit (default C)")
    parser.add_argument("--port", metavar="PORT", type=_argument(validate_port),
                        help=f"web server port (default {DEFAULT_PORT})")
    parser.add_argument("--sensor", metavar="PATH",
                        help="file holding the temperature in millidegrees Celsius "
                             "(default: detect automatically)")
    parser.add_argument("--config", metavar="PATH",
                        help="settings file (default: ~/.config/talmarpitemp/config.json)")
    parser.add_argument("--setup", action="store_true",
                        help="ask for the settings interactively and save them")
    parser.add_argument("--no-display", action="store_true",
                        help="do not draw the live terminal dashboard")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def resolve_settings(args: argparse.Namespace, saved: Mapping[str, Any]) -> Settings:
    """Combine defaults, saved values and command line values, in that order."""
    csv_text = args.csv or saved.get("csv_path") or str(default_csv_path())
    return Settings(
        csv_path=resolve_csv_path(csv_text),
        interval=args.interval if args.interval is not None else saved.get("interval", DEFAULT_INTERVAL),
        unit=args.unit if args.unit is not None else saved.get("unit", UNIT_CELSIUS),
        port=args.port if args.port is not None else saved.get("port", DEFAULT_PORT),
        sensor=args.sensor,
    )


def _ask(label: str, default: Any, convert: Callable[[str], Any],
         input_fn: Callable[[str], str], output_fn: Callable[[str], None]) -> Any:
    while True:
        try:
            answer = input_fn(f"{label} [{default}]: ").strip()
        except EOFError:
            raise SettingsError("Setup was cancelled because no input is available") from None
        try:
            return convert(answer or str(default))
        except ValueError as exc:
            output_fn(f"  {exc}")


def ask_csv_path(default: Path, input_fn: Optional[Callable[[str], str]] = None,
                    output_fn: Callable[[str], None] = print) -> Path:
    input_fn = input if input_fn is None else input_fn
    output_fn("No CSV location is configured yet. Press Enter to accept the value in brackets.")
    return _ask("CSV file path", default, check_csv_path, input_fn, output_fn)


def run_setup(current: Settings, input_fn: Optional[Callable[[str], str]] = None,
              output_fn: Callable[[str], None] = print) -> Tuple[Settings, bool]:
    """Ask for every saved setting. Returns the new settings and the copy history choice."""
    input_fn = input if input_fn is None else input_fn
    output_fn("Setup. Press Enter to keep the value in brackets.")
    csv_path = _ask("CSV file path", current.csv_path, check_csv_path, input_fn, output_fn)
    copy_history = False
    if csv_path != current.csv_path and not is_blank_csv(current.csv_path):
        reply = input_fn("Copy existing history to the new file? [y/N]: ")
        copy_history = reply.strip().lower() in ("y", "yes")
    interval = _ask("Recording interval in seconds", current.interval,
                    validate_interval, input_fn, output_fn)
    unit = _ask("Temperature unit (C or F)", current.unit, validate_unit, input_fn, output_fn)
    port = _ask("Web server port", current.port, validate_port, input_fn, output_fn)
    updated = Settings(csv_path=csv_path, interval=interval, unit=unit, port=port,
                       sensor=current.sensor)
    return updated, copy_history

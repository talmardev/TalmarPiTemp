"""CSV file format, path validation and safe appending."""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path, PurePath
from typing import Optional, Tuple

from .temperature import is_plausible

HEADER = "timestamp,temperature_c"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_CSV_NAME = "temperature.csv"

Reading = Tuple[datetime, float]

# Path parts (root included) that make up a mount point under common mount roots.
_MOUNT_DEPTHS = {("media",): 4, ("mnt",): 3, ("run", "media"): 5}
_BLANK_CSV_MAX_BYTES = 64


class CsvPathError(ValueError):
    """The CSV location is unusable. The message is safe to show to users."""


def format_timestamp(moment: datetime) -> str:
    return moment.strftime(TIMESTAMP_FORMAT)


def format_reading(moment: datetime, celsius: float) -> str:
    return f"{format_timestamp(moment)},{celsius:.2f}"


def parse_timestamp(text: str) -> Optional[datetime]:
    text = text.strip().strip('"')
    if len(text) != 19:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_row(line: str) -> Optional[Reading]:
    """Return (timestamp, celsius), or None for headers, blanks and bad rows."""
    stamp, separator, value = line.partition(",")
    if not separator or "," in value:
        return None
    moment = parse_timestamp(stamp)
    if moment is None:
        return None
    try:
        celsius = float(value.strip().strip('"'))
    except ValueError:
        return None
    return (moment, celsius) if is_plausible(celsius) else None


def resolve_csv_path(text: str) -> Path:
    """Turn user input into an absolute path to a .csv file."""
    text = (text or "").strip()
    if not text:
        raise CsvPathError("CSV path must not be empty")
    if "\x00" in text:
        raise CsvPathError("CSV path contains an invalid character")
    path = Path(os.path.abspath(os.path.expanduser(text)))
    if text.endswith(("/", os.sep)) or path.is_dir():
        path = path / DEFAULT_CSV_NAME
    if path.suffix.lower() != ".csv":
        raise CsvPathError("CSV file name must end with .csv")
    return path


def _mount_point(path: PurePath) -> Optional[PurePath]:
    parts = path.parts
    if not parts or parts[0] != "/":
        return None
    for prefix, depth in _MOUNT_DEPTHS.items():
        if parts[1:1 + len(prefix)] == prefix and len(parts) > depth:
            return type(path)(*parts[:depth])
    return None


def _nearest_existing_dir(path: Path) -> Optional[Path]:
    for parent in path.parents:
        if parent.exists():
            return parent if parent.is_dir() else None
    return None


def _first_line(path: Path) -> str:
    with open(path, "rb") as handle:
        return handle.readline().decode("utf-8", "replace").lstrip("﻿").strip()


def validate_csv_target(path: Path) -> None:
    """Check a location without changing anything. Raises CsvPathError."""
    mount = _mount_point(path)
    if mount is not None and not Path(mount).is_dir():
        raise CsvPathError(f"{mount} does not exist. Connect and mount the drive first.")
    if path.exists():
        if not path.is_file():
            raise CsvPathError(f"{path} is not a regular file")
        if not os.access(path, os.W_OK):
            raise CsvPathError(f"{path} is not writable")
        if path.stat().st_size > 0 and _first_line(path) != HEADER:
            raise CsvPathError(
                f"{path} already exists and is not a TalmarPiTemp CSV (expected header {HEADER})")
        return
    parent = _nearest_existing_dir(path)
    if parent is None:
        raise CsvPathError(f"Cannot create {path.parent}: a path component is not a directory")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise CsvPathError(f"{parent} is not writable")


def check_csv_path(text: str) -> Path:
    """Resolve and validate user input without creating anything."""
    path = resolve_csv_path(text)
    validate_csv_target(path)
    return path


def prepare_csv(path: Path) -> None:
    """Create parent folders and the header, then prove the file is writable."""
    validate_csv_target(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write((HEADER + "\n").encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        raise CsvPathError(f"Cannot write to {path}: {exc.strerror or exc}") from exc


def append_reading(path: Path, moment: datetime, celsius: float) -> None:
    """Append one row, or leave the file exactly as it was and raise OSError.

    A last line without a newline (foreign or hand edited file) is closed first.
    A failed or partial write, for example on a full disk, is rolled back so no
    truncated row can be mistaken for a real reading later.
    """
    line = (format_reading(moment, celsius) + "\n").encode("ascii")
    with open(path, "a+b", buffering=0) as handle:
        end = handle.seek(0, os.SEEK_END)
        if end > 0:
            handle.seek(end - 1)
            if handle.read(1) not in (b"\n", b"\r"):
                line = b"\n" + line
        handle.seek(0, os.SEEK_END)
        try:
            if handle.write(line) != len(line):
                raise OSError(errno.ENOSPC, "Partial write")
            os.fsync(handle.fileno())
        except OSError:
            try:
                handle.truncate(end)
            except OSError:
                pass
            raise


def is_blank_csv(path: Path) -> bool:
    """True for a missing file, an empty file or a file holding only the header."""
    try:
        size = path.stat().st_size
        if size == 0:
            return True
        if size > _BLANK_CSV_MAX_BYTES:
            return False
        return path.read_bytes().decode("utf-8", "replace").lstrip("﻿").strip() == HEADER
    except FileNotFoundError:
        return True


def copy_history(source: Path, target: Path) -> bool:
    """Copy a CSV into a blank target. The source is never modified.

    Returns False when there was nothing to copy.
    """
    if not source.is_file() or is_blank_csv(source):
        return False
    if target.exists() and source.resolve() == target.resolve():
        raise CsvPathError("Source and target are the same file")
    if not is_blank_csv(target):
        raise CsvPathError(f"{target} already contains data, so history cannot be copied into it")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False)
    try:
        with handle, open(source, "rb") as reader:
            shutil.copyfileobj(reader, handle, 1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except OSError as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise CsvPathError(f"Cannot copy history to {target}: {exc.strerror or exc}") from exc
    return True

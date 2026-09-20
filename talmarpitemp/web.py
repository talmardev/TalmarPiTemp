"""Local web dashboard: a small threaded HTTP server with a JSON API.

The server only listens on the loopback address. Requests must carry a loopback
Host header and, for changes, a JSON content type and a loopback Origin, which
blocks DNS rebinding and cross site form posts from other pages in the browser.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

from . import config
from .engine import Engine, Snapshot
from .stats import wall_seconds
from .temperature import UNIT_NAMES, from_celsius, unit_symbol

BIND_HOST = "127.0.0.1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 8 * 1024
DEFAULT_POINTS = 10000
MIN_POINTS = 50
MAX_POINTS = 20000
ALLOWED_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
SETTING_KEYS = {"interval", "unit", "csv_path", "copy_history"}
PAGES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/chart.js": ("chart.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
RANGES = {
    "1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24),
    "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90),
    "1y": timedelta(days=365),
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
}


class RequestError(ValueError):
    """A bad request. The message is shown to the user."""


def _hostname(value: str) -> Optional[str]:
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


def host_allowed(host_header: str) -> bool:
    return _hostname(host_header or "") in ALLOWED_HOSTNAMES


def origin_allowed(origin: Optional[str]) -> bool:
    if origin is None:
        return True
    parts = urlsplit(origin)
    return parts.scheme == "http" and parts.hostname in ALLOWED_HOSTNAMES


def _number(value: Optional[float], unit: str) -> Optional[float]:
    return None if value is None else round(from_celsius(value, unit), 2)


def _time(seconds: float) -> float:
    return int(seconds) if seconds == int(seconds) else round(seconds, 1)


def _stamp(moment: Optional[datetime]) -> Optional[str]:
    return None if moment is None else moment.strftime("%Y-%m-%d %H:%M:%S")


def state_payload(snap: Snapshot) -> Dict[str, Any]:
    unit = snap.unit
    summary = snap.summary
    return {
        "now": _stamp(snap.now),
        "status": snap.status,
        "unit": unit,
        "unit_name": UNIT_NAMES[unit],
        "unit_symbol": unit_symbol(unit),
        "temperature": _number(snap.celsius, unit),
        "sensor_error": snap.sensor_error,
        "record_error": snap.record_error,
        "history_error": snap.history_error,
        "summary": None if summary is None else {
            "maximum": _number(summary.maximum, unit),
            "minimum": _number(summary.minimum, unit),
            "average": _number(summary.average, unit),
            "count_24h": summary.count_24h,
            "count_7d": summary.count_7d,
        },
        "interval": snap.interval,
        "csv_path": str(snap.csv_path),
        "last_recorded": _stamp(snap.last_recorded),
        "uptime_seconds": int(snap.uptime_seconds),
        "source": snap.source,
        "hostname": snap.hostname,
        "limits": {"min_interval": config.MIN_INTERVAL, "max_interval": config.MAX_INTERVAL},
    }


def _query_value(query: Mapping[str, List[str]], name: str, default: Optional[str] = None) -> Optional[str]:
    values = query.get(name)
    return values[0] if values else default


def _query_time(text: Optional[str], name: str, end_of_day: bool = False) -> datetime:
    if not text:
        raise RequestError(f"Missing {name} time")
    try:
        moment = datetime.fromisoformat(text.strip().replace(" ", "T"))
    except ValueError:
        raise RequestError(f"Invalid {name} time. Use YYYY-MM-DD HH:MM") from None
    if end_of_day and len(text.strip()) == 10:
        moment += timedelta(days=1) - timedelta(seconds=1)
    return moment


def chart_payload(engine: Engine, query: Mapping[str, List[str]]) -> Dict[str, Any]:
    unit = engine.settings.unit
    now = engine.now()
    name = _query_value(query, "range", "24h")
    if name in RANGES:
        start, end = now - RANGES[name], now
    elif name == "all":
        first = engine.first_reading_time()
        start, end = (first if first is not None and first < now else now - RANGES["1h"]), now
    elif name == "custom":
        start = _query_time(_query_value(query, "from"), "start")
        end = _query_time(_query_value(query, "to"), "end", end_of_day=True)
    else:
        raise RequestError("Unknown range")
    if start >= end:
        raise RequestError("The start time must be before the end time")
    try:
        points = int(_query_value(query, "points", str(DEFAULT_POINTS)))
    except ValueError:
        raise RequestError("Invalid points value") from None
    data = engine.chart_data(start, end, min(max(points, MIN_POINTS), MAX_POINTS))

    def extreme(item):
        return None if item is None else [_time(item[0]), _number(item[1], unit)]

    return {
        "unit": unit,
        "unit_symbol": unit_symbol(unit),
        "range": name,
        "start": _time(wall_seconds(start)),
        "end": _time(wall_seconds(end)),
        "bucket_seconds": round(data.bucket_seconds, 2),
        "count": data.count,
        "points": [[_time(t), _number(avg, unit), _number(low, unit), _number(high, unit), n]
                   for t, avg, low, high, n in data.points],
        "minimum": extreme(data.minimum),
        "maximum": extreme(data.maximum),
    }


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TalmarPiTemp"
        sys_version = ""
        timeout = 10

        def log_message(self, format, *args):
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Dict[str, Any]) -> None:
            self._send(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"ok": False, "error": message})

        def _guard(self, changing: bool = False) -> bool:
            if not host_allowed(self.headers.get("Host", "")):
                self._error(403, "Forbidden host")
                return False
            if changing and not origin_allowed(self.headers.get("Origin")):
                self._error(403, "Forbidden origin")
                return False
            return True

        def _static(self, path: str) -> None:
            name, content_type = PAGES[path]
            self._send(200, (STATIC_DIR / name).read_bytes(), content_type)

        def do_GET(self) -> None:
            if not self._guard():
                return
            parts = urlsplit(self.path)
            try:
                if parts.path in PAGES:
                    self._static(parts.path)
                elif parts.path == "/api/state":
                    self._json(200, state_payload(engine.snapshot()))
                elif parts.path == "/api/chart":
                    self._json(200, chart_payload(engine, parse_qs(parts.query)))
                else:
                    self._error(404, "Not found")
            except RequestError as exc:
                self._error(400, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._error(500, "Internal error")

        def do_POST(self) -> None:
            if not self._guard(changing=True):
                return
            try:
                if urlsplit(self.path).path != "/api/config":
                    self._error(404, "Not found")
                elif self.headers.get_content_type() != "application/json":
                    self._error(415, "Content type must be application/json")
                else:
                    self._change_config()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._error(500, "Internal error")

        def _change_config(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY_BYTES:
                self._error(400, "Missing or oversized request body")
                return
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._error(400, "Body is not valid JSON")
                return
            if not isinstance(body, dict):
                self._error(400, "Body must be a JSON object")
                return
            unknown = sorted(set(body) - SETTING_KEYS)
            if unknown:
                self._error(400, f"Unknown setting: {unknown[0]}")
                return
            if not isinstance(body.get("copy_history", False), bool):
                self._error(400, "copy_history must be true or false")
                return
            try:
                result = engine.apply_changes(body)
            except ValueError as exc:
                self._error(400, str(exc))
                return
            except OSError as exc:
                self._error(400, f"File system error: {exc.strerror or exc}")
                return
            self._json(200, {"ok": True, "warnings": result["warnings"],
                             "state": state_payload(engine.snapshot())})

    return Handler


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


class WebServer:
    """Runs the HTTP server on its own thread. Raises OSError if the port is busy."""

    def __init__(self, engine: Engine, port: int) -> None:
        self._httpd = DashboardServer((BIND_HOST, port), make_handler(engine))
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="talmarpitemp-web", daemon=True)
        self._started = False

    @property
    def port(self) -> int:
        return self._httpd.server_port

    @property
    def url(self) -> str:
        return f"http://{BIND_HOST}:{self.port}"

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        if self._started:
            self._httpd.shutdown()
            self._thread.join(3.0)
        self._httpd.server_close()

import http.client
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from talmarpitemp import config, engine, storage, web
from tests.test_engine import START, FakeSource, FakeTime


class HostAndOriginTests(unittest.TestCase):
    def test_host_allowed(self):
        for host in ("localhost", "localhost:5000", "127.0.0.1:8080", "[::1]:5000", "LOCALHOST"):
            self.assertTrue(web.host_allowed(host), host)
        for host in ("", "evil.example.com", "evil.example.com:5000", "127.0.0.1.evil.com",
                     "192.168.1.5:5000", "localhost.evil.com"):
            self.assertFalse(web.host_allowed(host), host)

    def test_origin_allowed(self):
        self.assertTrue(web.origin_allowed(None))
        self.assertTrue(web.origin_allowed("http://localhost:5000"))
        self.assertTrue(web.origin_allowed("http://127.0.0.1:9999"))
        for origin in ("http://evil.example.com", "https://localhost", "null", "http://localhost.evil.com"):
            self.assertFalse(web.origin_allowed(origin), origin)


class WebCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.csv = self.root / "t.csv"
        storage.prepare_csv(self.csv)
        with open(self.csv, "a") as handle:
            for minutes, celsius in ((180, 40.0), (120, 60.0), (60, 50.0), (10, 45.0)):
                moment = START - timedelta(minutes=minutes)
                handle.write(storage.format_reading(moment, celsius) + "\n")
        self.time = FakeTime()
        self.engine = engine.Engine(config.Settings(csv_path=self.csv), FakeSource(48.7),
                                    config_path=self.root / "conf.json", clock=self.time.clock,
                                    monotonic=self.time.monotonic)
        self.engine.sample_once()
        self.engine._history.refresh(self.csv, START)
        self.server = web.WebServer(self.engine, 0)
        self.server.start()
        self.addCleanup(self.server.stop)

    def request(self, method, path, body=None, headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            all_headers = dict(headers or {})
            if host is not None:
                all_headers["Host"] = host
            payload = json.dumps(body) if isinstance(body, (dict, list)) else body
            if isinstance(body, (dict, list)):
                all_headers.setdefault("Content-Type", "application/json")
            connection.request(method, path, body=payload, headers=all_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, response.getheader("Content-Type"), data
        finally:
            connection.close()

    def get_json(self, path):
        status, _, data = self.request("GET", path)
        return status, json.loads(data)

    def post_config(self, body, **kwargs):
        status, _, data = self.request("POST", "/api/config", body, **kwargs)
        return status, json.loads(data)


class StaticTests(WebCase):
    def test_pages_and_content_types(self):
        for path, content_type in (("/", "text/html"), ("/style.css", "text/css"),
                                   ("/app.js", "text/javascript"), ("/chart.js", "text/javascript")):
            status, header, data = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(header.startswith(content_type), path)
            self.assertGreater(len(data), 100)
        self.assertIn(b"TalmarPiTemp", self.request("GET", "/")[2])

    def test_unknown_paths_and_traversal_are_not_found(self):
        for path in ("/nope", "/../monitor.py", "/static/../cli.py", "/%2e%2e/monitor.py", "/index.html"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_security_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/api/state")
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))

    def test_server_only_listens_on_loopback(self):
        self.assertEqual(self.server._httpd.server_address[0], "127.0.0.1")

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.request("GET", "/api/state", host="evil.example.com")[0], 403)
        self.assertEqual(self.request("GET", "/", host="192.168.1.5:5000")[0], 403)
        self.assertEqual(self.request("GET", "/api/state", host="localhost:9999")[0], 200)


class StateTests(WebCase):
    def test_state_in_celsius(self):
        status, state = self.get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual((state["unit"], state["unit_symbol"], state["temperature"]), ("C", "°C", 48.7))
        self.assertEqual(state["status"], "Running")
        self.assertEqual(state["summary"]["maximum"], 60.0)
        self.assertEqual(state["summary"]["minimum"], 40.0)
        self.assertEqual(state["summary"]["count_7d"], 4)
        self.assertEqual(state["interval"], 10)
        self.assertEqual(state["csv_path"], str(self.csv))
        for key in ("now", "uptime_seconds", "source", "hostname", "last_recorded", "limits"):
            self.assertIn(key, state)

    def test_state_is_converted_to_fahrenheit_but_the_csv_is_not(self):
        before = self.csv.read_bytes()
        status, body = self.post_config({"unit": "F"})
        self.assertEqual(status, 200)
        state = body["state"]
        self.assertEqual((state["unit"], state["unit_symbol"]), ("F", "°F"))
        self.assertEqual(state["temperature"], 119.66)
        self.assertEqual(state["summary"]["maximum"], 140.0)
        self.assertEqual(state["summary"]["minimum"], 104.0)
        self.assertEqual(self.csv.read_bytes(), before)

    def test_sensor_error_is_reported(self):
        eng = engine.Engine(config.Settings(csv_path=self.csv),
                            FakeSource(RuntimeError("no sensor")), clock=self.time.clock,
                            monotonic=self.time.monotonic)
        eng.sample_once()
        payload = web.state_payload(eng.snapshot())
        self.assertEqual((payload["status"], payload["temperature"]), ("Sensor error", None))
        self.assertIn("no sensor", payload["sensor_error"])


class ChartTests(WebCase):
    def test_24h_chart_reads_the_csv(self):
        status, chart = self.get_json("/api/chart?range=24h")
        self.assertEqual(status, 200)
        self.assertEqual(chart["count"], 4)
        self.assertEqual([p[1] for p in chart["points"]], [40.0, 60.0, 50.0, 45.0])
        self.assertEqual(chart["maximum"][1], 60.0)
        self.assertEqual(chart["minimum"][1], 40.0)
        self.assertEqual(chart["unit_symbol"], "°C")

    def test_chart_values_follow_the_unit(self):
        self.post_config({"unit": "F"})
        chart = self.get_json("/api/chart?range=7d")[1]
        self.assertEqual([p[1] for p in chart["points"]], [104.0, 140.0, 122.0, 113.0])
        self.assertEqual((chart["minimum"][1], chart["maximum"][1]), (104.0, 140.0))

    def test_all_range_and_custom_range(self):
        self.assertEqual(self.get_json("/api/chart?range=all")[1]["count"], 4)
        low = (START - timedelta(minutes=130)).strftime("%Y-%m-%dT%H:%M:%S")
        high = (START - timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S").replace(" ", "%20")
        chart = self.get_json(f"/api/chart?range=custom&from={low}&to={high}")[1]
        self.assertEqual([p[1] for p in chart["points"]], [60.0, 50.0])

    def test_old_ranges_are_read_from_the_file(self):
        far = (START - timedelta(days=30)).strftime("%Y-%m-%d")
        chart = self.get_json(f"/api/chart?range=custom&from={far}&to={far}")[1]
        self.assertEqual((chart["count"], chart["points"], chart["minimum"]), (0, [], None))

    def test_invalid_requests(self):
        for query in ("range=bogus", "range=custom", "range=custom&from=x&to=y",
                      "range=custom&from=2026-09-20T12:00&to=2026-09-20T11:00", "range=24h&points=abc"):
            status, body = self.get_json("/api/chart?" + query)
            self.assertEqual(status, 400, query)
            self.assertFalse(body["ok"])


class ConfigTests(WebCase):
    def test_interval_change_is_applied_and_saved(self):
        status, body = self.post_config({"interval": 30})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"]["interval"], 30)
        self.assertEqual(json.loads((self.root / "conf.json").read_text())["interval"], 30)
        self.assertEqual(self.post_config({"interval": "45"})[1]["state"]["interval"], 45)

    def test_invalid_values_are_rejected_without_changes(self):
        for body in ({"interval": 0}, {"interval": -5}, {"interval": "abc"}, {"interval": 1.5},
                     {"interval": 99999999}, {"unit": "K"}, {"csv_path": "notes.txt"}, {"csv_path": ""},
                     {"bogus": 1}, {"copy_history": "yes"}, {"interval": None}):
            status, payload = self.post_config(body)
            self.assertEqual(status, 400, body)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["error"])
        self.assertEqual(self.get_json("/api/state")[1]["interval"], 10)

    def test_bad_bodies(self):
        self.assertEqual(self.request("POST", "/api/config", b"{not json", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/config", "[1]", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/config", b"", {"Content-Type": "application/json"})[0], 400)
        big = json.dumps({"csv_path": "x" * 20000})
        self.assertEqual(self.request("POST", "/api/config", big, {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/other", {"a": 1})[0], 404)

    def test_wrong_content_type_and_foreign_origin_are_refused(self):
        self.assertEqual(self.request("POST", "/api/config", '{"interval": 5}',
                                      {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post_config({"interval": 5}, headers={"Origin": "http://evil.example.com"})[0], 403)
        self.assertEqual(self.post_config({"interval": 5}, host="evil.example.com")[0], 403)
        self.assertEqual(self.get_json("/api/state")[1]["interval"], 10)
        self.assertEqual(self.post_config({"interval": 5}, headers={"Origin": "http://localhost:5000"})[0], 200)

    def test_csv_switch_keeps_the_old_file_and_can_copy(self):
        before = self.csv.read_bytes()
        target = self.root / "sub" / "new.csv"
        status, body = self.post_config({"csv_path": str(target)})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"]["csv_path"], str(target))
        self.assertEqual(target.read_text(), storage.HEADER + "\n")
        self.assertEqual(self.csv.read_bytes(), before)
        copied = self.root / "copied.csv"
        self.post_config({"csv_path": str(self.csv)})
        status, body = self.post_config({"csv_path": str(copied), "copy_history": True})
        self.assertEqual(status, 200)
        self.assertEqual(copied.read_bytes(), before)

    def test_csv_switch_to_a_foreign_file_is_refused(self):
        foreign = self.root / "foreign.csv"
        foreign.write_text("a,b\n1,2\n")
        status, body = self.post_config({"csv_path": str(foreign)})
        self.assertEqual(status, 400)
        self.assertIn("not a TalmarPiTemp CSV", body["error"])
        self.assertEqual(foreign.read_text(), "a,b\n1,2\n")


class LifecycleTests(unittest.TestCase):
    def test_port_in_use_is_reported_and_stop_releases_the_port(self):
        with tempfile.TemporaryDirectory() as folder:
            csv = Path(folder) / "t.csv"
            storage.prepare_csv(csv)
            eng = engine.Engine(config.Settings(csv_path=csv), FakeSource(50.0))
            first = web.WebServer(eng, 0)
            first.start()
            port = first.port
            with self.assertRaises(OSError):
                web.WebServer(eng, port)
            first.stop()
            second = web.WebServer(eng, port)
            second.start()
            second.stop()

    def test_stop_before_start_is_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            csv = Path(folder) / "t.csv"
            storage.prepare_csv(csv)
            web.WebServer(engine.Engine(config.Settings(csv_path=csv), FakeSource(50.0)), 0).stop()


if __name__ == "__main__":
    unittest.main()

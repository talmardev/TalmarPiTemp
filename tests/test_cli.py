import _thread
import contextlib
import io
import json
import signal
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from talmarpitemp import cli, storage


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class CliCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.sensor = self.root / "sensor.txt"
        self.sensor.write_text("50000")
        self.csv = self.root / "data" / "t.csv"
        self.conf = self.root / "conf.json"
        self.port = free_port()

    def argv(self, *extra):
        return ["--csv", str(self.csv), "--sensor", str(self.sensor), "--interval", "1",
                "--port", str(self.port), "--config", str(self.conf), *extra]

    def run_main(self, argv, stop, stdin=None, delay=2.5):
        seen = {}

        def trigger():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/state", timeout=3) as reply:
                    seen["state"] = json.loads(reply.read())
            except OSError as exc:
                seen["error"] = exc
            stop()

        out, err = io.StringIO(), io.StringIO()
        timer = threading.Timer(delay, trigger)
        timer.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch("sys.stdin", stdin or io.StringIO()):
                code = cli.main(argv)
        finally:
            timer.cancel()
        return code, out.getvalue(), err.getvalue(), seen

    def assert_clean_shutdown(self):
        leftovers = [t.name for t in threading.enumerate() if t.name.startswith("talmarpitemp")]
        self.assertEqual(leftovers, [])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class RunTests(CliCase):
    def test_sigterm_stops_cleanly_after_recording(self):
        code, out, err, seen = self.run_main(self.argv(), lambda: signal.raise_signal(signal.SIGTERM))
        self.assertEqual(code, 0)
        self.assertIn("is running", out)
        self.assertTrue(out.rstrip().endswith("Stopped."))
        self.assertEqual(seen["state"]["temperature"], 50.0)
        self.assertEqual(seen["state"]["csv_path"], str(self.csv))
        rows = [storage.parse_row(line) for line in self.csv.read_text().splitlines()[1:]]
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(row and row[1] == 50.0 for row in rows))
        self.assert_clean_shutdown()

    def test_keyboard_interrupt_stops_cleanly(self):
        code, out, err, seen = self.run_main(self.argv("--interval", "2"), _thread.interrupt_main)
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("Stopped."))
        self.assertNotIn("Traceback", err)
        self.assert_clean_shutdown()

    def test_settings_flags_reach_the_engine_without_being_saved(self):
        code, out, err, seen = self.run_main(self.argv("--unit", "F", "--interval", "7"),
                                             lambda: signal.raise_signal(signal.SIGTERM))
        self.assertEqual(code, 0)
        self.assertEqual((seen["state"]["unit"], seen["state"]["interval"]), ("F", 7))
        self.assertEqual(seen["state"]["temperature"], 122.0)
        self.assertFalse(self.conf.exists())


class StartupFailureTests(CliCase):
    def run_failing(self, argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", stdin or io.StringIO()):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_invalid_arguments_exit_with_usage_errors(self):
        for extra in (["--interval", "0"], ["--interval", "-1"], ["--port", "70000"], ["--unit", "K"],
                      ["--csv", str(self.root / "data.txt")]):
            code, out, err = self.run_failing(self.argv(*extra))
            self.assertEqual(code, 2, extra)
            self.assertIn("error", err)

    def test_unusable_csv_reports_a_clear_error(self):
        foreign = self.root / "foreign.csv"
        foreign.write_text("a,b\n1,2\n")
        code, out, err = self.run_failing(["--csv", str(foreign), "--config", str(self.conf),
                                           "--port", str(self.port), "--sensor", str(self.sensor)])
        self.assertEqual(code, 1)
        self.assertIn("not a TalmarPiTemp CSV", err)
        self.assertIn("--csv", err)
        self.assertEqual(foreign.read_text(), "a,b\n1,2\n")

    def test_port_in_use_reports_a_clear_error(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            code, out, err = self.run_failing(self.argv("--port", str(port)))
        self.assertEqual(code, 1)
        self.assertIn(f"port {port}", err)
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("talmarpitemp")], [])

    def test_setup_needs_a_terminal(self):
        code, out, err = self.run_failing(self.argv("--setup"))
        self.assertEqual(code, 2)
        self.assertIn("interactive terminal", err)

    def test_corrupt_config_file_is_only_a_warning(self):
        self.conf.write_text("{broken")
        stop = lambda: signal.raise_signal(signal.SIGTERM)
        code, out, err, seen = self.run_main(self.argv(), stop, delay=1.5)
        self.assertEqual(code, 0)
        self.assertIn("Warning: Ignoring unreadable config file", err)


class FirstRunTests(CliCase):
    def test_asks_for_the_csv_once_and_saves_it(self):
        argv = ["--sensor", str(self.sensor), "--interval", "1", "--port", str(self.port),
                "--config", str(self.conf), "--no-display"]
        terminal = FakeTerminal()
        out = FakeTerminal()
        with mock.patch("builtins.input", return_value=str(self.csv)) as ask:
            timer = threading.Timer(1.5, lambda: signal.raise_signal(signal.SIGTERM))
            timer.start()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch("sys.stdin", terminal):
                code = cli.main(argv)
        self.assertEqual(code, 0)
        ask.assert_called_once()
        self.assertEqual(json.loads(self.conf.read_text()), {"csv_path": str(self.csv)})
        self.assertTrue(self.csv.is_file())

        with mock.patch("builtins.input", side_effect=AssertionError("must not ask again")):
            timer = threading.Timer(1.5, lambda: signal.raise_signal(signal.SIGTERM))
            timer.start()
            with contextlib.redirect_stdout(FakeTerminal()), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch("sys.stdin", FakeTerminal()):
                self.assertEqual(cli.main(argv), 0)

    def test_cancelled_question_exits_without_starting(self):
        argv = ["--sensor", str(self.sensor), "--port", str(self.port), "--config", str(self.conf)]
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stdout(FakeTerminal()), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch("sys.stdin", FakeTerminal()):
                self.assertEqual(cli.main(argv), 130)
        self.assertFalse(self.conf.exists())


if __name__ == "__main__":
    unittest.main()

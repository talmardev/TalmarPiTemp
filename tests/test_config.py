import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from talmarpitemp import config as c
from talmarpitemp import storage


class ValidationTests(unittest.TestCase):
    def test_interval_accepts_whole_seconds_in_range(self):
        for value, expected in ((10, 10), ("30", 30), (" 60 ", 60), ("10.0", 10),
                                (1, 1), (c.MAX_INTERVAL, c.MAX_INTERVAL)):
            self.assertEqual(c.validate_interval(value), expected)

    def test_interval_rejects_zero_negative_and_junk(self):
        for value in (0, -1, -30, "0", "-5", "abc", "", "2.5", "nan", "inf", None, True,
                      c.MAX_INTERVAL + 1):
            with self.assertRaises(c.SettingsError, msg=repr(value)):
                c.validate_interval(value)

    def test_port_range(self):
        for value, expected in ((5000, 5000), ("8080", 8080), (1, 1), (65535, 65535)):
            self.assertEqual(c.validate_port(value), expected)
        for value in (0, -1, 65536, "abc", "80.5", "", None, "nan"):
            with self.assertRaises(c.SettingsError, msg=repr(value)):
                c.validate_port(value)

    def test_unit(self):
        self.assertEqual(c.validate_unit("f"), "F")
        self.assertEqual(c.validate_unit("Celsius"), "C")
        with self.assertRaises(c.SettingsError):
            c.validate_unit("kelvin")


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.path = self.root / "conf" / "config.json"

    def test_config_file_path_precedence(self):
        self.assertEqual(c.config_file_path(str(self.path), {}), self.path)
        self.assertEqual(c.config_file_path(None, {"XDG_CONFIG_HOME": str(self.root)}),
                         self.root / "talmarpitemp" / "config.json")
        self.assertEqual(c.config_file_path(None, {}).name, "config.json")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(c.load_saved(self.path), ({}, []))

    def test_corrupt_file_becomes_a_warning(self):
        self.path.parent.mkdir()
        self.path.write_text("{not json")
        values, warnings = c.load_saved(self.path)
        self.assertEqual(values, {})
        self.assertEqual(len(warnings), 1)
        self.path.write_text("[1, 2]")
        self.assertEqual(c.load_saved(self.path)[0], {})

    def test_invalid_entries_are_dropped_with_warnings(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"interval": 0, "unit": "F", "port": "x",
                                         "csv_path": str(self.root / "a.csv"), "other": 1}))
        values, warnings = c.load_saved(self.path)
        self.assertEqual(values, {"unit": "F", "csv_path": str(self.root / "a.csv")})
        self.assertEqual(len(warnings), 2)

    def test_save_merges_and_creates_folders(self):
        c.save_values(self.path, {"interval": 30, "csv_path": self.root / "a.csv", "bogus": 1})
        c.save_values(self.path, {"unit": "F"})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved, {"interval": 30, "csv_path": str(self.root / "a.csv"), "unit": "F"})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.json"])


class ParserTests(unittest.TestCase):
    def parse(self, *argv):
        return c.build_parser().parse_args(list(argv))

    def test_defaults_are_unset(self):
        args = self.parse()
        self.assertIsNone(args.csv)
        self.assertIsNone(args.interval)
        self.assertIsNone(args.unit)
        self.assertIsNone(args.port)
        self.assertFalse(args.setup)

    def test_full_command_line(self):
        args = self.parse("--interval", "60", "--unit", "c", "--csv", "/x/y.csv", "--port", "8080")
        self.assertEqual((args.interval, args.unit, args.csv, args.port), (60, "C", "/x/y.csv", 8080))

    def test_invalid_arguments_exit_cleanly(self):
        for argv in (["--interval", "0"], ["--interval", "-3"], ["--interval", "x"],
                     ["--unit", "k"], ["--port", "70000"], ["--port", "0"], ["--nope"]):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as ctx:
                    self.parse(*argv)
            self.assertEqual(ctx.exception.code, 2, argv)
            self.assertIn("error", err.getvalue())


class ResolveSettingsTests(unittest.TestCase):
    def parse(self, *argv):
        return c.build_parser().parse_args(list(argv))

    def test_defaults(self):
        settings = c.resolve_settings(self.parse(), {})
        self.assertEqual((settings.interval, settings.unit, settings.port), (10, "C", 5000))
        self.assertEqual(settings.csv_path, c.default_csv_path())

    def test_saved_values_beat_defaults_and_flags_beat_saved(self):
        saved = {"interval": 30, "unit": "F", "port": 6000, "csv_path": "/saved/t.csv"}
        settings = c.resolve_settings(self.parse(), saved)
        self.assertEqual((settings.interval, settings.unit, settings.port), (30, "F", 6000))
        settings = c.resolve_settings(self.parse("--interval", "5", "--unit", "C", "--port", "7000",
                                                 "--csv", "/flag/t.csv"), saved)
        self.assertEqual((settings.interval, settings.unit, settings.port), (5, "C", 7000))
        self.assertEqual(settings.csv_path.name, "t.csv")
        self.assertIn("flag", settings.csv_path.parts)

    def test_bad_csv_flag_is_a_settings_problem(self):
        with self.assertRaises(ValueError):
            c.resolve_settings(self.parse("--csv", "notes.txt"), {})


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.output = []

    def answers(self, *replies):
        queue = list(replies)
        return lambda label: queue.pop(0)

    def test_first_run_enter_accepts_the_default(self):
        default = self.root / "t.csv"
        chosen = c.ask_csv_path(default, self.answers(""), self.output.append)
        self.assertEqual(chosen, default)

    def test_first_run_asks_again_after_invalid_input(self):
        good = self.root / "ok" / "t.csv"
        chosen = c.ask_csv_path(self.root / "d.csv", self.answers("bad.txt", str(good)),
                                self.output.append)
        self.assertEqual(chosen, good)
        self.assertTrue(any(".csv" in line for line in self.output))

    def test_cancelled_input_is_a_clear_error(self):
        def closed(label):
            raise EOFError
        with self.assertRaises(c.SettingsError):
            c.ask_csv_path(self.root / "t.csv", closed, self.output.append)

    def test_setup_asks_for_everything_and_offers_to_copy(self):
        old = self.root / "old.csv"
        storage.prepare_csv(old)
        storage.append_reading(old, datetime(2026, 9, 20, 12, 0, 0), 45.0)
        current = c.Settings(csv_path=old)
        new = self.root / "new.csv"
        settings, copy = c.run_setup(
            current, self.answers(str(new), "y", "0", "15", "f", "70000", "8080"), self.output.append)
        self.assertTrue(copy)
        self.assertEqual((settings.csv_path, settings.interval, settings.unit, settings.port),
                         (new, 15, "F", 8080))

    def test_setup_does_not_offer_copy_for_an_unchanged_or_empty_file(self):
        current = c.Settings(csv_path=self.root / "t.csv")
        settings, copy = c.run_setup(current, self.answers("", "", "", ""), self.output.append)
        self.assertFalse(copy)
        self.assertEqual(settings, current)


if __name__ == "__main__":
    unittest.main()

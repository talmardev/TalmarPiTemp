import errno
import tempfile
import unittest
from datetime import datetime
from pathlib import Path, PurePosixPath
from unittest import mock

from talmarpitemp import storage as s


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path


class ParseRowTests(unittest.TestCase):
    def test_valid_row(self):
        row = s.parse_row("2026-09-20 12:00:10,46.10")
        self.assertEqual(row, (datetime(2026, 9, 20, 12, 0, 10), 46.10))

    def test_tolerates_quotes_spaces_and_line_endings(self):
        expected = (datetime(2026, 9, 20, 12, 0, 10), 46.1)
        self.assertEqual(s.parse_row('"2026-09-20 12:00:10", "46.1"\r\n'), expected)
        self.assertEqual(s.parse_row(" 2026-09-20 12:00:10 , 46.1 \n"), expected)

    def test_invalid_rows_are_skipped(self):
        bad = ["", "   ", s.HEADER, "garbage", "2026-09-20 12:00:10", "2026-09-20 12:00:10,",
               "2026-09-20 12:00:10,abc", "2026-09-20 12:00:10,46.1,extra",
               "2026-13-45 99:00:00,46.1", "20-09-2026 12:00:10,46.1",
               "2026-09-20 12:00:10,nan", "2026-09-20 12:00:10,inf",
               "2026-09-20 12:00:10,9999", "2026-09-20 12:00:10,-90", "\x00\x00,\x00"]
        for line in bad:
            self.assertIsNone(s.parse_row(line), line)

    def test_format_round_trip_uses_two_decimals(self):
        moment = datetime(2026, 9, 20, 1, 2, 3)
        line = s.format_reading(moment, 45.2)
        self.assertEqual(line, "2026-09-20 01:02:03,45.20")
        self.assertEqual(s.parse_row(line), (moment, 45.2))


class ResolvePathTests(TempDirCase):
    def test_rejects_empty_and_wrong_extension(self):
        for text in ("", "   ", "data.txt", "data"):
            with self.assertRaises(s.CsvPathError):
                s.resolve_csv_path(text)

    def test_accepts_any_case_extension_and_absolute_result(self):
        path = s.resolve_csv_path(str(self.root / "T.CSV"))
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "T.CSV")

    def test_directory_gets_default_file_name(self):
        self.assertEqual(s.resolve_csv_path(str(self.root)).name, s.DEFAULT_CSV_NAME)
        self.assertEqual(s.resolve_csv_path(str(self.root / "new") + "/").name,
                         s.DEFAULT_CSV_NAME)

    def test_expands_home(self):
        self.assertTrue(s.resolve_csv_path("~/x.csv").is_absolute())
        self.assertNotIn("~", str(s.resolve_csv_path("~/x.csv")))

    def test_rejects_null_character(self):
        with self.assertRaises(s.CsvPathError):
            s.resolve_csv_path("a\x00b.csv")


class ValidateTargetTests(TempDirCase):
    def test_missing_file_in_missing_folders_is_valid_and_not_created(self):
        path = self.root / "a" / "b" / "t.csv"
        s.validate_csv_target(path)
        self.assertFalse(path.parent.exists())

    def test_existing_valid_and_empty_files_are_valid(self):
        s.validate_csv_target(self.write("ok.csv", s.HEADER + "\n"))
        s.validate_csv_target(self.write("empty.csv", ""))
        s.validate_csv_target(self.write("bom.csv", "﻿" + s.HEADER + "\r\n"))

    def test_foreign_file_is_rejected(self):
        path = self.write("other.csv", "name,age\nbob,3\n")
        with self.assertRaises(s.CsvPathError) as ctx:
            s.validate_csv_target(path)
        self.assertIn("not a TalmarPiTemp CSV", str(ctx.exception))

    def test_directory_and_file_parent_are_rejected(self):
        folder = self.root / "folder.csv"
        folder.mkdir()
        with self.assertRaises(s.CsvPathError):
            s.validate_csv_target(folder)
        blocker = self.write("blocker", "x")
        with self.assertRaises(s.CsvPathError):
            s.validate_csv_target(blocker / "t.csv")

    def test_unmounted_drive_is_rejected_on_posix_paths(self):
        mount = s._mount_point(PurePosixPath("/media/pi/USB/temperature.csv"))
        self.assertEqual(mount, PurePosixPath("/media/pi/USB"))
        self.assertEqual(s._mount_point(PurePosixPath("/mnt/storage/t.csv")),
                         PurePosixPath("/mnt/storage"))
        self.assertEqual(s._mount_point(PurePosixPath("/run/media/me/D/t.csv")),
                         PurePosixPath("/run/media/me/D"))
        self.assertIsNone(s._mount_point(PurePosixPath("/mnt/t.csv")))
        self.assertIsNone(s._mount_point(PurePosixPath("/home/pi/t.csv")))

    def test_check_csv_path_combines_resolve_and_validate(self):
        path = s.check_csv_path(str(self.root / "sub" / "t.csv"))
        self.assertEqual(path.name, "t.csv")
        with self.assertRaises(s.CsvPathError):
            s.check_csv_path(str(self.root / "t.txt"))


class PrepareAndAppendTests(TempDirCase):
    def test_prepare_creates_folders_file_and_header(self):
        path = self.root / "deep" / "er" / "t.csv"
        s.prepare_csv(path)
        self.assertEqual(path.read_text(), s.HEADER + "\n")

    def test_prepare_keeps_existing_rows(self):
        path = self.write("t.csv", s.HEADER + "\n2026-09-20 12:00:00,45.20\n")
        s.prepare_csv(path)
        self.assertEqual(path.read_text(), s.HEADER + "\n2026-09-20 12:00:00,45.20\n")

    def test_append_adds_one_celsius_row(self):
        path = self.root / "t.csv"
        s.prepare_csv(path)
        s.append_reading(path, datetime(2026, 9, 20, 12, 0, 10), 46.1)
        s.append_reading(path, datetime(2026, 9, 20, 12, 0, 20), 47.346)
        self.assertEqual(path.read_text().splitlines(),
                         [s.HEADER, "2026-09-20 12:00:10,46.10", "2026-09-20 12:00:20,47.35"])

    def test_append_closes_a_torn_last_line(self):
        path = self.write("t.csv", s.HEADER + "\n2026-09-20 12:00:00,45.2")
        s.append_reading(path, datetime(2026, 9, 20, 12, 0, 10), 46.1)
        lines = path.read_text().splitlines()
        self.assertEqual(lines[-1], "2026-09-20 12:00:10,46.10")
        self.assertEqual(len(lines), 3)

    def test_failed_write_is_rolled_back(self):
        path = self.write("t.csv", s.HEADER + "\n2026-09-20 12:00:00,45.20\n")
        before = path.read_bytes()
        with mock.patch("talmarpitemp.storage.os.fsync", side_effect=OSError(errno.EIO, "boom")):
            with self.assertRaises(OSError):
                s.append_reading(path, datetime(2026, 9, 20, 12, 0, 10), 46.1)
        self.assertEqual(path.read_bytes(), before)

    def test_failed_write_after_a_repair_newline_is_rolled_back(self):
        path = self.write("t.csv", s.HEADER + "\n2026-09-20 12:00:00,45.20")
        before = path.read_bytes()
        with mock.patch("talmarpitemp.storage.os.fsync", side_effect=OSError(errno.EIO, "boom")):
            with self.assertRaises(OSError):
                s.append_reading(path, datetime(2026, 9, 20, 12, 0, 10), 46.1)
        self.assertEqual(path.read_bytes(), before)

    def test_append_works_when_the_file_has_crlf_endings(self):
        path = self.write("t.csv", s.HEADER + "\r\n")
        s.append_reading(path, datetime(2026, 9, 20, 12, 0, 10), 46.1)
        self.assertEqual(path.read_text().splitlines()[-1], "2026-09-20 12:00:10,46.10")


class CopyHistoryTests(TempDirCase):
    ROWS = s.HEADER + "\n2026-09-20 12:00:00,45.20\n2026-09-20 12:00:10,46.10\n"

    def test_copies_into_missing_target_and_keeps_source(self):
        source = self.write("old.csv", self.ROWS)
        target = self.root / "new" / "t.csv"
        self.assertTrue(s.copy_history(source, target))
        self.assertEqual(target.read_text(), self.ROWS)
        self.assertEqual(source.read_text(), self.ROWS)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["t.csv"])

    def test_copies_into_header_only_target(self):
        source = self.write("old.csv", self.ROWS)
        target = self.write("t.csv", s.HEADER + "\n")
        self.assertTrue(s.copy_history(source, target))
        self.assertEqual(target.read_text(), self.ROWS)

    def test_refuses_target_with_data(self):
        source = self.write("old.csv", self.ROWS)
        target = self.write("t.csv", self.ROWS)
        with self.assertRaises(s.CsvPathError):
            s.copy_history(source, target)

    def test_nothing_to_copy(self):
        target = self.root / "t.csv"
        self.assertFalse(s.copy_history(self.root / "missing.csv", target))
        self.assertFalse(s.copy_history(self.write("blank.csv", s.HEADER + "\n"), target))
        self.assertFalse(target.exists())

    def test_same_file_is_rejected(self):
        source = self.write("old.csv", self.ROWS)
        with self.assertRaises(s.CsvPathError):
            s.copy_history(source, source)

    def test_is_blank_csv(self):
        self.assertTrue(s.is_blank_csv(self.root / "missing.csv"))
        self.assertTrue(s.is_blank_csv(self.write("a.csv", "")))
        self.assertTrue(s.is_blank_csv(self.write("b.csv", s.HEADER + "\n")))
        self.assertFalse(s.is_blank_csv(self.write("c.csv", self.ROWS)))


if __name__ == "__main__":
    unittest.main()

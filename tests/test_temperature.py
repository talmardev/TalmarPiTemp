import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from talmarpitemp import temperature as t


class ConversionTests(unittest.TestCase):
    def test_celsius_to_fahrenheit(self):
        self.assertAlmostEqual(t.celsius_to_fahrenheit(0), 32.0)
        self.assertAlmostEqual(t.celsius_to_fahrenheit(100), 212.0)
        self.assertAlmostEqual(t.celsius_to_fahrenheit(-40), -40.0)
        self.assertAlmostEqual(t.celsius_to_fahrenheit(37), 98.6)

    def test_fahrenheit_to_celsius(self):
        self.assertAlmostEqual(t.fahrenheit_to_celsius(32), 0.0)
        self.assertAlmostEqual(t.fahrenheit_to_celsius(212), 100.0)
        self.assertAlmostEqual(t.fahrenheit_to_celsius(t.celsius_to_fahrenheit(48.7)), 48.7)

    def test_from_celsius_uses_display_unit(self):
        self.assertEqual(t.from_celsius(50.0, "C"), 50.0)
        self.assertAlmostEqual(t.from_celsius(50.0, "F"), 122.0)

    def test_normalize_unit(self):
        for text in ("c", "C", " celsius ", "CELSIUS"):
            self.assertEqual(t.normalize_unit(text), "C")
        for text in ("f", "F", "Fahrenheit"):
            self.assertEqual(t.normalize_unit(text), "F")
        for text in ("", "k", "kelvin", "cf"):
            with self.assertRaises(ValueError):
                t.normalize_unit(text)

    def test_unit_symbol(self):
        self.assertEqual(t.unit_symbol("C"), "°C")

    def test_plausibility(self):
        self.assertTrue(t.is_plausible(48.7))
        for bad in (float("nan"), float("inf"), -60.0, 151.0):
            self.assertFalse(t.is_plausible(bad))


class SysfsSensorTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_reads_millidegrees(self):
        sensor = t.SysfsSensor(self.write("temp", "48694\n"))
        self.assertAlmostEqual(sensor.read_celsius(), 48.694)

    def test_reads_plain_degrees(self):
        sensor = t.SysfsSensor(self.write("temp", "48.7"))
        self.assertAlmostEqual(sensor.read_celsius(), 48.7)

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(t.TemperatureReadError) as ctx:
            t.SysfsSensor(self.root / "missing").read_celsius()
        self.assertIn("Cannot read", str(ctx.exception))

    def test_garbage_content_is_an_error(self):
        with self.assertRaises(t.TemperatureReadError):
            t.SysfsSensor(self.write("temp", "hot")).read_celsius()


class VcgencmdSensorTests(unittest.TestCase):
    @staticmethod
    def fake_run(stdout="temp=48.7'C\n", returncode=0, error=None):
        def run(*args, **kwargs):
            if error:
                raise error
            return SimpleNamespace(stdout=stdout, returncode=returncode)
        return run

    def test_parses_output(self):
        sensor = t.VcgencmdSensor("vcgencmd", self.fake_run())
        self.assertAlmostEqual(sensor.read_celsius(), 48.7)

    def test_bad_output_and_failures(self):
        for run in (self.fake_run(stdout="error"), self.fake_run(returncode=1),
                    self.fake_run(error=OSError("gone")),
                    self.fake_run(error=subprocess.TimeoutExpired("vcgencmd", 3))):
            with self.assertRaises(t.TemperatureReadError):
                t.VcgencmdSensor("vcgencmd", run).read_celsius()


class TemperatureSourceTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def make_zone(self, name, temp, zone_type="cpu-thermal"):
        zone = self.root / name
        zone.mkdir()
        (zone / "temp").write_text(temp)
        (zone / "type").write_text(zone_type)

    def source(self, **kwargs):
        kwargs.setdefault("which", lambda name: None)
        return t.TemperatureSource(thermal_root=self.root, **kwargs)

    def test_prefers_thermal_zone0(self):
        self.make_zone("thermal_zone0", "51000")
        src = self.source()
        self.assertAlmostEqual(src.read_celsius(), 51.0)
        self.assertTrue(src.description.endswith("temp"))

    def test_falls_back_to_a_cpu_zone(self):
        self.make_zone("thermal_zone1", "40000", "gpu-thermal")
        self.make_zone("thermal_zone2", "45000", "cpu_thermal")
        self.assertAlmostEqual(self.source().read_celsius(), 45.0)

    def test_falls_back_to_vcgencmd(self):
        run = VcgencmdSensorTests.fake_run()
        src = self.source(which=lambda name: "/usr/bin/vcgencmd", run=run)
        self.assertAlmostEqual(src.read_celsius(), 48.7)
        self.assertEqual(src.description, "vcgencmd measure_temp")

    def test_no_source_reports_clearly_and_recovers(self):
        src = self.source()
        with self.assertRaises(t.TemperatureReadError) as ctx:
            src.read_celsius()
        self.assertIn("No CPU temperature source", str(ctx.exception))
        self.make_zone("thermal_zone0", "50000")
        self.assertAlmostEqual(src.read_celsius(), 50.0)

    def test_override_path(self):
        path = self.root / "custom"
        path.write_text("60000")
        src = t.TemperatureSource(override=str(path), thermal_root=self.root)
        self.assertAlmostEqual(src.read_celsius(), 60.0)
        self.assertEqual(src.description, str(path))

    def test_implausible_reading_is_rejected(self):
        self.make_zone("thermal_zone0", "999000")
        with self.assertRaises(t.TemperatureReadError):
            self.source().read_celsius()


if __name__ == "__main__":
    unittest.main()

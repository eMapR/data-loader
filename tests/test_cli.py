"""`data-loader` command line: exit codes and messages. Network-free."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from data_loader.cli import main
from tests.fakes import FakeProvider, july

CONFIG = """version: 1
provider: planetary_computer
sensors: [landsat]
aoi: {{upper_left: [-122.43, 44.29], lower_right: [-122.40, 44.27]}}
time: {{start_date: 2023-07-01, end_date: 2023-07-31}}
temporal_mode: scene
grid: {{resolution_m: 300}}
output: {{dir: "{out}", bands: [red]}}
"""


def _cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        wait = patch("data_loader.engine.RETRY_WAIT_S", 0)
        wait.start()
        self.addCleanup(wait.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = self.dir / "c.yaml"
        self.cfg.write_text(CONFIG.format(out=self.dir / "out"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_validate_ok(self):
        code, out, _ = _cli("validate", str(self.cfg))
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        self.assertIn("1 window(s) 2023-07-01..2023-07-31", out)

    def test_validate_bad_config_exit_2(self):
        self.cfg.write_text(CONFIG.format(out="x").replace("temporal_mode", "temporal_mod"))
        code, _, err = _cli("validate", str(self.cfg))
        self.assertEqual(code, 2)
        self.assertIn("did you mean 'temporal_mode'", err)

    def test_old_command_line_explains_the_change(self):
        code, _, err = _cli("--config", str(self.cfg))
        self.assertEqual(code, 2)
        self.assertIn("data-loader run CONFIG", err)

    def test_run_status_verify(self):
        p = FakeProvider(july(2))
        with patch("data_loader.engine.get_provider", return_value=p):
            code, out, _ = _cli("run", str(self.cfg), "--workers", "2")
        self.assertEqual(code, 0, out)
        code, out, _ = _cli("status", str(self.dir / "out"))
        self.assertEqual(code, 0)
        self.assertIn("acquired 2", out)
        code, out, _ = _cli("verify", str(self.dir / "out"))
        self.assertEqual(code, 0)
        self.assertIn("matches its sha256", out)

    def test_run_with_failures_exit_3(self):
        p = FakeProvider(july(2), fail={"scene-1": -1})
        with patch("data_loader.engine.get_provider", return_value=p):
            code, out, _ = _cli("run", str(self.cfg))
        self.assertEqual(code, 3)
        self.assertIn("run the same command again", out)

    def test_output_dir_override(self):
        p = FakeProvider(july(1))
        with patch("data_loader.engine.get_provider", return_value=p):
            code, _, _ = _cli("run", str(self.cfg), "--output-dir", str(self.dir / "elsewhere"))
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "elsewhere" / "manifest.json").exists())

    def test_status_of_non_dataset_exit_2(self):
        code, _, err = _cli("status", str(self.dir))
        self.assertEqual(code, 2)
        self.assertIn("no manifest.json", err)


if __name__ == "__main__":
    unittest.main()

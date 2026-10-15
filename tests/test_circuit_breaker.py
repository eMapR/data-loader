"""Circuit breaker and run-stopping failures: an outage or a full disk must
not use up every acquisition's max_attempts."""
from __future__ import annotations

import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data_loader.engine import RunStopped
from data_loader.providers.base import SystemicError
from tests.fakes import FakeProvider, july, make_config, read_items, run_with


def _rows(tmp):
    return {r["itemId"]: r for r in read_items(tmp)}


class BreakerTests(unittest.TestCase):
    def test_outage_failures_are_not_counted(self):
        # every scene fails once (an outage), limit 5: both streaks are refunded,
        # and the retry pass acquires everything at attempt 1
        p = FakeProvider(july(10), fail={f"scene-{i}": 1 for i in range(10)})
        with tempfile.TemporaryDirectory() as tmp, patch("data_loader.engine.BREAKER_PAUSES_S", (0, 0, 0)):
            s = run_with(p, make_config(tmp, max_consecutive_failures=5, max_attempts=2))
            rows = _rows(tmp)
        self.assertEqual(s.counts["acquired"], 10)
        self.assertEqual({r["attempts"] for r in rows.values()}, {1})

    def test_persistent_outage_stops_the_run_without_using_attempts(self):
        p = FakeProvider(july(30), fail={f"scene-{i}": -1 for i in range(5, 30)})
        with tempfile.TemporaryDirectory() as tmp, patch("data_loader.engine.BREAKER_PAUSES_S", (0, 0)):
            with self.assertRaisesRegex(RunStopped, "source looks unavailable"):
                run_with(p, make_config(tmp, max_consecutive_failures=5))
            rows = _rows(tmp)
            m = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertEqual(sum(r["status"] == "acquired" for r in rows.values()), 5)
        self.assertTrue(all(r["status"] == "pending" and r["attempts"] == 0
                            for k, r in rows.items() if int(k.split("-")[1]) >= 5))
        self.assertIn("stopped", m["dataset"]["runs"][-1])
        self.assertNotIn("failed", {r["status"] for r in rows.values()})

    def test_scattered_failures_still_count(self):
        fail = {f"scene-{i}": -1 for i in (1, 4, 7)}
        with tempfile.TemporaryDirectory() as tmp:
            s = run_with(FakeProvider(july(9), fail=fail), make_config(tmp, max_consecutive_failures=2))
            rows = _rows(tmp)
        self.assertEqual(s.counts["failed"], 3)
        self.assertEqual({rows[k]["attempts"] for k in fail}, {3})

    def test_disabled(self):
        p = FakeProvider(july(6), fail={f"scene-{i}": -1 for i in range(6)})
        with tempfile.TemporaryDirectory() as tmp:
            s = run_with(p, make_config(tmp, max_consecutive_failures=0))
        self.assertEqual(s.counts["failed"], 6)


class SystemicFailureTests(unittest.TestCase):
    def _stops(self, exc):
        p = FakeProvider(july(4), fail_exc={"scene-2": exc})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RunStopped) as cm:
                run_with(p, make_config(tmp))
            rows = _rows(tmp)
        self.assertEqual(rows["scene-2"]["status"], "pending")
        self.assertEqual(rows["scene-2"]["attempts"], 0)
        self.assertNotEqual(rows["scene-3"]["status"], "failed")  # may have been read before the stop
        return str(cm.exception)

    def test_disk_full(self):
        self.assertIn("No space left on device", self._stops(OSError(errno.ENOSPC, "No space left on device")))

    def test_disk_full_as_gdal_message(self):
        self.assertIn("output disk", self._stops(RuntimeError("TIFFWrite: No space left on device")))

    def test_credentials_rejected(self):
        self.assertIn("token rejected", self._stops(SystemicError("token rejected")))

    def test_cli_exit_code(self):
        from data_loader.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c.yaml"
            import yaml
            cfg.write_text(yaml.safe_dump({
                "version": 1, "provider": "fake", "sensors": ["landsat"],
                "aoi": {"upper_left": [-122.43, 44.29], "lower_right": [-122.40, 44.27]},
                "time": {"start_date": "2023-07-01", "end_date": "2023-07-31"}, "temporal_mode": "scene",
                "grid": {"resolution_m": 300}, "output": {"dir": str(Path(tmp) / "out"), "bands": ["red"]}}))
            p = FakeProvider(july(2), fail_exc={"scene-0": SystemicError("token rejected")})
            with patch("data_loader.engine.make_provider", return_value=p), \
                    patch("data_loader.engine.RETRY_WAIT_S", 0):
                self.assertEqual(main(["run", str(cfg)]), 5)


if __name__ == "__main__":
    unittest.main()

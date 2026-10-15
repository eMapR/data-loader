"""Provider switches must not silently substitute a different product, and
processing-version policies a provider can't honour fail at validate time."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_loader.config import ConfigError
from data_loader.engine import check_request
from data_loader.providers import planetary_computer
from tests.fakes import FakeProvider, july, make_config, run_with


class PinnedPolicyTests(unittest.TestCase):
    def test_pinned_refused_where_there_are_no_versions(self):
        cfg = make_config("/tmp/x", sensors=[{"name": "landsat", "processing_version_policy": "pinned:05.00"}],
                          provider="planetary_computer")
        with self.assertRaisesRegex(ConfigError, "no processing versions to choose from"):
            check_request(cfg, planetary_computer.make_provider())

    def test_pinned_accepted_for_sentinel2_on_stac(self):
        cfg = make_config("/tmp/x", sensors=[{"name": "sentinel2", "processing_version_policy": "pinned:05.00"}],
                          provider="planetary_computer", output={"bands": ["red"]})
        check_request(cfg, planetary_computer.make_provider())

    def test_provider_without_declaration_refuses_pinned(self):
        cfg = make_config("/tmp/x", sensors=[{"name": "landsat", "processing_version_policy": "pinned:1"}])
        with self.assertRaisesRegex(ConfigError, "no processing versions"):
            check_request(cfg, FakeProvider([]))


class RecordedProductTests(unittest.TestCase):
    def test_recorded_request_names_the_resolved_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1)), make_config(tmp))
            m = json.loads((Path(tmp) / "manifest.json").read_text())
            yaml_text = (Path(tmp) / "request.yaml").read_text()
        self.assertEqual(m["request"]["sensors"][0]["product_family"], "usgs_c2_l2")
        self.assertIn("product_family: usgs_c2_l2", yaml_text)

    def test_dataset_recorded_without_family_resumes_with_it_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1)), make_config(tmp))
            mp = Path(tmp) / "manifest.json"
            m = json.loads(mp.read_text())
            m["request"]["sensors"][0]["product_family"] = None  # as DataLoader 1.0 recorded it
            mp.write_text(json.dumps(m))
            pinned = make_config(tmp, sensors=[{"name": "landsat", "product_family": "usgs_c2_l2"}])
            s = run_with(FakeProvider(july(2)), pinned)
            s2 = run_with(FakeProvider(july(2)), make_config(tmp))  # and unpinned again
        self.assertEqual((s.already_done, s.acquired_now, s2.already_done), (1, 1, 2))

    def test_derived_product_needs_explicit_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = FakeProvider(july(1), product_family="glad_ard")
            with self.assertRaisesRegex(ConfigError, "product_family: glad_ard"):
                run_with(p, make_config(tmp))
            s = run_with(p, make_config(tmp, sensors=[{"name": "landsat", "product_family": "glad_ard"}]))
        self.assertEqual(s.acquired_now, 1)


if __name__ == "__main__":
    unittest.main()

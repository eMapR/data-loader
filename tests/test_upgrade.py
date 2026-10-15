"""`data-loader upgrade`: a 1.0 dataset's metadata brought to 1.1 without
touching imagery, and the result resumable by the current engine."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_loader.cli import main
from data_loader.dataset import DatasetError, DatasetWriter, open_dataset
from data_loader.upgrade import parse_run, upgrade_dataset
from tests.fakes import FakeProvider, july, make_config, read_items, run_with

ARCHIVE = {"encoding": "native", "bands": ["blue", "nir"], "qa_band": True}


def _as_1_0(tmp):
    """Rewrite a fresh dataset's metadata the way DataLoader 1.0 wrote it."""
    root = Path(tmp)
    m = json.loads((root / "manifest.json").read_text())
    m["schemaVersion"] = "1.0"
    del m["dataset"]["runs"]
    m["dataset"].pop("gitDirty", None)
    for b in m["bandSets"]["landsat"]["bands"]:
        if b["role"] == "qa":
            b["nodata"] = None
    (root / "manifest.json").write_text(json.dumps(m))
    rows = read_items(tmp)
    for i, r in enumerate(rows):
        r.pop("software", None)
        r["updated"] = f"2026-10-0{7 + i}T12:00:00+00:00"
        for f in r.get("files", []):
            f.pop("bandScaleOffset", None)  # 1.0: only written when it differed from the band set
    rows[0]["status"], rows[0]["error"] = "failed", "504 for url: https://x/y.TIF?<redacted><redacted>"
    (root / "items.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return [(root / f["path"]).stat().st_mtime_ns for r in rows for f in r.get("files", [])]


class UpgradeTests(unittest.TestCase):
    def test_1_0_to_1_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(3), native=True), make_config(tmp, output=ARCHIVE))
            mtimes = _as_1_0(tmp)
            r = upgrade_dataset(tmp, runs=[parse_run("2026-10-07T00:00:00Z=aaa"),
                                           parse_run("2026-10-08T06:00:00Z=bbb")], check_files=3)
            m = json.loads((Path(tmp) / "manifest.json").read_text())
            rows = {x["itemId"]: x for x in read_items(tmp)}
            after = [(Path(tmp) / f["path"]).stat().st_mtime_ns for x in read_items(tmp) for f in x.get("files", [])]
        self.assertTrue(r.written)
        self.assertEqual(r.check_problems, [])
        self.assertEqual(m["schemaVersion"], "1.1")
        self.assertEqual(m["bandSets"]["landsat"]["bands"][2]["nodata"], 0)
        self.assertEqual(rows["scene-1"]["files"][0]["bandScaleOffset"],
                         {"blue": [2.75e-5, -0.2], "nir": [2.75e-5, -0.2], "qa_pixel": [1.0, 0.0]})
        # updated 2026-10-07T12 -> aaa; 10-08T12 and 10-09T12 -> bbb
        self.assertEqual({k: v["software"]["gitCommit"] for k, v in rows.items()},
                         {"scene-0": "aaa", "scene-1": "bbb", "scene-2": "bbb"})
        self.assertEqual(rows["scene-0"]["error"], "504 for url: https://x/y.TIF?<redacted>")
        self.assertEqual([x["gitCommit"] for x in m["dataset"]["runs"]], ["aaa", "bbb"])
        self.assertEqual(m["dataset"]["upgrades"][0]["from"], "1.0")
        self.assertEqual(after, mtimes)  # no imagery rewritten

    def test_upgraded_dataset_resumes_with_current_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, output=ARCHIVE)
            run_with(FakeProvider(july(2), native=True), cfg)
            _as_1_0(tmp)
            upgrade_dataset(tmp)
            s = run_with(FakeProvider(july(3), native=True), cfg)
            m = open_dataset(tmp).manifest
        # scene-0 was turned into a failed row by _as_1_0 and is retried
        self.assertEqual((s.already_done, s.acquired_now), (1, 2))
        self.assertEqual(m["dataset"]["runs"][-1]["acquired"], 2)
        self.assertEqual(m["dataset"]["runs"][0]["note"], "run history before schema 1.1 not recorded")

    def test_upgrade_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1), native=True), make_config(tmp, output=ARCHIVE))
            self.assertFalse(upgrade_dataset(tmp).written)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(2), native=True), make_config(tmp, output=ARCHIVE))
            _as_1_0(tmp)
            before = (Path(tmp) / "items.jsonl").read_text(), (Path(tmp) / "manifest.json").read_text()
            r = upgrade_dataset(tmp, dry_run=True)
            after = (Path(tmp) / "items.jsonl").read_text(), (Path(tmp) / "manifest.json").read_text()
        self.assertEqual(r.files_scale_offset_added, 2)
        self.assertEqual(before, after)

    def test_refused_while_a_run_holds_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, output=ARCHIVE)
            run_with(FakeProvider(july(1), native=True), cfg)
            _as_1_0(tmp)
            w = DatasetWriter(tmp, cfg.to_dict())
            try:
                with self.assertRaisesRegex(DatasetError, "another DataLoader run"):
                    upgrade_dataset(tmp)
            finally:
                w.close()

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1), native=True), make_config(tmp, output=ARCHIVE))
            _as_1_0(tmp)
            self.assertEqual(main(["upgrade", tmp, "--dry-run", "--check-files", "1"]), 0)
            self.assertEqual(main(["upgrade", tmp, "--run", "yesterday=abc"]), 2)
            self.assertEqual(main(["upgrade", tmp]), 0)
            self.assertEqual(json.loads((Path(tmp) / "manifest.json").read_text())["schemaVersion"], "1.1")


if __name__ == "__main__":
    unittest.main()

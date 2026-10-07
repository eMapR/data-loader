"""The output contract and run lifecycle: manifest/items.jsonl content,
native encoding + QA band + optional masking, resume, retry limits,
updates (open-ended / extended time), request-mismatch protection,
locking, verify, and the open_dataset reader. Network-free."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import rasterio

from data_loader.config import ConfigError
from data_loader.dataset import DatasetError, DatasetWriter, open_dataset
from tests.fakes import QA_CLEAR, FakeProvider, FakeScene, july, make_config, read_items, run_with

ARCHIVE = {"encoding": "native", "bands": ["blue", "nir"], "qa_band": True}


def _manifest(tmp):
    return json.loads((Path(tmp) / "manifest.json").read_text())


class ManifestContentTests(unittest.TestCase):
    def test_manifest_describes_everything_downstream_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(2), native=True), make_config(tmp, output=ARCHIVE))
            m = _manifest(tmp)
            row = read_items(tmp)[0]
        self.assertEqual((m["schema"], m["schemaVersion"]), ("dataloader-manifest", "1.0"))
        self.assertEqual(m["request"]["provider"], "fake")
        self.assertTrue(m["dataset"]["complete"])
        bands = m["bandSets"]["landsat"]["bands"]
        self.assertEqual([b["name"] for b in bands], ["blue", "nir", "qa_pixel"])
        self.assertEqual(bands[0]["dataType"], "uint16")
        self.assertEqual((bands[0]["scale"], bands[0]["offset"], bands[0]["nodata"]), (2.75e-5, -0.2, 0))
        self.assertEqual(bands[2]["role"], "qa")
        self.assertEqual(bands[2]["qa"]["bits"]["3"], "cloud")
        self.assertFalse(m["processing"]["pixelCloudMask"]["applied"])
        grid = m["grids"]["landsat"]["aoi"]
        self.assertEqual(grid["resolutionM"], 300)
        self.assertEqual(m["coverage"]["counts"]["acquired"], 2)
        for key in ("key", "status", "sensor", "grid", "date", "datetime", "itemId", "platform",
                    "eo:cloud_cover", "fillPercent", "validFraction", "files", "provenance", "sourceMetadata"):
            self.assertIn(key, row)
        f = row["files"][0]
        self.assertEqual(f["bandSet"], "bands")
        self.assertTrue(f["path"].startswith("landsat/aoi/2023/2023-07-01_scene-0_bands"))
        self.assertEqual(len(f["sha256"]), 64)

    def test_mask_rules_recorded_when_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1), native=True),
                     make_config(tmp, output=ARCHIVE, filters={"pixel_cloud_mask": True}))
            mask = _manifest(tmp)["processing"]["pixelCloudMask"]
        self.assertTrue(mask["applied"])
        self.assertEqual(mask["rules"]["landsat"]["bits"],
                         {"1": "dilated_cloud", "2": "cirrus", "3": "cloud", "4": "cloud_shadow"})


class NativeEncodingTests(unittest.TestCase):
    def _first_file(self, tmp):
        row = read_items(tmp)[0]
        return rasterio.open(Path(tmp) / row["files"][0]["path"]), row

    def test_native_keeps_source_dn_and_qa_unmasked(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1, value=0.1), native=True), make_config(tmp, output=ARCHIVE))
            src, row = self._first_file(tmp)
            with src:
                data = src.read()
                self.assertEqual(src.descriptions, ("blue", "nir", "qa_pixel"))
                self.assertEqual(src.dtypes[0], "uint16")
                self.assertEqual(src.nodata, 0)
                self.assertEqual(src.scales[:2], (2.75e-5, 2.75e-5))
                self.assertEqual(src.offsets[:2], (-0.2, -0.2))
        dn = round((0.1 + 0.2) / 2.75e-5)
        self.assertEqual(int(data[0, 0, 0]), dn)  # the cloudy pixel is NOT masked
        self.assertEqual(int(data[2, 0, 0]), 0b0101011100001000)  # raw QA kept
        self.assertEqual(int(data[2, 1, 1]), QA_CLEAR)
        self.assertEqual(int(data[0, -1, -1]), 0)  # source fill stays nodata
        self.assertLess(row["validFraction"], 1.0)

    def test_native_with_mask_sets_flagged_pixels_to_nodata(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1), native=True),
                     make_config(tmp, output=ARCHIVE, filters={"pixel_cloud_mask": True}))
            src, _ = self._first_file(tmp)
            with src:
                data = src.read()
        self.assertEqual(int(data[0, 0, 0]), 0)  # cloud -> nodata
        self.assertNotEqual(int(data[0, 1, 1]), 0)
        self.assertNotEqual(int(data[2, 0, 0]), 0)  # the QA band itself is never masked

    def test_float32_through_native_reader_matches_reflectance(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1, value=0.1), native=True),
                     make_config(tmp, output={"bands": ["red"], "indices": ["ndvi"], "qa_band": True},
                                 filters={"pixel_cloud_mask": True}))
            row = read_items(tmp)[0]
            with rasterio.open(Path(tmp) / row["files"][0]["path"]) as s:
                bands = s.read()
                self.assertEqual(s.descriptions, ("red", "qa_pixel"))
        self.assertAlmostEqual(float(bands[0, 1, 1]), 0.1, places=4)
        self.assertTrue(np.isnan(bands[0, 0, 0]))  # masked
        self.assertTrue(np.isnan(bands[0, -1, -1]))  # fill
        self.assertEqual(float(bands[1, 1, 1]), QA_CLEAR)
        self.assertEqual([f["bandSet"] for f in row["files"]], ["bands", "indices"])

    def test_native_needs_provider_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "can't return source values"):
                run_with(FakeProvider(july(1)), make_config(tmp, output=ARCHIVE))


class ResumeAndUpdateTests(unittest.TestCase):
    def test_rerun_skips_acquired(self):
        p = FakeProvider(july(3), native=True)
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, output=ARCHIVE))
            n = len(p.reads)
            s = run_with(p, make_config(tmp, output=ARCHIVE))
        self.assertEqual(n, 3)
        self.assertEqual(len(p.reads), 3)
        self.assertEqual((s.acquired_now, s.already_done), (0, 3))

    def test_deleted_or_truncated_file_is_redone(self):
        p = FakeProvider(july(2), native=True)
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, output=ARCHIVE))
            rows = read_items(tmp)
            (Path(tmp) / rows[0]["files"][0]["path"]).unlink()
            with open(Path(tmp) / rows[1]["files"][0]["path"], "ab") as f:
                f.write(b"junk")
            s = run_with(p, make_config(tmp, output=ARCHIVE))
        self.assertEqual(s.acquired_now, 2)

    def test_failures_retried_until_max_attempts(self):
        p = FakeProvider(july(2), fail={"scene-1": -1})
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, max_attempts=2)
            run_with(p, cfg)  # both attempts used within this run
            s = run_with(p, cfg)
            row = {r["itemId"]: r for r in read_items(tmp)}["scene-1"]
        self.assertEqual(p.reads.count("scene-1"), 2)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 2))
        self.assertEqual(s.acquired_now + s.failed_now, 0)

    def test_transient_failure_retried_within_the_same_run(self):
        p = FakeProvider(july(2), fail={"scene-1": 1})
        with tempfile.TemporaryDirectory() as tmp:
            first = run_with(p, make_config(tmp))
            row = {r["itemId"]: r for r in read_items(tmp)}["scene-1"]
        self.assertTrue(first.complete)
        self.assertEqual((first.acquired_now, first.failed_now), (2, 0))
        self.assertEqual((row["status"], row["attempts"]), ("acquired", 2))

    def test_interrupt_records_acquisitions_that_finished(self):
        p = FakeProvider(july(4), interrupt={"scene-3"}, delays={"scene-0": 0.3})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyboardInterrupt):
                run_with(p, make_config(tmp, workers=4))
            rows = {r["itemId"]: r for r in read_items(tmp)}
            on_disk = {f.name.split("_")[1] for f in Path(tmp).rglob("*.tif")}
        acquired = {k for k, r in rows.items() if r["status"] == "acquired"}
        self.assertIn("scene-0", acquired)  # was in flight when the interrupt arrived
        self.assertEqual(on_disk, acquired)  # no file on disk without a record
        self.assertEqual(rows["scene-3"]["status"], "pending")

    def test_stray_tmp_files_from_a_killed_run_are_removed(self):
        p = FakeProvider(july(1))
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp))
            stray = Path(tmp) / "landsat" / "aoi" / "2023" / "x_bands.tif.tmp"
            stray.write_bytes(b"partial")
            run_with(p, make_config(tmp))
            self.assertFalse(stray.exists())

    def test_extending_the_end_date_adds_only_new_acquisitions(self):
        scenes = july(5)
        p = FakeProvider(scenes)
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, time={"end_date": "2023-07-03"}))
            s = run_with(p, make_config(tmp, time={"end_date": "2023-07-31"}))
            m = _manifest(tmp)
        self.assertEqual((s.already_done, s.acquired_now), (3, 2))
        self.assertEqual(m["request"]["time"]["end_date"], "2023-07-31")

    def test_open_ended_request_picks_up_new_imagery_later(self):
        scenes = [FakeScene("a", date(2026, 9, 1)), FakeScene("b", date(2026, 10, 1)), FakeScene("c", date(2026, 10, 20))]
        p = FakeProvider(scenes)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, time={"start_date": None, "end_date": None, "start_year": 2026})
            first = run_with(p, cfg, today=date(2026, 10, 7))
            later = run_with(p, cfg, today=date(2026, 11, 1))
        self.assertEqual(first.acquired_now, 2)
        self.assertEqual((later.already_done, later.acquired_now), (2, 1))

    def test_composite_rebuilt_when_new_scenes_join_its_season(self):
        p = FakeProvider([FakeScene("a", date(2026, 7, 1), value=0.1)])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, temporal_mode="annual_composite",
                              time={"start_date": None, "end_date": None, "start_year": 2026})
            run_with(p, cfg, today=date(2026, 7, 2))
            p.scenes.append(FakeScene("b", date(2026, 7, 20), value=0.3))
            s = run_with(p, cfg, today=date(2026, 8, 1))
            row = read_items(tmp)[0]
        self.assertEqual(s.acquired_now, 1)
        self.assertEqual(row["candidateItems"], ["a", "b"])
        self.assertEqual([x["itemId"] for x in row["sources"]], ["a", "b"])
        self.assertEqual(row["windowEnd"], "2026-08-01")

    def test_scene_cloud_filter_recorded_as_filtered(self):
        scenes = [FakeScene("clear", date(2023, 7, 1), cloud=5), FakeScene("cloudy", date(2023, 7, 2), cloud=90),
                  FakeScene("unknown", date(2023, 7, 3), cloud=None)]
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(scenes), make_config(tmp, filters={"max_cloud_percent": 50}))
            rows = {r["itemId"]: r for r in read_items(tmp)}
        self.assertEqual(rows["cloudy"]["status"], "filtered")
        self.assertIn("90.0% > filters.max_cloud_percent 50", rows["cloudy"]["reason"])
        self.assertEqual(rows["clear"]["status"], "acquired")
        self.assertEqual(rows["unknown"]["status"], "acquired")

    def test_season_windows_select_scenes_and_label_season_year(self):
        scenes = [FakeScene("dec", date(2020, 12, 15)), FakeScene("jan", date(2021, 1, 15)),
                  FakeScene("jul", date(2021, 7, 1))]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, time={"start_date": None, "end_date": None, "start_year": 2020,
                                         "end_year": 2020, "season": {"start": "11-01", "end": "02-28"}})
            run_with(FakeProvider(scenes), cfg)
            rows = {r["itemId"]: r for r in read_items(tmp)}
        self.assertEqual(set(rows), {"dec", "jan"})
        self.assertEqual({r["seasonYear"] for r in rows.values()}, {2020})


class DirectoryProtectionTests(unittest.TestCase):
    def test_different_request_into_same_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1)), make_config(tmp))
            with self.assertRaisesRegex(DatasetError, "different dataset.*output.bands"):
                run_with(FakeProvider(july(1)), make_config(tmp, output={"bands": ["nir"]}))

    def test_non_empty_foreign_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "something.txt").write_text("x")
            with self.assertRaisesRegex(DatasetError, "not empty"):
                run_with(FakeProvider(july(1)), make_config(tmp))

    def test_second_writer_is_locked_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = DatasetWriter(tmp, make_config(tmp).to_dict())
            try:
                with self.assertRaisesRegex(DatasetError, "another DataLoader run"):
                    DatasetWriter(tmp, make_config(tmp).to_dict())
            finally:
                w.close()

    def test_interrupted_journal_line_is_ignored(self):
        p = FakeProvider(july(2))
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp))
            with open(Path(tmp) / ".state" / "journal.jsonl", "a") as f:
                f.write('{"key": "landsat/aoi/scene-9", "sta')  # killed mid-write
            s = run_with(p, make_config(tmp))
        self.assertEqual(s.already_done, 2)


class ReaderTests(unittest.TestCase):
    def test_open_dataset_filters_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(4), native=True), make_config(tmp, output=ARCHIVE))
            ds = open_dataset(tmp)
            self.assertEqual(len(list(ds.items())), 4)
            self.assertEqual([r["itemId"] for r in ds.items(start="2023-07-02", end="2023-07-03")],
                             ["scene-1", "scene-2"])
            files = list(ds.files(grid="aoi"))
            self.assertTrue(all(p.is_absolute() and p.exists() for p, _, _ in files))
            self.assertEqual(ds.verify(), [])
            with open(files[0][0], "r+b") as f:  # same size, different bytes
                f.seek(200)
                f.write(b"\xff\xff")
            self.assertEqual(len(ds.verify()), 1)
            self.assertEqual(ds.verify(checksums=False), [])
            self.assertEqual(ds.status()["byStatus"]["acquired"], 4)

    def test_reader_sees_a_run_in_progress_via_the_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1)), make_config(tmp))
            with open(Path(tmp) / ".state" / "journal.jsonl", "a") as f:
                f.write(json.dumps({"key": "landsat/aoi/new", "status": "acquired", "date": "2023-07-30",
                                    "files": []}) + "\n")
            self.assertEqual(len(list(open_dataset(tmp).items())), 2)

    def test_not_a_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(DatasetError, "no manifest.json"):
                open_dataset(tmp)


if __name__ == "__main__":
    unittest.main()

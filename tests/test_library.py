"""The imagery library: importing a multi-tile archive as per-tile datasets
(source untouched), DataLoader resuming/updating an imported tile without
re-downloading, product-spec checks, the catalog and AOI queries.
Network-free: archives are written by the real engine with the fake provider."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from data_loader.config import config_from_dict
from data_loader.dataset import open_dataset
from tests.fakes import FakeProvider, FakeScene, july, make_config, read_items, run_with
from tests.test_tiles import _SmallGrid

try:
    import geopandas  # noqa: F401
    from imagery_library import LibraryError, open_library
    from imagery_library.acquire import tiles_needed, write_configs
    from imagery_library.catalog import build_catalog, verify_tiles
    from imagery_library.importer import import_archive, plan_import
    from imagery_library.layout import add_product, init_library
    HAVE_GEO = True
except ImportError:  # pragma: no cover - library extra not installed
    HAVE_GEO = False

TILES = ["h003v004", "h003v005"]
OUTPUT = {"encoding": "native", "bands": ["red"], "qa_band": True}
PRODUCT = {
    "product": "test_ard", "version": "c2", "title": "fake ARD for tests",
    "grid_systems": ["usgs_ard_conus", "usgs_ard_alaska"],
    "acquisition": {"version": 1, "provider": "fake",
                    "sensors": [{"name": "landsat", "product_family": "usgs_c2_l2"}],
                    "time": {"start_date": "2023-07-01", "end_date": "2023-07-31"},
                    "temporal_mode": "scene", "output": dict(OUTPUT), "workers": 1},
}


def _provider(scenes, **kw):
    p = FakeProvider(scenes, native=True, tiles=True, **kw)
    p.tile_grid = _SmallGrid()
    return p


def _archive(root, scenes=None, **kw):
    """A project-style archive: two tiles in one DataLoader dataset."""
    p = _provider(scenes or july(3), **kw)
    cfg = make_config(root, aoi={"upper_left": None, "lower_right": None, "tiles": TILES},
                      grid={"resolution_m": None}, output=OUTPUT, workers=1)
    run_with(p, cfg)
    return Path(root)


def _library(root, spec=None):
    lib = init_library(root, name="test library", description="tests", maintainers=["tester"])
    sp = Path(root) / "spec.yaml"
    sp.write_text(yaml.safe_dump(spec or PRODUCT))
    add_product(lib, sp)
    return lib


def _bytes(p: Path) -> bytes:
    return p.read_bytes()


@unittest.skipUnless(HAVE_GEO, "needs the library extra (geopandas, pyarrow)")
class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = _archive(Path(self.tmp.name) / "project_archive")
        self.lib = _library(Path(self.tmp.name) / "lib")
        self.product = self.lib.product("test_ard", "c2")
        self.before = {n: _bytes(self.src / n) for n in ("manifest.json", "items.jsonl", "request.yaml")}

    def test_hardlink_import_splits_tiles_and_leaves_source_untouched(self):
        r = import_archive(self.lib, self.product, self.src, mode="hardlink", log=lambda m: None)
        self.assertTrue(r.source_unchanged)
        self.assertEqual([tp.action for tp in r.tiles], ["imported", "imported"])
        src_rows = read_items(self.src)
        for t in TILES:
            d = self.product.tile_path("usgs_ard_conus", t)
            ds = open_dataset(d)
            self.assertEqual(ds.verify(), [])
            self.assertEqual(read_items(d), [x for x in src_rows if x["grid"] == t])  # rows verbatim
            m = ds.manifest
            self.assertEqual(list(m["grids"]["landsat"]), [t])
            self.assertEqual(m["request"]["aoi"]["tiles"], [t])
            self.assertEqual(m["coverage"]["counts"]["acquired"], 3)
            for path, _, f in ds.files(band_set=None):  # same inode: no copy
                self.assertEqual(path.stat().st_ino, (self.src / f["path"]).stat().st_ino)
            lineage = json.loads((d / "LINEAGE.json").read_text())
            self.assertEqual(lineage["source"]["path"], str(self.src.resolve()))
            self.assertTrue(lineage["imageryPresent"])
        for n, b in self.before.items():
            self.assertEqual(_bytes(self.src / n), b, n)
        self.assertEqual(len((self.lib.admin_dir / "imports.jsonl").read_text().splitlines()), 2)

    def test_engine_resumes_imported_tile_and_adds_only_new_observations(self):
        import_archive(self.lib, self.product, self.src, log=lambda m: None)
        t = "h003v004"
        cfg_d = self.product.config_for_tile(t, "usgs_ard_conus")
        cfg_d["time"]["end_date"] = "2023-08-31"  # extending the time range is how a tile is updated
        newer = july(3) + [FakeScene(id="scene-aug", date=date(2023, 8, 2))]
        p = _provider(newer)
        s = run_with(p, config_from_dict(cfg_d))
        self.assertTrue(s.complete)
        self.assertEqual(p.reads, ["scene-aug"])  # nothing already acquired is downloaded again
        d = self.product.tile_path("usgs_ard_conus", t)
        self.assertEqual(open_dataset(d).verify(), [])
        self.assertEqual(sum(1 for _ in open_dataset(d).items()), 4)
        for n, b in self.before.items():  # the project archive did not change
            self.assertEqual(_bytes(self.src / n), b, n)

    def test_product_spec_mismatch_is_refused(self):
        other = json.loads(json.dumps(PRODUCT))
        other["version"] = "c2-blue"
        other["acquisition"]["output"]["bands"] = ["blue"]
        sp = Path(self.tmp.name) / "other.yaml"
        sp.write_text(yaml.safe_dump(other))
        prod = add_product(self.lib, sp)
        r = import_archive(self.lib, prod, self.src, log=lambda m: None)
        self.assertTrue(all(tp.action == "refuse" for tp in r.tiles))
        self.assertIn("output", r.tiles[0].problems[0])
        self.assertFalse(any(prod.tiles()))

    def test_second_import_is_a_no_op(self):
        import_archive(self.lib, self.product, self.src, log=lambda m: None)
        _, plans = plan_import(self.lib, self.product, self.src)
        self.assertEqual({tp.action for tp in plans}, {"skip (already in library)"})

    def test_missing_source_file_refuses_that_tile(self):
        row = next(x for x in read_items(self.src) if x["grid"] == "h003v005")
        (self.src / row["files"][0]["path"]).unlink()
        r = import_archive(self.lib, self.product, self.src, log=lambda m: None)
        actions = {tp.tile: tp.action for tp in r.tiles}
        self.assertEqual(actions, {"h003v004": "imported", "h003v005": "refuse"})
        self.assertFalse(self.product.tile_path("usgs_ard_conus", "h003v005").exists())

    def test_copy_mode_makes_independent_files(self):
        import_archive(self.lib, self.product, self.src, mode="copy", log=lambda m: None)
        ds = open_dataset(self.product.tile_path("usgs_ard_conus", "h003v004"))
        self.assertEqual(ds.verify(), [])
        for path, _, f in ds.files(band_set=None):
            self.assertNotEqual(path.stat().st_ino, (self.src / f["path"]).stat().st_ino)

    def test_failed_rows_travel_with_their_tile(self):
        src = _archive(Path(self.tmp.name) / "with_failure", fail={"scene-1": -1})
        lib = _library(Path(self.tmp.name) / "lib2")
        prod = lib.product()
        import_archive(lib, prod, src, log=lambda m: None)
        build_catalog(lib, log=lambda m: None)
        import geopandas as gpd

        from imagery_library.catalog import current_catalog
        t = gpd.read_parquet(current_catalog(lib) / "tiles.parquet").set_index("tile")
        self.assertEqual(t.loc["h003v004", "failed"], 1)
        self.assertFalse(t.loc["h003v004", "complete"])


@unittest.skipUnless(HAVE_GEO, "needs the library extra (geopandas, pyarrow)")
class CatalogQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        src = _archive(Path(self.tmp.name) / "project_archive")
        self.lib = _library(Path(self.tmp.name) / "lib")
        self.product = self.lib.product()
        import_archive(self.lib, self.product, src, log=lambda m: None)
        verify_tiles(self.lib, log=lambda m: None)
        build_catalog(self.lib, log=lambda m: None)
        from data_loader.tiles import UsgsArdGrid
        self.grid = UsgsArdGrid()

    def _inside(self, tile, frac=0.25):
        w, s, e, n = self.grid.bbox_lonlat(tile)
        cx, cy = (w + e) / 2, (s + n) / 2
        return (cx - (e - w) * frac / 2, cy - (n - s) * frac / 2, cx + (e - w) * frac / 2, cy + (n - s) * frac / 2)

    def test_query_by_aoi_finds_tile_without_tile_ids(self):
        r = self.lib.query(self._inside("h003v004"))
        self.assertEqual(list(r.tiles["tile"]), ["h003v004"])
        self.assertEqual(len(r.observations), 3)
        self.assertAlmostEqual(r.covered_share, 1.0, places=6)
        self.assertEqual(r.missing, [])
        self.assertTrue(all(p.exists() for p in r.paths()))
        self.assertTrue(bool(r.tiles["verify_ok"].iloc[0]))

    def test_date_filter(self):
        r = self.lib.query(self._inside("h003v004"), start="2023-07-02", end="2023-07-02")
        self.assertEqual(list(r.observations["date"]), ["2023-07-02"])

    def test_aoi_beyond_the_library_reports_missing_tiles(self):
        w, s, _, _ = self.grid.bbox_lonlat("h003v005")
        _, _, e, n = self.grid.bbox_lonlat("h004v005")
        r = self.lib.query((w + 0.3, s + 0.3, e - 0.3, n - 0.3))
        self.assertIn("h003v005", set(r.tiles["tile"]))
        self.assertIn("h004v005", {m["tile"] for m in r.missing})
        self.assertLess(r.covered_share, 0.9)

    def test_acquire_config_skips_held_tiles(self):
        w, s, _, _ = self.grid.bbox_lonlat("h003v005")
        _, _, e, n = self.grid.bbox_lonlat("h004v005")
        from shapely.geometry import box
        needed = tiles_needed(self.lib, self.product, aoi=box(w + 0.3, s + 0.3, e - 0.3, n - 0.3))
        held = {r["tile"] for r in needed if r["held"]}
        self.assertIn("h003v005", held)
        out = write_configs(self.lib, self.product, needed, Path(self.tmp.name) / "cfgs")
        names = " ".join(p.name for p in out)
        self.assertNotIn("h003v005", names)
        self.assertIn("h004v005", names)
        cfg = config_from_dict(yaml.safe_load(out[0].read_text()))
        self.assertTrue(cfg.output.dir.startswith(str(self.lib.root)))

    def test_catalog_builds_swap_atomically_and_old_builds_are_pruned(self):
        for _ in range(4):
            build_catalog(self.lib, log=lambda m: None)
        cur = (self.lib.catalog_dir / "current")
        self.assertTrue(cur.is_symlink())
        builds = [p for p in self.lib.catalog_dir.iterdir() if p.is_dir() and not p.is_symlink()]
        self.assertEqual(len(builds), 3)
        self.assertIn(cur.resolve(), builds)
        summary = json.loads((cur / "catalog.json").read_text())
        self.assertEqual((summary["tiles"], summary["observations"], summary["problems"]), (2, 6, []))

    def test_metadata_only_import_is_catalogued_but_flagged(self):
        src = _archive(Path(self.tmp.name) / "meta_src")
        lib = _library(Path(self.tmp.name) / "lib_meta")
        import_archive(lib, lib.product(), src, mode="metadata", log=lambda m: None)
        self.assertEqual(verify_tiles(lib, log=lambda m: None), [])  # nothing to verify
        build_catalog(lib, log=lambda m: None)
        r = lib.query(self._inside("h003v004"))
        self.assertEqual(len(r.observations), 3)
        self.assertEqual(r.unavailable_tiles, ["usgs_ard_conus/h003v004"])

    def test_library_files_are_strict(self):
        (self.lib.root / "LIBRARY.yaml").write_text(
            (self.lib.root / "LIBRARY.yaml").read_text() + "owner_project: x\n")
        with self.assertRaisesRegex(LibraryError, "unknown key"):
            open_library(self.lib.root)


if __name__ == "__main__":
    unittest.main()

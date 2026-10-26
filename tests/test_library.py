"""The imagery library: importing a multi-tile archive as per-tile datasets
(source untouched), DataLoader resuming/updating an imported tile without
re-downloading, product-spec checks, the catalog and AOI queries.
Network-free: archives are written by the real engine with the fake provider."""
from __future__ import annotations

import json
import os
import sys
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
        self.assertEqual(t.loc["h003v004", "acquisition_state"], "incomplete")
        self.assertIn("simulated failure", t.loc["h003v004", "failed_example"])


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
        self.assertEqual(r.tiles["verification_state"].iloc[0], "verified")
        self.assertEqual(r.tiles["acquisition_state"].iloc[0], "complete")
        self.assertTrue(r.observations["verified"].all())

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
        from unittest.mock import patch
        with patch("imagery_library.catalog.BUILD_MIN_AGE_S", 0):
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


@unittest.skipUnless(HAVE_GEO, "needs the library extra (geopandas, pyarrow)")
class SafetyTests(unittest.TestCase):
    """States, concurrency and grid edge cases (the pre-migration review)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = _archive(Path(self.tmp.name) / "project_archive")
        self.lib = _library(Path(self.tmp.name) / "lib")
        self.product = self.lib.product()
        import_archive(self.lib, self.product, self.src, log=lambda m: None)
        from data_loader.tiles import UsgsArdGrid
        w, s, e, n = UsgsArdGrid().bbox_lonlat("h003v004")
        self.inside = ((w + e) / 2 - 0.2, (s + n) / 2 - 0.2, (w + e) / 2 + 0.2, (s + n) / 2 + 0.2)

    def _tiles(self):
        import geopandas as gpd
        from imagery_library.catalog import current_catalog
        return gpd.read_parquet(current_catalog(self.lib) / "tiles.parquet").set_index("tile")

    def _hold_tile_lock(self, tile):
        """What a running `data-loader run` holds on its tile directory."""
        import fcntl
        lock = self.product.tile_path("usgs_ard_conus", tile) / ".state" / "lock"
        lock.parent.mkdir(exist_ok=True)
        f = open(lock, "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        self.addCleanup(f.close)
        return f

    def test_verification_states_follow_new_observations(self):
        build_catalog(self.lib, log=lambda m: None)
        self.assertEqual(self._tiles().loc["h003v004", "verification_state"], "unverified")
        verify_tiles(self.lib, log=lambda m: None)
        build_catalog(self.lib, log=lambda m: None)
        self.assertEqual(self._tiles().loc["h003v004", "verification_state"], "verified")
        # an update adds one observation after the verify
        cfg = self.product.config_for_tile("h003v004", "usgs_ard_conus")
        cfg["time"]["end_date"] = "2023-08-31"
        run_with(_provider(july(3) + [FakeScene(id="scene-aug", date=date(2023, 8, 2))]), config_from_dict(cfg),
                 today=date(2099, 1, 1))
        build_catalog(self.lib, log=lambda m: None)
        t = self._tiles().loc["h003v004"]
        self.assertEqual((t["verification_state"], t["observations"], t["verified_observations"]),
                         ("partially_verified", 4, 3))
        r = self.lib.query(self.inside)
        self.assertEqual(int(r.observations["verified"].sum()), 3)
        self.assertIn("1 not yet verified", r.summary())
        rv = self.lib.query(self.inside, verified_only=True)
        self.assertEqual((len(rv.observations), rv.excluded_unverified), (3, 1))

    def test_sizes_only_verify_does_not_count_as_verified(self):
        verify_tiles(self.lib, checksums=False, log=lambda m: None)
        build_catalog(self.lib, log=lambda m: None)
        self.assertEqual(self._tiles().loc["h003v004", "verification_state"], "unverified")

    def test_tile_being_written_is_marked_updating_and_not_verified(self):
        self._hold_tile_lock("h003v004")
        res = verify_tiles(self.lib, log=lambda m: None)
        self.assertEqual({r["tile"]: r["ok"] for r in res}, {"h003v004": None, "h003v005": True})
        build_catalog(self.lib, log=lambda m: None)
        t = self._tiles()
        self.assertEqual(t.loc["h003v004", "acquisition_state"], "updating")
        self.assertEqual(t.loc["h003v005", "acquisition_state"], "complete")
        self.assertIn("updating", self.lib.query(self.inside).summary())

    def test_rows_read_mid_snapshot_are_not_lost(self):
        from imagery_library.catalog import tile_rows
        d = self.product.tile_path("usgs_ard_conus", "h003v004")
        m = json.loads((d / "manifest.json").read_text())
        rows = read_items(d)
        newer = dict(rows[0], status="acquired", updated="2099-01-01T00:00:00+00:00", note="newer")
        only_journal = dict(rows[0], key="landsat/h003v004/scene-new", updated="2099-01-01T00:00:00+00:00")
        (d / ".state").mkdir(exist_ok=True)
        (d / ".state" / "journal.jsonl").write_text(json.dumps(newer) + "\n" + json.dumps(only_journal) + "\n{\"cut")
        got = {r["key"]: r for r in tile_rows(d, m)}
        self.assertEqual(len(got), 4)
        self.assertEqual(got[rows[0]["key"]].get("note"), "newer")

    def test_one_catalog_build_at_a_time(self):
        import fcntl
        self.lib.catalog_dir.mkdir(exist_ok=True)
        with open(self.lib.catalog_dir / ".build.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with self.assertRaisesRegex(LibraryError, "another catalog build"):
                build_catalog(self.lib, log=lambda m: None)
        build_catalog(self.lib, log=lambda m: None)  # fine once released

    def test_current_build_is_never_pruned(self):
        from unittest.mock import patch
        from imagery_library.catalog import _prune, current_catalog
        with patch("imagery_library.catalog.BUILD_MIN_AGE_S", 0):
            for _ in range(5):
                build_catalog(self.lib, log=lambda m: None)
            first = sorted(p for p in self.lib.catalog_dir.iterdir() if p.is_dir() and not p.is_symlink())[0]
            _prune(self.lib, first.resolve())  # e.g. a slower build that finished last points `current` at it
            self.assertTrue(first.exists())
        self.assertTrue((current_catalog(self.lib) / "tiles.parquet").exists())

    def test_tile_spellings_resolve_to_one_directory(self):
        needed = tiles_needed(self.lib, self.product, tiles=["CU_h003v004", "h003v004", "AK_h020v008"])
        self.assertEqual([(r["grid_system"], r["tile"], r["held"]) for r in needed],
                         [("usgs_ard_conus", "h003v004", True), ("usgs_ard_alaska", "AK_h020v008", False)])

    def test_unpublished_grid_positions_are_not_missing(self):
        spec = json.loads(json.dumps(PRODUCT))
        spec["version"] = "c2-pub"
        spec["published_tiles"] = "published.json"
        d = Path(self.tmp.name) / "specdir"
        d.mkdir()
        (d / "published.json").write_text(json.dumps(
            {"tiles": {"usgs_ard_conus": ["h003v004", "h003v005"], "usgs_ard_alaska": []}}))
        (d / "PRODUCT.yaml").write_text(yaml.safe_dump(spec))
        prod = add_product(self.lib, d / "PRODUCT.yaml")
        import_archive(self.lib, prod, self.src, log=lambda m: None)
        build_catalog(self.lib, log=lambda m: None)
        from data_loader.tiles import UsgsArdGrid
        g = UsgsArdGrid()
        w, s, _, _ = g.bbox_lonlat("h003v005")
        _, _, e, n = g.bbox_lonlat("h004v005")  # h004v005 is "not published" in this product
        r = self.lib.query((w + 0.3, s + 0.3, e - 0.3, n - 0.3), version="c2-pub")
        self.assertEqual(r.missing, [])
        self.assertLess(r.published_share, 0.9)
        self.assertIn("outside the product", r.summary())
        with self.assertRaisesRegex(LibraryError, "publishes no data"):
            tiles_needed(self.lib, prod, tiles=["h004v005"])

    def test_antimeridian_tile_footprint_is_split(self):
        from data_loader.tiles import ARD_REGIONS, UsgsArdGrid
        from imagery_library.grids import footprint_lonlat
        g = UsgsArdGrid()
        tile = next(f"AK_h{h:03d}v{v:03d}" for h in range(3) for v in range(14)
                    if (lambda b: b[2] - b[0] >= 359)(g.bbox_lonlat(f"AK_h{h:03d}v{v:03d}")))
        xmin, _, _, ymax = g.bounds(tile)
        fp = footprint_lonlat({"crsWkt": None, "epsg": ARD_REGIONS["AK"].crs, "transform": [30, 0, xmin, 0, -30, ymax],
                               "width": 5000, "height": 5000})
        self.assertEqual(fp.geom_type, "MultiPolygon")
        self.assertEqual(len(fp.geoms), 2)
        self.assertLess(sum(p.bounds[2] - p.bounds[0] for p in fp.geoms), 20)

    def test_acquire_uses_dataloaders_account_lock_in_the_library(self):
        from data_loader.account_lock import AccountLock
        from imagery_library.acquire import acquire, lock_dir
        fake = Path(self.tmp.name) / "fake-data-loader"
        probe = Path(self.tmp.name) / "probe.txt"
        # stands in for `data-loader run`: takes DataLoader's real account lock the way run() does
        fake.write_text(f"""#!{sys.executable}
import os, sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from data_loader.account_lock import AccountBusy, AccountLock
lock = AccountLock("acct-x", sys.argv[2])
try:
    lock.acquire(wait="--wait-for-account" in sys.argv, log=lambda m: None)
except AccountBusy:
    sys.exit(6)
open({str(probe)!r}, "a").write(os.environ["DATALOADER_LOCK_DIR"] + " " + " ".join(sys.argv[3:]) + "\\n")
lock.release()
""")
        fake.chmod(0o755)
        needed = tiles_needed(self.lib, self.product, tiles=["h004v004", "h004v005"])
        rows = acquire(self.lib, self.product, needed, data_loader=str(fake), log=lambda m: None)
        self.assertEqual([r["exit"] for r in rows], [0, 0])
        lines = probe.read_text().splitlines()
        self.assertEqual(lines, [f"{lock_dir(self.lib)} --wait-for-account"] * 2)
        # a direct `data-loader run` holding the account (same DATALOADER_LOCK_DIR) makes acquire skip
        from unittest.mock import patch
        with patch.dict(os.environ, {"DATALOADER_LOCK_DIR": str(lock_dir(self.lib))}):
            holder = AccountLock("acct-x", "/direct-run")
            holder.acquire(wait=False)
        try:
            rows = acquire(self.lib, self.product, needed, wait=False, data_loader=str(fake), log=lambda m: None)
        finally:
            holder.release()
        self.assertEqual({r["outcome"] for r in rows}, {"skipped: account busy"})
        self.assertEqual(len(probe.read_text().splitlines()), 2)


@unittest.skipUnless(HAVE_GEO, "needs the library extra (geopandas, pyarrow)")
class CompletenessTests(unittest.TestCase):
    def test_tile_without_failures_is_complete_when_source_failed_elsewhere(self):
        from imagery_library.importer import _tile_manifest
        m = {"dataset": {"complete": False}, "request": {"aoi": {}, "output": {}},
             "grids": {"landsat": {"h003v004": {}, "h003v005": {}}}, "coverage": {}}
        ok_rows = [{"key": "a", "status": "acquired", "date": "2023-07-01", "files": []}]
        self.assertTrue(_tile_manifest(m, "h003v004", ok_rows, Path("/x"), source_unfinished_elsewhere=True)[0]
                        ["dataset"]["complete"])
        self.assertFalse(_tile_manifest(m, "h003v004", ok_rows, Path("/x"))[0]["dataset"]["complete"])


@unittest.skipUnless(HAVE_GEO, "needs the library extra (geopandas, pyarrow)")
class PermissionsSupersededBackupTests(unittest.TestCase):
    def setUp(self):
        import grp
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.group = grp.getgrgid(os.getgid()).gr_name
        self.lib = init_library(Path(self.tmp.name) / "lib", name="t", description="t", maintainers=["m"],
                                group=self.group)
        sp = Path(self.tmp.name) / "spec.yaml"
        sp.write_text(yaml.safe_dump(PRODUCT))
        self.product = add_product(self.lib, sp)

    def _mode(self, p):
        import stat
        return stat.S_IMODE(p.stat().st_mode)

    def test_policy_is_applied_after_import_and_after_a_dataloader_run(self):
        src = _archive(Path(self.tmp.name) / "src")
        import_archive(self.lib, self.product, src, mode="copy", log=lambda m: None)
        d = self.product.tile_path("usgs_ard_conus", "h003v004")
        tif = next(d.rglob("*.tif"))
        self.assertEqual(self._mode(tif), 0o444)
        self.assertEqual(self._mode(d / "manifest.json"), 0o644)
        self.assertEqual(self._mode(d), 0o2775)
        self.assertEqual(self._mode(self.lib.root / "LIBRARY.yaml"), 0o644)
        if os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                open(tif, "r+b")  # nobody edits imagery in place, not even its owner
        # a DataLoader update into the sealed tile still works (temp file + rename)
        cfg = self.product.config_for_tile("h003v004", "usgs_ard_conus")
        cfg["time"]["end_date"] = "2023-08-31"
        run_with(_provider(july(3) + [FakeScene(id="scene-aug", date=date(2023, 8, 2))]), config_from_dict(cfg))
        from imagery_library.permissions import apply
        before = apply(self.lib, fix=False)
        self.assertTrue([p for p in before.problems if p])  # new files arrive with the umask, .state too
        apply(self.lib)
        after = apply(self.lib, fix=False)
        self.assertEqual(after.problems, [])
        self.assertEqual(self._mode(d / ".state"), 0o2770)
        self.assertEqual(self._mode(d / ".state" / "lock"), 0o660)
        self.assertEqual(open_dataset(d).verify(), [])

    def test_bad_policies_are_refused(self):
        from imagery_library.permissions import Policy
        for bad in ({"imagery": "0666"}, {"dirs": "2777"}, {"group": "no-such-group-xyz"}, {"owner": "x"}):
            self.lib.info["permissions"] = bad
            with self.subTest(bad=bad), self.assertRaises(LibraryError):
                Policy.from_library(self.lib)

    def test_superseded_versions_in_catalog_and_backup_tiers(self):
        from tests.test_superseded import NEW, OLD, OTHER, VersionedProvider, _scenes
        src = Path(self.tmp.name) / "src"
        cfg = make_config(src, aoi={"upper_left": None, "lower_right": None, "tiles": ["h003v004"]},
                          grid={"resolution_m": None}, output=OUTPUT, workers=1)
        run_with(VersionedProvider(_scenes(OLD, OTHER)), cfg)          # old version acquired first
        run_with(VersionedProvider(_scenes(OLD, NEW, OTHER)), cfg)     # then a newer one appears
        import_archive(self.lib, self.product, src, log=lambda m: None)
        build_catalog(self.lib, log=lambda m: None)
        import geopandas as gpd
        import pandas as pd
        from imagery_library.catalog import current_catalog
        t = gpd.read_parquet(current_catalog(self.lib) / "tiles.parquet").set_index("tile")
        o = pd.read_parquet(current_catalog(self.lib) / "observations.parquet").set_index("item_id")
        self.assertEqual(t.loc["h003v004", "acquisition_state"], "complete")
        self.assertEqual(t.loc["h003v004", "retired_versions_held"], 1)
        self.assertEqual(o.loc[OLD, "superseded_by"], NEW)
        from imagery_library.backup import backup_list
        irr = list(backup_list(self.lib, "irreplaceable"))
        img = list(backup_list(self.lib, "imagery"))
        meta = list(backup_list(self.lib, "metadata"))
        self.assertTrue(any(OLD in p and p.endswith(".tif") for p in irr))
        self.assertFalse(any(OLD in p for p in img))
        self.assertEqual(sum(p.endswith(".tif") for p in img), 2)
        self.assertIn("LIBRARY.yaml", meta)
        self.assertTrue(any(p.endswith("/items.jsonl") for p in meta))
        self.assertTrue(any("/catalog/" in "/" + p and p.endswith("tiles.parquet") for p in meta))
        self.assertFalse(any(p.endswith(".tif") for p in meta))
        for p in meta + irr + img:
            self.assertTrue((self.lib.root / p).is_file(), p)

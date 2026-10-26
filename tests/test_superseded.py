"""Superseded processing versions and the per-account run lock.

USGS keeps listing an ARD item's retired processing version in STAC after
it stops distributing it (found live: LC08_CU_002006_20240608_20260113 next
to ..._20260202). The older version must not be downloaded or counted as a
failure, an older version already acquired must be kept, and completeness
is judged against the current inventory. Network-free."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from data_loader.account_lock import AccountBusy, AccountLock
from data_loader.dataset import open_dataset
from data_loader.engine import current_versions, plan_summary
from data_loader.providers.usgs_ard import UsgsArdProvider
from tests.fakes import FakeProvider, FakeScene, make_config, read_items, run_with
from tests.test_tiles import _SmallGrid

OLD = "LC08_CU_003004_20230705_20240101_02_SR"
NEW = "LC08_CU_003004_20230705_20260202_02_SR"
OTHER = "LC09_CU_003004_20230713_20240101_02_SR"
TILES_AOI = {"upper_left": None, "lower_right": None, "tiles": ["h003v004"]}
OUTPUT = {"encoding": "native", "bands": ["red"], "qa_band": True}


class VersionedProvider(FakeProvider):
    """The fake provider with ARD-style ids, version grouping and an account."""

    acquisition_key = staticmethod(UsgsArdProvider.acquisition_key)
    processing_rank = staticmethod(UsgsArdProvider.processing_rank)

    def __init__(self, scenes, account=None, **kw):
        super().__init__(scenes, native=True, tiles=True, **kw)
        self.tile_grid = _SmallGrid()
        self._account = account

    def account_lock_key(self):
        return self._account


def _scenes(*ids):
    return [FakeScene(id=i, date=date(2023, 7, int(i.split("_")[3][6:]))) for i in ids]


def _cfg(tmp, **kw):
    return make_config(tmp, aoi=TILES_AOI, grid={"resolution_m": None}, output=OUTPUT, workers=1, **kw)


def _rows(tmp):
    return {r["itemId"]: r for r in read_items(tmp)}


class SupersededTests(unittest.TestCase):
    def test_older_version_is_superseded_not_downloaded(self):
        p = VersionedProvider(_scenes(OLD, NEW, OTHER))
        with tempfile.TemporaryDirectory() as tmp:
            s = run_with(p, _cfg(tmp))
            rows = _rows(tmp)
            m = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertTrue(s.complete)
        self.assertEqual(sorted(p.reads), sorted([NEW, OTHER]))
        self.assertEqual(rows[OLD]["status"], "superseded")
        self.assertEqual(rows[OLD]["supersededBy"], NEW)
        self.assertNotIn("history", rows[OLD])
        self.assertEqual(m["coverage"]["counts"]["superseded"], 1)
        self.assertTrue(m["dataset"]["complete"])
        self.assertEqual(m["schemaVersion"], "1.2")

    def test_earlier_failure_becomes_superseded_and_keeps_its_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            # first run: only the old version is listed and USGS will not serve it
            p1 = VersionedProvider(_scenes(OLD, OTHER), fail={OLD: -1})
            s1 = run_with(p1, _cfg(tmp, max_attempts=2))
            self.assertFalse(s1.complete)
            self.assertEqual(_rows(tmp)[OLD]["status"], "failed")
            # later: the newer version appears next to it
            p2 = VersionedProvider(_scenes(OLD, NEW, OTHER), fail={OLD: -1})
            s2 = run_with(p2, _cfg(tmp, max_attempts=2))
            rows = _rows(tmp)
        self.assertTrue(s2.complete)
        self.assertEqual(p2.reads, [NEW])
        self.assertEqual(rows[OLD]["status"], "superseded")
        self.assertEqual(rows[OLD]["history"]["status"], "failed")
        self.assertEqual(rows[OLD]["history"]["attempts"], 2)
        self.assertIn("simulated failure", rows[OLD]["history"]["error"])

    def test_acquired_older_version_is_kept_as_the_historical_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_with(VersionedProvider(_scenes(OLD, OTHER)), _cfg(tmp))
            old_file = Path(tmp) / _rows(tmp)[OLD]["files"][0]["path"]
            p2 = VersionedProvider(_scenes(OLD, NEW, OTHER))
            s2 = run_with(p2, _cfg(tmp))
            rows = _rows(tmp)
            ds = open_dataset(tmp)
            self.assertEqual(ds.verify(), [])
            self.assertTrue(old_file.exists())
        self.assertTrue(s2.complete)
        self.assertEqual(p2.reads, [NEW])
        self.assertEqual(rows[OLD]["status"], "acquired")
        self.assertEqual(rows[OLD]["supersededBy"], NEW)
        self.assertEqual(rows[NEW]["status"], "acquired")

    def test_superseded_rows_are_rederived_and_stable_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                run_with(VersionedProvider(_scenes(OLD, NEW)), _cfg(tmp))
            rows = read_items(tmp)
        self.assertEqual(sorted(r["status"] for r in rows), ["acquired", "superseded"])

    def test_current_versions_grouping(self):
        p = VersionedProvider([])
        refs = [p._ref(s) for s in _scenes(OLD, NEW, OTHER)]
        cur, older = current_versions(p, refs)
        self.assertEqual(sorted(r.id for r in cur), sorted([NEW, OTHER]))
        self.assertEqual([(o.id, n.id) for o, n in older], [(OLD, NEW)])
        same = "LC08_CU_003004_20230705_20260202_02_SR"
        cur, older = current_versions(p, [p._ref(s) for s in _scenes(NEW)] + [p._ref(FakeScene(id=same, date=date(2023, 7, 5)))])
        self.assertEqual(len(older), 0)  # a tie keeps both: never pick arbitrarily
        plain = FakeProvider([])
        self.assertEqual(current_versions(plain, refs), (refs, []))

    def test_plan_counts_superseded(self):
        p = VersionedProvider(_scenes(OLD, NEW, OTHER))
        with tempfile.TemporaryDirectory() as tmp, patch("data_loader.engine.make_provider", return_value=p):
            s = plan_summary(_cfg(tmp), log=lambda m: None, today=date(2026, 10, 7))
        self.assertEqual((s["grids"][0]["units"], s["grids"][0]["superseded"]), (2, 1))

    def test_ard_acquisition_key(self):
        p = VersionedProvider([])
        r = p._ref(FakeScene(id="LC08_AK_020008_20240726_20250721_02_SR", date=date(2024, 7, 26)))
        self.assertEqual(UsgsArdProvider.acquisition_key(r), "LC08_AK_020008_20240726_02_SR")
        self.assertEqual(UsgsArdProvider.processing_rank(r), "20250721")


class AccountLockTests(unittest.TestCase):
    def setUp(self):
        self.locks = tempfile.TemporaryDirectory()
        self.addCleanup(self.locks.cleanup)
        env = patch.dict(os.environ, {"DATALOADER_LOCK_DIR": self.locks.name})
        env.start()
        self.addCleanup(env.stop)

    def test_a_second_run_on_the_same_account_is_refused_and_touches_nothing(self):
        holder = AccountLock("acct-1", "/elsewhere")
        holder.acquire(wait=False)
        self.addCleanup(holder.release)
        p = VersionedProvider(_scenes(NEW), account="acct-1")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ds"
            with self.assertRaisesRegex(AccountBusy, "another run is using this source account"):
                run_with(p, _cfg(out))
            self.assertFalse(out.exists())
        self.assertEqual(p.reads, [])

    def test_other_accounts_and_lockless_providers_are_not_blocked(self):
        holder = AccountLock("acct-1", "/elsewhere")
        holder.acquire(wait=False)
        self.addCleanup(holder.release)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(run_with(VersionedProvider(_scenes(NEW), account="acct-2"), _cfg(Path(tmp) / "a")).complete)
            self.assertTrue(run_with(VersionedProvider(_scenes(NEW), account=None), _cfg(Path(tmp) / "b")).complete)

    def test_wait_for_account_queues_behind_the_holder(self):
        holder = AccountLock("acct-1", "/elsewhere")
        holder.acquire(wait=False)
        threading.Timer(1.0, holder.release).start()
        p = VersionedProvider(_scenes(NEW), account="acct-1")
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp, patch("data_loader.account_lock.POLL_S", 0.1):
            s = run_with(p, _cfg(tmp), wait_for_account=True)
        self.assertTrue(s.complete)
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)

    def test_lock_is_released_after_a_run_and_names_no_account(self):
        p = VersionedProvider(_scenes(NEW), account="acct-1")
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, _cfg(tmp))
            again = AccountLock("acct-1", tmp)
            again.acquire(wait=False)  # free again
            again.release()
        from data_loader.providers.base import usgs_m2m_account_lock_key
        key = usgs_m2m_account_lock_key("someone@example.org")
        self.assertNotIn("someone", key)
        self.assertEqual(key, usgs_m2m_account_lock_key("someone@example.org"))


if __name__ == "__main__":
    unittest.main()

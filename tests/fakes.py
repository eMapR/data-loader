"""Network-free stand-ins for engine tests: a configurable fake Provider and
a config builder for the v1 schema."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np

from data_loader.config import config_from_dict
from data_loader.product_contract import SCENE, USGS_C2_L2, AcquisitionProvenance, ProductIdentity
from data_loader.providers.base import NativeEncoding, NativeRead, SceneRef
from data_loader.tiles import UsgsArdConusGrid

AOI = {"upper_left": [-122.43, 44.29], "lower_right": [-122.40, 44.27]}

# QA_PIXEL values: 21824 = clear land (bit 6); 22280 = cloud (bits 3, 1 ...)
QA_CLEAR = 21824
QA_CLOUD = 0b0101011100001000  # bit 3 (cloud) set


def make_config(output_dir, **over):
    d = {
        "version": 1,
        "provider": "fake",
        "sensors": [{"name": "landsat"}],
        "aoi": dict(AOI),
        "time": {"start_date": "2023-07-01", "end_date": "2023-07-31"},
        "temporal_mode": "scene",
        "grid": {"resolution_m": 300},
        "output": {"dir": str(output_dir), "bands": ["red"]},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {k2: v2 for k2, v2 in {**d[k], **v}.items() if v2 is not None}  # None = drop the key
        else:
            d[k] = v
    return config_from_dict(d)


@dataclass
class FakeScene:
    id: str
    date: date
    cloud: Optional[float] = 1.0
    value: float = 0.5  # reflectance written for every band
    qa: int = QA_CLEAR


class FakeProvider:
    """Serves `scenes` (FakeScene list). Every band of a scene reads as its
    `value` (reflectance) / the matching uint16 DN; QA is `qa` everywhere
    except pixel (0, 0), which is cloud. `native=True` adds the native-read
    capability, `tiles=True` the USGS ARD tile grid."""

    name = "fake"
    SCALE, OFFSET = 2.75e-5, -0.2

    def __init__(self, scenes, *, native=False, tiles=False, product_family=USGS_C2_L2,
                 fail=None, delays=None, shared_item_id=None):
        self.scenes = list(scenes)
        self._identity = ProductIdentity("landsat", product_family, SCENE)
        self.fail = dict(fail or {})  # scene id -> number of times to fail (or -1 = always)
        self.delays = dict(delays or {})
        self.shared_item_id = dict(shared_item_id or {})
        self.last_excluded_versions = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_concurrent = 0
        self.reads: list[str] = []
        self.source_metadata_calls: list[str] = []
        self.search_calls: list[tuple] = []
        if native:
            self.read_scene_native = self._read_scene_native
            self.native_encoding = self._native_encoding
        if tiles:
            self.tile_grid = UsgsArdConusGrid()
            self.search_tile = self._search_tile

    def capabilities(self):
        return {"landsat": self._identity}

    def processing_profile(self, sensor):
        return {"qa_policy": "landsat_qa_pixel"}

    def credential_problems(self, sensors=()):
        return []

    def _ref(self, s: FakeScene) -> SceneRef:
        item_id = self.shared_item_id.get(s.id, s.id)
        return SceneRef(id=s.id, date=s.date, cloud_percent=s.cloud, handle=s, provenance=AcquisitionProvenance(
            provider=self.name, provider_item_id=item_id, upstream_product_id=f"UPSTREAM_{s.id}",
            acquisition_datetime=f"{s.date.isoformat()}T18:00:00+00:00", platform="landsat-8",
            product_family=self._identity.product_family, collection="fake-collection",
            extra={"fill_percent": 10.0}))

    def search_scenes(self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent,
                      processing_version_policy="any"):
        self.search_calls.append((start, end, max_cloud_percent))
        return [self._ref(s) for s in self.scenes if start <= s.date <= end]

    def _search_tile(self, tile_id, sensor, start, end, max_cloud_percent=None):
        self.search_calls.append((tile_id, start, end))
        return [self._ref(s) for s in self.scenes if start <= s.date <= end]

    def _enter(self, scene):
        with self.lock:
            self.active += 1
            self.max_concurrent = max(self.max_concurrent, self.active)
        time.sleep(self.delays.get(scene.id, 0.0))
        with self.lock:
            self.reads.append(scene.id)
            n = self.fail.get(scene.id, 0)
            if n:
                self.fail[scene.id] = n - 1 if n > 0 else n
                self.active -= 1
                raise RuntimeError(f"simulated failure reading {scene.id}")

    def _exit(self):
        with self.lock:
            self.active -= 1

    def _qa(self, s: FakeScene, grid):
        qa = np.full((grid.height, grid.width), s.qa, "u2")
        qa[0, 0] = QA_CLOUD
        return qa

    def read_scene_bands(self, scene, sensor, bands, grid, pixel_cloud_mask):
        self._enter(scene)
        try:
            s = scene.handle
            out = {b: np.full((grid.height, grid.width), s.value, "f4") for b in bands}
            if pixel_cloud_mask:
                for a in out.values():
                    a[0, 0] = np.nan
            return out
        finally:
            self._exit()

    def _native_encoding(self, sensor):
        return NativeEncoding(data_type="uint16", nodata=0, scale=self.SCALE, offset=self.OFFSET,
                              qa_name="qa_pixel", qa_data_type="uint16", qa_kind="landsat_qa_pixel")

    def _read_scene_native(self, scene, sensor, bands, grid, include_qa):
        self._enter(scene)
        try:
            s = scene.handle
            dn = int(round((s.value - self.OFFSET) / self.SCALE))
            arrays = {b: np.full((grid.height, grid.width), dn, "u2") for b in bands}
            for a in arrays.values():
                a[-1, -1] = 0  # one fill pixel
            return NativeRead(bands=arrays, scale={b: self.SCALE for b in bands},
                              offset={b: self.OFFSET for b in bands}, nodata=0,
                              qa=self._qa(s, grid) if include_qa else None)
        finally:
            self._exit()

    def source_metadata(self, scene):
        with self.lock:
            self.source_metadata_calls.append(scene.provenance.provider_item_id)
        return {"raw": True, "id": scene.provenance.provider_item_id}


def july(n, start_day=1, **kw):
    return [FakeScene(id=f"scene-{i}", date=date(2023, 7, start_day + i), **kw) for i in range(n)]


def run_with(provider, config, **kw):
    from data_loader.engine import run

    return run(config, provider=provider, log=lambda m: None, today=kw.pop("today", date(2026, 10, 7)), **kw)


def read_items(path) -> list[dict]:
    import json
    from pathlib import Path

    return [json.loads(line) for line in (Path(path) / "items.jsonl").read_text().splitlines() if line.strip()]

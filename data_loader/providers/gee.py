"""Ad hoc Google Earth Engine provider — ee.Initialize + synchronous
Image.getDownloadURL, no batch Export/Drive step. Good for
exploratory/geoprocessing-sized AOIs (getDownloadURL is capped around
48MB per request). Large-corridor production exports keep using
GeoTimeSeriesApp3's existing gee_export/export_timeseries.py batch/Drive
pipeline, untouched.

Band maps and cloud-bit choices mirror export_timeseries.py so results are
consistent whether an AOI comes through this loader or through the app's
batch pipeline. Requires `earthengine-authenticate` once beforehand (or
GOOGLE_APPLICATION_CREDENTIALS set) and an EE project id passed as
`provider_options: {gee_project: "..."}` in the run config (or the
GEE_PROJECT env var) — GEE requires an explicit project, see CLAUDE.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np

from data_loader.masking import LANDSAT_QA_BAD_BITS, SENTINEL2_SCL_BAD_VALUES
from data_loader.providers.base import Grid, SceneRef

LANDSAT_TM_ETM_BANDS = {
    "blue": "SR_B1", "green": "SR_B2", "red": "SR_B3",
    "nir": "SR_B4", "swir1": "SR_B5", "swir2": "SR_B7",
}
LANDSAT_OLI_BANDS = {
    "blue": "SR_B2", "green": "SR_B3", "red": "SR_B4",
    "nir": "SR_B5", "swir1": "SR_B6", "swir2": "SR_B7",
}
SENTINEL2_BANDS = {
    "blue": "B2", "green": "B3", "red": "B4",
    "nir": "B8", "swir1": "B11", "swir2": "B12",
}


@dataclass(frozen=True)
class SensorGeeSpec:
    collections: tuple[str, ...]
    band_maps: dict[str, dict[str, str]]  # collection id -> canonical->raw
    qa_band: dict[str, str]  # collection id -> qa/scl band name
    qa_kind: str  # "landsat" | "sentinel2"
    sr_scale: float
    sr_offset: float
    cloud_property: str


_LANDSAT_COLLECTIONS = (
    "LANDSAT/LT05/C02/T1_L2",
    "LANDSAT/LE07/C02/T1_L2",
    "LANDSAT/LC08/C02/T1_L2",
    "LANDSAT/LC09/C02/T1_L2",
)

SENSORS = {
    "landsat": SensorGeeSpec(
        collections=_LANDSAT_COLLECTIONS,
        band_maps={
            "LANDSAT/LT05/C02/T1_L2": LANDSAT_TM_ETM_BANDS,
            "LANDSAT/LE07/C02/T1_L2": LANDSAT_TM_ETM_BANDS,
            "LANDSAT/LC08/C02/T1_L2": LANDSAT_OLI_BANDS,
            "LANDSAT/LC09/C02/T1_L2": LANDSAT_OLI_BANDS,
        },
        qa_band={c: "QA_PIXEL" for c in _LANDSAT_COLLECTIONS},
        qa_kind="landsat",
        sr_scale=2.75e-5, sr_offset=-0.2,
        cloud_property="CLOUD_COVER",
    ),
    "sentinel2": SensorGeeSpec(
        collections=("COPERNICUS/S2_SR_HARMONIZED",),
        band_maps={"COPERNICUS/S2_SR_HARMONIZED": SENTINEL2_BANDS},
        qa_band={"COPERNICUS/S2_SR_HARMONIZED": "SCL"},
        qa_kind="sentinel2",
        sr_scale=0.0001, sr_offset=0.0,
        cloud_property="CLOUDY_PIXEL_PERCENTAGE",
    ),
}


def _grid_bounds(grid: Grid):
    xmin = grid.transform.c
    ymax = grid.transform.f
    res = grid.transform.a
    xmax = xmin + grid.width * res
    ymin = ymax - grid.height * res
    return [xmin, ymin, xmax, ymax]


def _ee_cloud_mask(img, spec: SensorGeeSpec, cid: str):
    """EE-side equivalent of data_loader.masking's numpy masks — kept in
    sync by bit/value choice, not shared code, since one runs client-side
    on numpy arrays and the other server-side on ee.Image."""
    import ee

    qa = img.select(spec.qa_band[cid])
    if spec.qa_kind == "landsat":
        bad_bits = 0
        for b in LANDSAT_QA_BAD_BITS:
            bad_bits |= 1 << b
        return img.updateMask(qa.bitwiseAnd(bad_bits).eq(0))
    bad = ee.Image.constant(0).byte()
    for v in SENTINEL2_SCL_BAD_VALUES:
        bad = bad.Or(qa.eq(v))
    return img.updateMask(bad.Not())


def _prep(cid: str, spec: SensorGeeSpec, pixel_cloud_mask: bool):
    band_map = spec.band_maps[cid]

    def _map(img):
        out = img
        if pixel_cloud_mask:
            out = _ee_cloud_mask(out, spec, cid)
        renamed = out.select(list(band_map.values()), list(band_map.keys()))
        renamed = renamed.multiply(spec.sr_scale).add(spec.sr_offset).toFloat()
        return renamed.copyProperties(img, ["system:time_start"])

    return _map


class GeeProvider:
    name = "gee"

    def __init__(self, project: Optional[str] = None):
        import os

        self.project = project or os.environ.get("GEE_PROJECT")
        self._initialized = False

    def _ensure_init(self):
        if self._initialized:
            return
        if not self.project:
            raise ValueError(
                "GEE provider needs an Earth Engine project id — pass "
                "provider_options: {gee_project: \"your-project\"} in the "
                "config, or set the GEE_PROJECT env var."
            )
        import ee

        ee.Initialize(project=self.project)
        self._initialized = True

    def search_scenes(self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent):
        self._ensure_init()
        import ee

        spec = SENSORS[sensor]
        region = ee.Geometry.Rectangle(list(bbox))
        refs: list[SceneRef] = []
        for cid in spec.collections:
            coll = (
                ee.ImageCollection(cid)
                .filterBounds(region)
                .filterDate(start.isoformat(), (end + timedelta(days=1)).isoformat())
                .filterMetadata(spec.cloud_property, "less_than", max_cloud_percent)
            )
            ids = coll.aggregate_array("system:index").getInfo()
            times = coll.aggregate_array("system:time_start").getInfo()
            clouds = coll.aggregate_array(spec.cloud_property).getInfo()
            for idx, t_ms, cloud in zip(ids, times, clouds):
                d = date.fromtimestamp(t_ms / 1000)
                if season_start and season_end:
                    md = (d.month, d.day)
                    s = tuple(int(x) for x in season_start.split("-"))
                    e = tuple(int(x) for x in season_end.split("-"))
                    in_season = s <= md <= e if s <= e else (md >= s or md <= e)
                    if not in_season:
                        continue
                refs.append(
                    SceneRef(
                        id=f"{cid}/{idx}", date=d, cloud_percent=cloud,
                        handle={"image_id": f"{cid}/{idx}", "collection": cid},
                    )
                )
        return refs

    def _download(self, image, band_order, grid: Grid):
        import requests
        from rasterio.io import MemoryFile

        url = image.select(band_order).getDownloadURL(
            {
                "region": _grid_bounds(grid),
                "dimensions": f"{grid.width}x{grid.height}",
                "crs": grid.crs,
                "format": "GEO_TIFF",
            }
        )
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        with MemoryFile(resp.content) as mem, mem.open() as src:
            return src.read()

    def read_scene_bands(self, scene: SceneRef, sensor, bands, grid: Grid, pixel_cloud_mask: bool):
        self._ensure_init()
        import ee

        spec = SENSORS[sensor]
        cid = scene.handle["collection"]
        band_map = spec.band_maps[cid]
        img = ee.Image(scene.handle["image_id"])
        renamed = img.select(list(band_map.values()), list(band_map.keys()))
        renamed = renamed.multiply(spec.sr_scale).add(spec.sr_offset).toFloat()
        qa_name = "qa_raw"
        composite = renamed.addBands(img.select(spec.qa_band[cid]).rename(qa_name))

        arr = self._download(composite, list(bands) + [qa_name], grid)
        out = {b: arr[i] for i, b in enumerate(bands)}
        if pixel_cloud_mask:
            qa = arr[len(bands)]
            from data_loader.masking import MASKS

            mask_key = "landsat_qa_pixel" if spec.qa_kind == "landsat" else "sentinel2_scl"
            bad = MASKS[mask_key](qa)
            for a in out.values():
                a[bad] = np.nan
        return out

    def read_annual_composite(
        self, bbox, sensor, year, season_start, season_end, bands, grid: Grid,
        max_cloud_percent, pixel_cloud_mask, reduce,
    ):
        """Server-side fast path: build the cloud-masked, scaled, merged
        collection for this one year (as export_timeseries.py's
        harmonized_collection does) and reduce it in one request instead
        of downloading every scene and reducing locally."""
        self._ensure_init()
        import ee

        spec = SENSORS[sensor]
        region = ee.Geometry.Rectangle(list(bbox))

        if season_start and season_end:
            sm, sd = (int(x) for x in season_start.split("-"))
            em, ed = (int(x) for x in season_end.split("-"))
            start_d = ee.Date.fromYMD(year, sm, sd)
            end_d = ee.Date.fromYMD(year, em, ed).advance(1, "day")
            if (sm, sd) > (em, ed):
                # Season wraps the year boundary — not supported by this
                # single-year fast path; fall back to the generic
                # per-scene path, which handles wrap-around itself.
                return None
        else:
            start_d = ee.Date.fromYMD(year, 1, 1)
            end_d = ee.Date.fromYMD(year + 1, 1, 1)

        merged = None
        for cid in spec.collections:
            coll = (
                ee.ImageCollection(cid)
                .filterBounds(region)
                .filterDate(start_d, end_d)
                .filterMetadata(spec.cloud_property, "less_than", max_cloud_percent)
                .map(_prep(cid, spec, pixel_cloud_mask))
            )
            merged = coll if merged is None else merged.merge(coll)

        reduced = merged.median() if reduce == "median" else merged.mean()
        arr = self._download(reduced, list(bands), grid)
        return {b: arr[i] for i, b in enumerate(bands)}


def make_provider(gee_project: Optional[str] = None) -> GeeProvider:
    return GeeProvider(project=gee_project)

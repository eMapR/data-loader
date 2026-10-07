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

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np

from data_loader.masking import LANDSAT_QA_BAD_BITS, SENTINEL2_SCL_BAD_VALUES
from data_loader.product_contract import (
    ESA_S2_L2A_HARMONIZED,
    SCENE,
    USGS_C2_L2,
    AcquisitionProvenance,
    ProductIdentity,
    parse_version_policy,
)
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


def _cloud_filter(coll, cloud_property: str, max_cloud_percent):
    """Scene cloud filter: none at 100 (keeps 100%-cloud and unlabeled
    images), else cloud <= max."""
    if max_cloud_percent is None or max_cloud_percent >= 100:
        return coll
    return coll.filterMetadata(cloud_property, "not_greater_than", max_cloud_percent)


@dataclass(frozen=True)
class SensorGeeSpec:
    collections: tuple[str, ...]
    product_family: str  # data_loader.product_contract.{USGS_C2_L2, ESA_S2_L2A_HARMONIZED}
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
        # Same USGS Collection 2 Level-2 archive PC/Earth Search/M2M serve
        # -- confirmed live: GEE's own LANDSAT_PRODUCT_ID for a test
        # acquisition matched PC's/Earth Search's full product id exactly,
        # including the processing-date component.
        product_family=USGS_C2_L2,
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
        # Deliberately NOT esa_s2_l2a: confirmed live that this collection
        # applies Google's own baseline-offset harmonization on top of ESA's
        # L2A product (every image carries BOA_ADD_OFFSET_B*=-1000 baked
        # into how Google reports/handles it) -- it is a Google-processed
        # derivative, not a passthrough of the same bytes PC/Earth Search
        # serve, so it must not be treated as the same product_family as
        # esa_s2_l2a (see the product-contract audit).
        product_family=ESA_S2_L2A_HARMONIZED,
        band_maps={"COPERNICUS/S2_SR_HARMONIZED": SENTINEL2_BANDS},
        qa_band={"COPERNICUS/S2_SR_HARMONIZED": "SCL"},
        qa_kind="sentinel2",
        sr_scale=0.0001, sr_offset=0.0,
        cloud_property="CLOUDY_PIXEL_PERCENTAGE",
    ),
}

_QA_KIND_TO_MASK_KEY = {"landsat": "landsat_qa_pixel", "sentinel2": "sentinel2_scl"}

# Per-sensor GEE image-property names used to build AcquisitionProvenance,
# aggregated in one batched .aggregate_array(...).getInfo() call per
# property (not per-scene) -- see search_scenes.
_PROVENANCE_PROPERTIES = {
    "landsat": {"product_id": "LANDSAT_PRODUCT_ID", "platform": "SPACECRAFT_ID"},
    "sentinel2": {
        "product_id": "PRODUCT_ID", "platform": "SPACECRAFT_NAME",
        "baseline": "PROCESSING_BASELINE", "generation_time": "GENERATION_TIME",
        "datatake_id": "DATATAKE_IDENTIFIER",
    },
}


def _grid_bounds(grid: Grid):
    xmin = grid.transform.c
    ymax = grid.transform.f
    res = grid.transform.a
    xmax = xmin + grid.width * res
    ymin = ymax - grid.height * res
    return [xmin, ymin, xmax, ymax]


def _grid_region(grid: Grid):
    """The target grid's footprint as an ee.Geometry explicitly tagged with
    the grid's own CRS.

    Passing `_grid_bounds(grid)`'s bare [xmin, ymin, xmax, ymax] list
    straight as `region` to getDownloadURL is silently interpreted by Earth
    Engine as WGS84 lon/lat regardless of the separate `crs` parameter --
    for a UTM grid (values in the hundreds of thousands of meters) that is
    nonsense as a lon/lat box, and EE does not error on it: it returns a
    raster with a corrupted geotransform and all-zero pixels rather than
    failing. Tagging the geometry with `proj=grid.crs` fixes that
    interpretation. `_download` additionally requires this alongside
    `crsTransform` and `dimensions` together -- any one of the four missing
    (region/crs/crsTransform/dimensions) was observed, empirically, to make
    Earth Engine fall back to exporting the *entire* source image
    reprojected/resampled into the requested pixel count, rather than
    clipping to this AOI, which for a Landsat scene is ~40x larger in each
    dimension than intended (and can trip EE's ~48MB per-request cap)."""
    import ee

    return ee.Geometry.Rectangle(_grid_bounds(grid), proj=grid.crs, geodesic=False)


_TRANSFORM_ABS_TOL = 1e-3  # grid coordinates are in projected meters; this is far tighter than any real georeferencing error


def _validate_grid_download(src, data: "np.ndarray", grid: Grid, band_order) -> None:
    """Guards against exactly the failure this module previously had: Earth
    Engine's getDownloadURL HTTP-succeeding while silently returning a
    raster on the wrong pixel grid, or an empty one. Raises RuntimeError
    with the specific mismatch instead of letting bad imagery reach the
    caller."""
    import rasterio.crs
    import rasterio.transform

    if (src.height, src.width) != (grid.height, grid.width):
        raise RuntimeError(
            f"[gee] downloaded raster is {src.width}x{src.height}, expected "
            f"{grid.width}x{grid.height} for bands {band_order} -- Earth "
            f"Engine did not honor the requested grid size."
        )

    expected_crs = rasterio.crs.CRS.from_user_input(grid.crs)
    if src.crs != expected_crs:
        raise RuntimeError(
            f"[gee] downloaded raster CRS {src.crs} does not match requested "
            f"grid CRS {grid.crs} for bands {band_order}."
        )

    for name, actual, expected in zip("abcdef", src.transform, grid.transform):
        if abs(actual - expected) > _TRANSFORM_ABS_TOL:
            raise RuntimeError(
                f"[gee] downloaded raster transform component {name}={actual} "
                f"does not match requested grid transform component "
                f"{name}={expected} (tolerance {_TRANSFORM_ABS_TOL}) for "
                f"bands {band_order} -- geotransform mismatch, most likely "
                f"means Earth Engine did not honor region/crsTransform."
            )

    grid_bounds = rasterio.transform.array_bounds(grid.height, grid.width, grid.transform)
    src_bounds = tuple(src.bounds)
    if not (src_bounds[0] <= grid_bounds[2] and src_bounds[2] >= grid_bounds[0]
            and src_bounds[1] <= grid_bounds[3] and src_bounds[3] >= grid_bounds[1]):
        raise RuntimeError(
            f"[gee] downloaded raster bounds {src_bounds} do not overlap "
            f"the requested AOI's grid bounds {grid_bounds} for bands "
            f"{band_order}."
        )

    if data.size and not np.any(data):
        raise RuntimeError(
            f"[gee] downloaded raster for bands {band_order} is entirely "
            f"zero across all pixels and all bands -- almost certainly a "
            f"broken/empty export rather than genuine no-data (a real "
            f"Landsat/Sentinel-2 AOI essentially never has every pixel of "
            f"every band at exactly 0)."
        )


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
        # Bilinear, to match the resampling stac_common.py's WarpedVRT reads
        # use for these same reflectance bands -- EE otherwise defaults to
        # nearest neighbor, which for a target grid offset from the source
        # scene's native pixel grid (the common case) measurably disagrees
        # with the STAC providers' output even for the identical acquisition.
        renamed = renamed.resample("bilinear")
        renamed = renamed.multiply(spec.sr_scale).add(spec.sr_offset).toFloat()
        return renamed.copyProperties(img, ["system:time_start"])

    return _map


class GeeProvider:
    name = "gee"

    def __init__(self, project: Optional[str] = None):
        import os

        self.project = project or os.environ.get("GEE_PROJECT")
        self._initialized = False
        # Guards _ensure_init's check-then-init against concurrent
        # scene-mode workers (data_loader.engine's config.workers) all
        # hitting this provider's first read_scene_bands()/
        # source_metadata() call at once.
        self._init_lock = threading.Lock()
        # Populated by read_annual_composite right before it reduces --
        # the images that fed the server-side median()/mean(), recovered
        # cheaply (batched aggregate_array, no per-pixel work) so
        # engine.py can still report full composite provenance for this
        # fast path instead of leaving it null. See read_annual_composite
        # and _refs_from_collection.
        self.last_composite_scenes: list[SceneRef] = []

    def _ensure_init(self):
        if self._initialized:
            return
        with self._init_lock:
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

    def capabilities(self) -> dict[str, ProductIdentity]:
        return {
            sensor: ProductIdentity(sensor=sensor, product_family=spec.product_family, temporal_product=SCENE)
            for sensor, spec in SENSORS.items()
        }

    def processing_profile(self, sensor: str) -> dict:
        spec = SENSORS[sensor]
        return {
            "sr_scale": spec.sr_scale,
            "sr_offset": spec.sr_offset,
            "qa_policy": _QA_KIND_TO_MASK_KEY[spec.qa_kind],
            "reflectance_resampling": "bilinear",
            "categorical_resampling": "nearest",
        }

    def source_metadata(self, scene: SceneRef) -> Optional[dict]:
        """One getInfo() call for this specific already-selected scene --
        never called during search_scenes (which only does batched
        aggregate_array calls across all candidates), so discovering N
        candidates never costs more than it already did; this is invoked
        by engine.py exactly once per scene actually selected for output.

        This is Google's own representation of the upstream USGS/ESA
        product (ingested, and for Sentinel-2 harmonized, by Google) --
        one hop further from the authoritative producer than a STAC item
        naming USGS/ESA directly, not the authoritative record itself."""
        self._ensure_init()
        import ee

        image_id = scene.handle["image_id"]
        return ee.Image(image_id).getInfo()

    def search_scenes(
        self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent,
        processing_version_policy="any",
    ):
        self._ensure_init()
        import ee

        # GEE's own ingestion has not been observed to expose more than one
        # processing version per acquisition for either collection used
        # here (confirmed live for both Landsat and the Sentinel-2
        # harmonized collection), so "any"/"latest"/"allow_mixed" are all
        # pass-through in this v1. "pinned:<baseline>" is still honored
        # where baseline metadata is available (Sentinel-2) by filtering,
        # and rejected where it isn't (Landsat -- USGS Collection 2 has no
        # such concept here, see stac_common.py's identical caveat).
        version_kind, pinned_baseline = parse_version_policy(processing_version_policy)
        if version_kind == "pinned" and sensor == "landsat":
            raise ValueError(
                "[gee] processing_version_policy='pinned:...' is not "
                "supported for landsat -- USGS Collection 2 doesn't expose "
                "a reprocessing-baseline concept the way Sentinel-2 does."
            )

        spec = SENSORS[sensor]
        region = ee.Geometry.Rectangle(list(bbox))
        refs: list[SceneRef] = []
        for cid in spec.collections:
            coll = _cloud_filter(
                ee.ImageCollection(cid)
                .filterBounds(region)
                .filterDate(start.isoformat(), (end + timedelta(days=1)).isoformat()),
                spec.cloud_property, max_cloud_percent,
            )
            refs.extend(
                self._refs_from_collection(
                    coll, cid, sensor, spec, season_start, season_end, version_kind, pinned_baseline
                )
            )
        return refs

    def _refs_from_collection(
        self, coll, cid: str, sensor: str, spec: "SensorGeeSpec", season_start, season_end,
        version_kind: str, pinned_baseline: Optional[str],
    ) -> list[SceneRef]:
        """Batched (one aggregate_array().getInfo() round trip per
        property, not per image) extraction of SceneRefs -- with full
        normalized provenance -- from an already bounds/date/cloud-filtered
        ImageCollection. Shared by search_scenes and, for the fast
        annual-composite path, read_annual_composite (called there BEFORE
        the median()/mean() reduce, purely to recover which images fed it
        -- no per-pixel work, no extra download)."""
        props = _PROVENANCE_PROPERTIES[sensor]
        ids = coll.aggregate_array("system:index").getInfo()
        times = coll.aggregate_array("system:time_start").getInfo()
        clouds = coll.aggregate_array(spec.cloud_property).getInfo()
        product_ids = coll.aggregate_array(props["product_id"]).getInfo()
        platforms = coll.aggregate_array(props["platform"]).getInfo()
        baselines = (
            coll.aggregate_array(props["baseline"]).getInfo() if "baseline" in props else [None] * len(ids)
        )
        gen_times_ms = (
            coll.aggregate_array(props["generation_time"]).getInfo() if "generation_time" in props else [None] * len(ids)
        )
        datatakes = (
            coll.aggregate_array(props["datatake_id"]).getInfo() if "datatake_id" in props else [None] * len(ids)
        )

        refs: list[SceneRef] = []
        for idx, t_ms, cloud, product_id, platform, baseline, gen_ms, datatake in zip(
            ids, times, clouds, product_ids, platforms, baselines, gen_times_ms, datatakes
        ):
            if version_kind == "pinned" and baseline != pinned_baseline:
                continue
            d = date.fromtimestamp(t_ms / 1000)
            if season_start and season_end:
                md = (d.month, d.day)
                s = tuple(int(x) for x in season_start.split("-"))
                e = tuple(int(x) for x in season_end.split("-"))
                in_season = s <= md <= e if s <= e else (md >= s or md <= e)
                if not in_season:
                    continue
            generation_time = (
                datetime.fromtimestamp(gen_ms / 1000, tz=timezone.utc).isoformat() if gen_ms else None
            )
            provenance = AcquisitionProvenance(
                provider=self.name,
                provider_item_id=f"{cid}/{idx}",
                upstream_product_id=product_id,
                acquisition_datetime=datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).isoformat(),
                platform=platform,
                product_family=spec.product_family,
                collection=cid,
                processing_baseline=baseline,
                generation_time=generation_time,
                extra={"datatake_id": datatake} if datatake else {},
            )
            refs.append(
                SceneRef(
                    id=f"{cid}/{idx}", date=d, cloud_percent=cloud,
                    handle={"image_id": f"{cid}/{idx}", "collection": cid},
                    provenance=provenance,
                )
            )
        return refs

    def _download(self, image, band_order, grid: Grid):
        import requests
        from rasterio.io import MemoryFile

        crs_transform = [
            grid.transform.a, grid.transform.b, grid.transform.c,
            grid.transform.d, grid.transform.e, grid.transform.f,
        ]
        url = image.select(band_order).getDownloadURL(
            {
                # All four of region/crs/crsTransform/dimensions are required
                # together -- see _grid_region's docstring for what happens
                # if any one is missing.
                "region": _grid_region(grid),
                "crs": grid.crs,
                "crsTransform": crs_transform,
                "dimensions": f"{grid.width}x{grid.height}",
                "format": "GEO_TIFF",
            }
        )
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        with MemoryFile(resp.content) as mem, mem.open() as src:
            data = src.read()
            _validate_grid_download(src, data, grid, band_order)
            return data

    def read_scene_bands(self, scene: SceneRef, sensor, bands, grid: Grid, pixel_cloud_mask: bool):
        self._ensure_init()
        import ee

        spec = SENSORS[sensor]
        cid = scene.handle["collection"]
        band_map = spec.band_maps[cid]
        img = ee.Image(scene.handle["image_id"])
        renamed = img.select(list(band_map.values()), list(band_map.keys()))
        # Bilinear for the reflectance bands only, matching stac_common.py's
        # WarpedVRT convention (see _prep's identical comment). The QA band
        # below is deliberately left unresampled (nearest neighbor, EE's
        # default) since it's categorical -- interpolating QA codes would
        # produce meaningless intermediate values.
        renamed = renamed.resample("bilinear")
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
        contributing_refs: list[SceneRef] = []
        for cid in spec.collections:
            coll = _cloud_filter(
                ee.ImageCollection(cid).filterBounds(region).filterDate(start_d, end_d),
                spec.cloud_property, max_cloud_percent,
            )
            # Recover which images are actually eligible for this composite
            # BEFORE reducing -- one batched round of aggregate_array calls
            # (see _refs_from_collection), not a per-pixel or per-image
            # download, so this doesn't meaningfully slow the fast path
            # down. `season_start`/`season_end` are already baked into
            # start_d/end_d above, so no further in-season filtering is
            # needed here. engine.py reads self.last_composite_scenes right
            # after calling this method to populate the composite's
            # manifest "scenes" list -- the same as the non-fast-path
            # composite does.
            contributing_refs.extend(
                self._refs_from_collection(coll, cid, sensor, spec, None, None, "any", None)
            )
            mapped = coll.map(_prep(cid, spec, pixel_cloud_mask))
            merged = mapped if merged is None else merged.merge(mapped)

        self.last_composite_scenes = contributing_refs

        reduced = merged.median() if reduce == "median" else merged.mean()
        arr = self._download(reduced, list(bands), grid)
        return {b: arr[i] for i, b in enumerate(bands)}


def make_provider(gee_project: Optional[str] = None) -> GeeProvider:
    return GeeProvider(project=gee_project)

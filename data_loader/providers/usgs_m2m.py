"""USGS Machine-to-Machine (M2M) API provider — Landsat Collection 2
Level-2 only (Sentinel-2 isn't USGS-distributed, so `sensor="sentinel2"`
raises).

Needs a free EROS account (https://ers.cr.usgs.gov/register) and an M2M
application token generated from your EROS profile page. Pass credentials
as `provider_options: {usgs_username: "...", usgs_token: "..."}` in the
run config, or set USGS_M2M_USERNAME / USGS_M2M_TOKEN env vars (preferred
— keeps credentials out of the config file).

Structurally different from the STAC providers: M2M doesn't serve
individual band COGs over range requests. Reading a scene means
`download-options` (list available bundle products) -> `download-request`
(kick off staging) -> `download-retrieve`-poll until a URL is ready ->
download the whole scene bundle (a .tar of the full Level-2 product) ->
extract locally -> read the needed *_SR_B*.TIF / *_QA_PIXEL.TIF files.
Much heavier per scene than the STAC providers' windowed reads.

NOTE: implemented against the documented M2M API v1.5 JSON schema
(https://m2m.cr.usgs.gov/api/docs/json/) without being run against a live
account — unlike the STAC providers (verified live against Planetary
Computer/Earth Search), the exact response field names here (especially
in `download-request`/`download-retrieve`) may need adjusting once
exercised for real. If a call fails, the error includes the endpoint and
raw error body from M2M to make that easy to diagnose.
"""
from __future__ import annotations

import tarfile
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np

from data_loader.masking import landsat_qa_mask
from data_loader.product_contract import SCENE, USGS_C2_L2, AcquisitionProvenance, ProductIdentity, parse_version_policy
from data_loader.providers.base import Grid, SceneRef
from data_loader.providers.gee import LANDSAT_OLI_BANDS, LANDSAT_TM_ETM_BANDS

BASE_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"

# M2M keys expire ~2h after last use; re-login proactively before that so a
# long batch run (multi-scene download-request/download-retrieve polling)
# doesn't die mid-run on an expired token.
RELOGIN_AFTER = 90 * 60

# Collection 2 Level-2 science product datasets, one per sensor
# generation — mirrors the OLI vs TM/ETM split used elsewhere (gee.py,
# export_timeseries.py) since band names/counts differ between them.
DATASETS = ("landsat_ot_c2_l2", "landsat_etm_c2_l2", "landsat_tm_c2_l2")

SR_SCALE, SR_OFFSET = 2.75e-5, -0.2


def _band_map_for(display_id: str) -> dict[str, str]:
    prefix = display_id[:4]
    if prefix in ("LC08", "LC09"):
        return LANDSAT_OLI_BANDS
    return LANDSAT_TM_ETM_BANDS


_PLATFORM_PREFIXES = {
    "LC08": "landsat-8", "LC09": "landsat-9",
    "LE07": "landsat-7", "LT05": "landsat-5", "LT04": "landsat-4",
}


def _platform_for(display_id: str) -> Optional[str]:
    return _PLATFORM_PREFIXES.get(display_id[:4])


def _parse_m2m_response(resp, endpoint: str, payload: dict) -> dict:
    """Parses an M2M response body before deciding whether to raise —
    `resp.raise_for_status()` alone discards the JSON body on non-2xx
    responses, which is exactly where M2M puts the useful errorCode/
    errorMessage explaining *why* (e.g. an unapproved/unpermissioned
    account), so check the body first and only fall back to the bare
    HTTP status if M2M didn't return JSON at all."""
    try:
        body = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"M2M {endpoint}: non-JSON response (HTTP {resp.status_code}): {resp.text[:500]}")

    if body.get("errorCode") or not resp.ok:
        raise RuntimeError(
            f"M2M {endpoint} failed (HTTP {resp.status_code}): "
            f"{body.get('errorCode')}: {body.get('errorMessage')} "
            f"(request: {payload})"
        )
    return body


def _in_season(d: date, season_start: Optional[str], season_end: Optional[str]) -> bool:
    if not season_start or not season_end:
        return True
    md = (d.month, d.day)
    s = tuple(int(x) for x in season_start.split("-"))
    e = tuple(int(x) for x in season_end.split("-"))
    return s <= md <= e if s <= e else (md >= s or md <= e)


class UsgsM2mProvider:
    name = "usgs_m2m"

    def __init__(self, username: Optional[str] = None, token: Optional[str] = None):
        import os

        self.username = username or os.environ.get("USGS_M2M_USERNAME")
        self.token = token or os.environ.get("USGS_M2M_TOKEN")
        self._session = None
        self._session_born = None

    def _ensure_login(self):
        if self._session is not None and time.time() - self._session_born < RELOGIN_AFTER:
            return
        if not self.username or not self.token:
            raise ValueError(
                "usgs_m2m needs credentials — pass provider_options: "
                "{usgs_username: \"...\", usgs_token: \"...\"} in the config, "
                "or set USGS_M2M_USERNAME / USGS_M2M_TOKEN env vars. "
                "Generate a token from your EROS profile page "
                "(https://ers.cr.usgs.gov)."
            )
        import requests

        session = self._session or requests.Session()
        resp = session.post(
            BASE_URL + "login-token",
            json={"username": self.username, "token": self.token},
        )
        body = _parse_m2m_response(resp, "login-token", {"username": self.username})
        session.headers.update({"X-Auth-Token": body["data"]})
        self._session = session
        self._session_born = time.time()

    def _post(self, endpoint: str, payload: dict) -> dict:
        self._ensure_login()
        resp = self._session.post(BASE_URL + endpoint, json=payload)
        body = _parse_m2m_response(resp, endpoint, payload)
        return body["data"]

    def logout(self):
        if self._session is not None:
            try:
                self._post("logout", {})
            except Exception:
                pass
            self._session = None
            self._session_born = None

    def capabilities(self) -> dict[str, ProductIdentity]:
        return {"landsat": ProductIdentity(sensor="landsat", product_family=USGS_C2_L2, temporal_product=SCENE)}

    def processing_profile(self, sensor: str) -> dict:
        return {
            "sr_scale": SR_SCALE,
            "sr_offset": SR_OFFSET,
            "qa_policy": "landsat_qa_pixel",
            "reflectance_resampling": "bilinear",
            "categorical_resampling": "nearest",
        }

    def source_metadata(self, scene: SceneRef) -> Optional[dict]:
        """The full scene-search result USGS returned for this scene,
        already fetched at search time (see search_scenes) -- no extra
        API call needed. NOTE: like the rest of this provider, unverified
        against a live account (see module docstring); this returns
        whatever shape the documented M2M scene-search API produces."""
        return scene.handle.get("raw_metadata")

    def search_scenes(
        self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent,
        processing_version_policy="any",
    ):
        if sensor != "landsat":
            raise ValueError(
                f"usgs_m2m only supports sensor='landsat' (got {sensor!r}) — "
                "Sentinel-2 isn't USGS-distributed."
            )
        # USGS Collection 2 doesn't expose a reprocessing-baseline concept
        # the way Sentinel-2 does, and this provider has not observed (or
        # been able to test live -- see the module docstring) more than one
        # catalog entry per acquisition, so only "any"/"latest"/
        # "allow_mixed" make sense here; a pin can't be honored.
        version_kind, _ = parse_version_policy(processing_version_policy)
        if version_kind == "pinned":
            raise ValueError(
                "[usgs_m2m] processing_version_policy='pinned:...' is not "
                "supported -- USGS Collection 2 doesn't expose a "
                "reprocessing-baseline concept here."
            )

        west, south, east, north = bbox
        refs: list[SceneRef] = []
        for dataset in DATASETS:
            data = self._post("scene-search", {
                "datasetName": dataset,
                "sceneFilter": {
                    "spatialFilter": {
                        "filterType": "mbr",
                        "lowerLeft": {"latitude": south, "longitude": west},
                        "upperRight": {"latitude": north, "longitude": east},
                    },
                    "acquisitionFilter": {
                        "start": start.isoformat(), "end": end.isoformat(),
                    },
                    "cloudCoverFilter": {
                        "min": 0, "max": int(max_cloud_percent), "includeUnknown": False,
                    },
                },
                "maxResults": 2000,
            })
            for r in data.get("results", []):
                acq = r.get("temporalCoverage", {}).get("startDate") or r.get("publishDate")
                if not acq:
                    continue
                d = date.fromisoformat(str(acq)[:10])
                if not _in_season(d, season_start, season_end):
                    continue
                display_id = r["displayId"]
                provenance = AcquisitionProvenance(
                    provider=self.name,
                    provider_item_id=display_id,
                    # M2M's displayId for Collection 2 L2 is already the
                    # full USGS product id (unlike the STAC providers'
                    # shortened item id) -- not verified live (see module
                    # docstring), based on the documented M2M schema.
                    upstream_product_id=display_id,
                    acquisition_datetime=str(acq),
                    platform=_platform_for(display_id),
                    product_family=USGS_C2_L2,
                    collection=dataset,
                    processing_baseline=None,
                    generation_time=None,
                    extra={"entity_id": r["entityId"]},
                )
                refs.append(SceneRef(
                    id=display_id, date=d, cloud_percent=r.get("cloudCover"),
                    # `raw_metadata: r` costs nothing extra -- it's the
                    # exact scene-search result already fetched -- and is
                    # what source_metadata() below returns unchanged as
                    # the source snapshot, rather than re-querying M2M.
                    handle={"entityId": r["entityId"], "dataset": dataset, "displayId": display_id, "raw_metadata": r},
                    provenance=provenance,
                ))
        return refs

    def _download_bundle(self, entity_id: str, dataset: str, display_id: str, tmp_dir: Path) -> Path:
        options = self._post("download-options", {"datasetName": dataset, "entityIds": [entity_id]})
        product = next(
            (o for o in options if "bundle" in o.get("productName", "").lower()
             and "level-2" in o.get("productName", "").lower()),
            None,
        )
        if product is None:
            raise RuntimeError(
                f"No Level-2 bundle download option found for {display_id} in "
                f"download-options response: {[o.get('productName') for o in options]}"
            )

        label = f"data_loader_{entity_id}"
        req = self._post("download-request", {
            "downloads": [{"entityId": entity_id, "productId": product["id"]}],
            "label": label,
        })

        url = None
        for d in req.get("availableDownloads", []):
            url = d.get("url")
            break

        if url is None:
            deadline = time.time() + 600
            while time.time() < deadline and url is None:
                time.sleep(10)
                retrieved = self._post("download-retrieve", {"label": label})
                for d in retrieved.get("available", []):
                    url = d.get("url")
                    break
                if not retrieved.get("requested") and url is None:
                    break
            if url is None:
                raise TimeoutError(
                    f"M2M download for {display_id} didn't become available within 600s "
                    f"(label={label!r}) — check https://m2m.cr.usgs.gov downloads for status."
                )

        resp = self._session.get(url, timeout=600)
        resp.raise_for_status()
        archive_path = tmp_dir / f"{display_id}.tar"
        archive_path.write_bytes(resp.content)
        with tarfile.open(archive_path) as tf:
            tf.extractall(tmp_dir)
        return tmp_dir

    def read_scene_bands(self, scene: SceneRef, sensor, bands, grid: Grid, pixel_cloud_mask: bool):
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.vrt import WarpedVRT

        entity_id = scene.handle["entityId"]
        dataset = scene.handle["dataset"]
        display_id = scene.handle["displayId"]
        band_map = _band_map_for(display_id)

        def read_local(path: Path, resampling):
            with rasterio.open(path) as src, WarpedVRT(
                src, crs=grid.crs, transform=grid.transform,
                width=grid.width, height=grid.height, resampling=resampling,
            ) as vrt:
                return vrt.read(1)

        def find_band(extract_dir: Path, band_code: str) -> Path:
            matches = list(extract_dir.rglob(f"*_{band_code}.TIF")) or list(extract_dir.rglob(f"*_{band_code}.tif"))
            if not matches:
                raise FileNotFoundError(f"Extracted bundle for {display_id} has no *_{band_code}.TIF file")
            return matches[0]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._download_bundle(entity_id, dataset, display_id, tmp_dir)

            out: dict[str, np.ndarray] = {}
            for b in bands:
                dn = read_local(find_band(tmp_dir, band_map[b]), Resampling.bilinear)
                out[b] = dn.astype("f4") * SR_SCALE + SR_OFFSET

            if pixel_cloud_mask:
                qa = read_local(find_band(tmp_dir, "QA_PIXEL"), Resampling.nearest)
                bad = landsat_qa_mask(qa)
                for arr in out.values():
                    arr[bad] = np.nan

        return out


def make_provider(usgs_username: Optional[str] = None, usgs_token: Optional[str] = None, **kwargs) -> UsgsM2mProvider:
    return UsgsM2mProvider(username=usgs_username, token=usgs_token)

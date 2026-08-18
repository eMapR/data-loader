"""Shared machinery for plain-STAC providers (Microsoft Planetary Computer,
AWS/Element84 Earth Search): search -> per-scene windowed COG read via a
WarpedVRT (reproject/resample on the fly, no local download of full
scenes) -> optional QA/SCL cloud mask -> physical-unit reflectance.

Planetary Computer and Earth Search differ only in endpoint URL,
collection/band-asset names, and whether asset hrefs need signing — one
implementation serves both via a `StacConfig`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from data_loader.masking import MASKS
from data_loader.providers.base import Grid, SceneRef

CANONICAL_BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")


@dataclass(frozen=True)
class SensorStacSpec:
    collection: str
    band_map: dict[str, str]  # canonical name (+ "qa") -> asset key
    qa_kind: str  # key into data_loader.masking.MASKS
    sr_scale: float
    sr_offset: float
    platform_filter: Optional[tuple[str, ...]] = None
    # True if this sensor's assets live in a Requester Pays S3 bucket
    # (e.g. Earth Search's Landsat mirror, s3://usgs-landsat) — genuinely
    # needs an AWS account + real credentials, not just unsigned public
    # access, unlike every other sensor/provider combination here.
    requires_aws_credentials: bool = False


@dataclass(frozen=True)
class StacConfig:
    name: str
    stac_url: str
    needs_signing: bool
    sensors: dict[str, SensorStacSpec]


def _has_aws_credentials() -> bool:
    import os

    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return True
    return (Path.home() / ".aws" / "credentials").exists()


def _in_season(d: date, season_start: Optional[str], season_end: Optional[str]) -> bool:
    """MM-DD window applied within every year; handles a window that wraps
    the year boundary (e.g. "11-01" to "02-28")."""
    if not season_start or not season_end:
        return True
    md = (d.month, d.day)
    s = tuple(int(x) for x in season_start.split("-"))
    e = tuple(int(x) for x in season_end.split("-"))
    if s <= e:
        return s <= md <= e
    return md >= s or md <= e


class StacProvider:
    def __init__(self, config: StacConfig, *, workers: int = 8, retries: int = 4):
        self.name = config.name
        self.config = config
        self.workers = workers
        self.retries = retries
        self._client = None

    def _open_client(self):
        if self._client is not None:
            return self._client
        import os

        os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
        os.environ.setdefault("VSI_CACHE", "TRUE")
        # Safe default for genuinely public s3:// buckets (e.g. Earth
        # Search's Sentinel-2 mirror is https, not s3, so this is a no-op
        # for it; it only matters for a future s3:// sensor that isn't
        # Requester Pays). Has no effect on plain https hrefs.
        os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")

        from pystac_client import Client

        modifier = None
        if self.config.needs_signing:
            import planetary_computer as pc

            modifier = pc.sign_inplace
        self._client = Client.open(self.config.stac_url, modifier=modifier)
        return self._client

    def search_scenes(
        self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent
    ) -> list[SceneRef]:
        spec = self.config.sensors[sensor]
        if spec.requires_aws_credentials and not _has_aws_credentials():
            raise RuntimeError(
                f"[{self.name}] sensor {sensor!r} reads from a Requester "
                "Pays S3 bucket (s3://usgs-landsat) — it needs a real AWS "
                "account with credentials configured (AWS_ACCESS_KEY_ID/"
                "AWS_SECRET_ACCESS_KEY env vars or ~/.aws/credentials), "
                "and incurs small per-request charges; it is not free/"
                "anonymous like the other provider/sensor combinations "
                "here. Use provider: planetary_computer for free Landsat "
                "instead."
            )
        cat = self._open_client()

        query: dict = {"eo:cloud_cover": {"lt": max_cloud_percent}}
        if spec.platform_filter:
            query["platform"] = {"in": list(spec.platform_filter)}

        items = None
        for attempt in range(self.retries):
            try:
                items = list(
                    cat.search(
                        collections=[spec.collection],
                        bbox=bbox,
                        datetime=f"{start.isoformat()}/{end.isoformat()}",
                        query=query,
                    ).items()
                )
                break
            except Exception as e:  # STAC endpoints can be flaky; retry with backoff
                print(f"[{self.name}] STAC search retry {attempt + 1}/{self.retries}: {e}")
                time.sleep(2 * (attempt + 1))
        if items is None:
            raise RuntimeError(f"[{self.name}] STAC search failed after {self.retries} attempts")

        refs = []
        for it in items:
            d = it.datetime.astimezone(timezone.utc).date() if it.datetime else None
            if d is None or not _in_season(d, season_start, season_end):
                continue
            refs.append(
                SceneRef(
                    id=it.id,
                    date=d,
                    cloud_percent=it.properties.get("eo:cloud_cover"),
                    handle=it,
                )
            )
        return refs

    def read_scene_bands(self, scene: SceneRef, sensor: str, bands, grid: Grid, pixel_cloud_mask: bool):
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.vrt import WarpedVRT

        spec = self.config.sensors[sensor]
        item = scene.handle

        def read_asset(asset_key, resampling):
            href = item.assets[asset_key].href
            with rasterio.open(href) as src, WarpedVRT(
                src,
                crs=grid.crs,
                transform=grid.transform,
                width=grid.width,
                height=grid.height,
                resampling=resampling,
            ) as vrt:
                return vrt.read(1)

        out: dict[str, np.ndarray] = {}
        for b in bands:
            dn = read_asset(spec.band_map[b], Resampling.bilinear)
            out[b] = dn.astype("f4") * spec.sr_scale + spec.sr_offset

        if pixel_cloud_mask:
            qa = read_asset(spec.band_map["qa"], Resampling.nearest)
            bad = MASKS[spec.qa_kind](qa)
            for arr in out.values():
                arr[bad] = np.nan
        return out

"""Shared machinery for plain-STAC providers (Microsoft Planetary Computer,
AWS/Element84 Earth Search): search -> per-scene windowed COG read via a
WarpedVRT (reproject/resample on the fly, no local download of full
scenes) -> optional QA/SCL cloud mask -> physical-unit reflectance.

Planetary Computer and Earth Search differ only in endpoint URL,
collection/band-asset names, and whether asset hrefs need signing — one
implementation serves both via a `StacConfig`.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from data_loader.masking import MASKS
from data_loader.product_contract import (
    ESA_S2_L2A,
    SCENE,
    AcquisitionProvenance,
    ProductIdentity,
    VersionCandidate,
    select_processing_versions,
)
from data_loader.providers.base import Grid, SceneRef

CANONICAL_BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")


@dataclass(frozen=True)
class SensorStacSpec:
    collection: str
    product_family: str  # data_loader.product_contract.{USGS_C2_L2, ESA_S2_L2A, ...}
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


# Matches the full USGS Landsat Collection 2 product id, e.g.
# "LC08_L2SP_046029_20230715_20230724_02_T1" -- present in asset hrefs
# (both PC and Earth Search) but not exposed as its own STAC property by
# either provider, unlike Sentinel-2's `s2:product_uri`.
_LANDSAT_PRODUCT_ID_RE = re.compile(r"(L[A-Z]\d{2}_L\d[A-Z]{2}_\d{6}_\d{8}_\d{8}_\d{2}_[A-Z0-9]{2})")

# Sentinel-2 `s2:datatake_id` looks like "GS2A_20200719T190921_026508_N02.14"
# -- the trailing "_N<baseline>" is the only part that changes between
# reprocessing versions of the same acquisition, so stripping it gives a
# stable per-acquisition (not per-processing-run) key.
_DATATAKE_BASELINE_SUFFIX_RE = re.compile(r"_N\d{2}\.\d{2}$")

# Matches the class of GDAL/rasterio read failures consistent with an
# expired or otherwise invalid signed URL (Planetary Computer's SAS tokens
# on Azure Blob Storage) -- as opposed to a genuinely absent asset, a
# malformed request, or unrelated network flakiness, none of which a
# re-sign would fix. Deliberately broad (HTTP-status text varies by GDAL
# version/driver) but scoped to auth-shaped failures specifically so we
# don't blindly retry-forever on e.g. a 404.
_AUTH_FAILURE_RE = re.compile(
    r"(HTTP\s*(response\s*code[: ]*)?40[13]\b|"
    r"AuthenticationFailed|ExpiredAuthenticationToken|ExpiredToken|"
    r"SignatureDoesNotMatch|Signature not valid|Forbidden|Unauthorized)",
    re.IGNORECASE,
)


def _looks_like_expired_auth(exc: Exception) -> bool:
    return bool(_AUTH_FAILURE_RE.search(str(exc)))


# Matches GDAL/rasterio/libcurl failures consistent with an ordinary
# transient network hiccup -- a timeout, a dropped/reset connection, a
# DNS blip -- as opposed to an auth problem (_AUTH_FAILURE_RE, handled
# separately by re-signing) or a genuinely missing/invalid asset (never
# retried). Discovered live (2026-09-20) during a real multi-hundred-read
# benchmark: `RasterioIOError: CURL error: Recv failure: Operation timed
# out` was NOT retried by the auth-only logic that existed at the time,
# so a single transient network blip failed that scene outright. Also
# matches WarpedVRT's own generic wrapper text ("Read failed. See
# previous exception for details.") -- GDAL sometimes loses the specific
# libcurl error message when a warp's internal block read fails, so this
# vague wrapper is itself treated as presumptively transient (bounded
# retry either way, so misclassifying a rarer non-transient cause here
# just costs a few retried attempts before the same terminal error
# surfaces, not an infinite loop). Applies to EVERY StacProvider
# (Planetary Computer AND Earth Search) regardless of `needs_signing`,
# unlike the auth-retry path -- a network timeout has nothing to do with
# whether a URL happens to be signed.
_TRANSIENT_NETWORK_RE = re.compile(
    r"(timed?\s*out|timeout|connection\s*reset|connection\s*refused|"
    r"could\s*n.t\s*connect|empty\s*reply\s*from\s*server|"
    r"server\s*returned\s*nothing|network\s*is\s*unreachable|"
    r"temporary\s*failure\s*in\s*name\s*resolution|could\s*not\s*resolve\s*host|"
    r"ssl\s*connect\s*error|ssl_error|eof\s*occurred\s*in\s*violation\s*of\s*protocol|"
    r"recv\s*failure|send\s*failure|broken\s*pipe|"
    r"read\s*failed\.\s*see\s*previous\s*exception)",
    re.IGNORECASE,
)


def _looks_like_transient_network_error(exc: Exception) -> bool:
    return bool(_TRANSIENT_NETWORK_RE.search(str(exc)))


def _extract_landsat_product_id(item) -> Optional[str]:
    for asset in item.assets.values():
        m = _LANDSAT_PRODUCT_ID_RE.search(asset.href)
        if m:
            return m.group(1)
    return None


def _landsat_generation_time(item, product_id: Optional[str]) -> Optional[str]:
    direct = item.properties.get("landsat:product_generated")
    if direct:
        return direct
    # Fall back to the processing-date segment embedded in the product id
    # (present even where the provider doesn't expose a dedicated
    # property, e.g. Planetary Computer) -- date precision only, no
    # time-of-day, since that's all the product id encodes.
    if product_id:
        parts = product_id.split("_")
        if len(parts) >= 5 and len(parts[4]) == 8 and parts[4].isdigit():
            proc_date = parts[4]
            return f"{proc_date[:4]}-{proc_date[4:6]}-{proc_date[6:]}"
    return None


def _s2_stable_key(properties: dict) -> tuple:
    datatake = properties.get("s2:datatake_id") or ""
    core = _DATATAKE_BASELINE_SUFFIX_RE.sub("", datatake)
    tile = properties.get("grid:code") or properties.get("mgrs:grid_square") or ""
    if core:
        return (core, tile)
    # s2:datatake_id missing is not expected from PC or Earth Search
    # (confirmed present for both), but degrade to datetime+tile rather
    # than crash if some other STAC source omits it.
    return (properties.get("datetime"), tile)


def _s2_version_candidates(items) -> list[VersionCandidate]:
    return [
        VersionCandidate(
            key=_s2_stable_key(it.properties),
            baseline=it.properties.get("s2:processing_baseline"),
            generation_time=it.properties.get("s2:generation_time"),
            payload=it,
        )
        for it in items
    ]


def _build_provenance(provider_name: str, item, spec: SensorStacSpec) -> AcquisitionProvenance:
    props = item.properties
    if spec.product_family == ESA_S2_L2A:
        upstream_product_id = props.get("s2:product_uri")
        processing_baseline = props.get("s2:processing_baseline")
        generation_time = props.get("s2:generation_time")
        extra = {
            "datatake_id": props.get("s2:datatake_id"),
            "grid_code": props.get("grid:code"),
            "granule_id": props.get("s2:granule_id"),
        }
    else:  # Landsat (usgs_c2_l2)
        upstream_product_id = _extract_landsat_product_id(item)
        # USGS Collection 2 doesn't expose a reprocessing-baseline concept
        # the way ESA does -- collection number/category (in `extra`) is
        # the closest analog, not a drop-in equivalent.
        processing_baseline = None
        generation_time = _landsat_generation_time(item, upstream_product_id)
        extra = {
            "wrs_path": props.get("landsat:wrs_path"),
            "wrs_row": props.get("landsat:wrs_row"),
            "collection_category": props.get("landsat:collection_category"),
            "collection_number": props.get("landsat:collection_number"),
            "scene_id": props.get("landsat:scene_id"),
        }
    return AcquisitionProvenance(
        provider=provider_name,
        provider_item_id=item.id,
        upstream_product_id=upstream_product_id,
        acquisition_datetime=item.datetime.isoformat() if item.datetime else None,
        platform=props.get("platform"),
        product_family=spec.product_family,
        collection=spec.collection,
        processing_baseline=processing_baseline,
        generation_time=generation_time,
        extra={k: v for k, v in extra.items() if v is not None},
    )


class StacProvider:
    def __init__(
        self, config: StacConfig, *, workers: int = 8, retries: int = 4,
        max_sign_retries: int = 2, max_transient_retries: int = 3,
    ):
        self.name = config.name
        self.config = config
        self.workers = workers
        self.retries = retries
        # Bounded number of RE-signs (not total attempts) permitted per
        # asset read when a read fails in a way that looks like an expired
        # signed URL -- see _looks_like_expired_auth / read_scene_bands.
        # Never unbounded: a genuinely broken/revoked credential must
        # surface as a failure, not spin forever.
        self.max_sign_retries = max_sign_retries
        # Bounded number of retries (with backoff, same href, no
        # re-signing) for ordinary transient network failures -- see
        # _looks_like_transient_network_error / read_scene_bands. Tracked
        # independently of max_sign_retries: a single asset read can hit
        # one failure class, the other, or both across its attempts, and
        # each class has its own bounded budget rather than sharing one
        # counter, so a transient blip can't eat into (or be starved by)
        # the signed-URL-refresh budget or vice versa.
        self.max_transient_retries = max_transient_retries
        self._client = None
        # Durable scene identity (STAC collection/item/asset) is what
        # search_scenes returns and what source_metadata()/provenance is
        # built from -- see module docstring and read_scene_bands below.
        # Signed hrefs are resolved lazily, only at read time, and cached
        # here (per (item id, asset key)) purely to avoid re-signing on
        # every band read of the same scene; a cache entry is only ever
        # replaced reactively, on a read failure that looks like an
        # expired token, never proactively. Shared across worker threads
        # when scene-level concurrency is in use, hence the lock.
        self._signed_href_cache: dict[tuple[str, str], str] = {}
        self._sign_lock = threading.Lock()
        # Populated by the most recent search_scenes call when its sensor's
        # product_family does version selection (currently Sentinel-2) --
        # acquisitions discovered but not selected under the requested
        # processing_version_policy. Read by engine.py right after calling
        # search_scenes if it wants to record the exclusion in the
        # manifest; not part of the Provider protocol itself since it's a
        # side channel for one optional piece of provenance, not core
        # behavior every provider needs to implement.
        self.last_excluded_versions: list[dict] = []

    def capabilities(self) -> dict[str, ProductIdentity]:
        return {
            sensor: ProductIdentity(sensor=sensor, product_family=spec.product_family, temporal_product=SCENE)
            for sensor, spec in self.config.sensors.items()
        }

    def processing_profile(self, sensor: str) -> dict:
        spec = self.config.sensors[sensor]
        return {
            "sr_scale": spec.sr_scale,
            "sr_offset": spec.sr_offset,
            "qa_policy": spec.qa_kind,
            "reflectance_resampling": "bilinear",
            "categorical_resampling": "nearest",
        }

    def source_metadata(self, scene: SceneRef) -> Optional[dict]:
        """The full selected STAC Item, verbatim. Its durable identity is
        `collection` + `id` (plus any DOI/links inside `properties`/
        `links`) -- NOT `assets.*.href`. Planetary Computer's hrefs are
        SAS-signed at fetch time and expire in hours; Earth Search's are
        plain public S3/HTTPS paths and stay valid indefinitely. Either
        way, the href present here is a snapshot of what was returned at
        capture time, not something to treat as a permanent identifier."""
        return scene.handle.to_dict()

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

        # Deliberately no `modifier=pc.sign_inplace` here (unlike this
        # provider's earlier design): signing at search time bakes a
        # short-lived SAS token into the item that outlives the token's
        # validity on any job longer than a few hours. Items returned by
        # search_scenes stay unsigned -- their durable identity is
        # collection + id + asset keys, not a signed href -- and signing
        # happens lazily, per asset, at read time (see
        # _get_signed_href/read_scene_bands), where it can also be
        # refreshed and retried if the token has since expired.
        self._client = Client.open(self.config.stac_url)
        return self._client

    def _get_signed_href(self, item, asset_key: str, *, force_refresh: bool = False) -> str:
        """Resolve the actual URL to open for `asset_key` on `item`. Never
        mutates `item`/`item.assets[...]` itself -- that object is the
        scene's durable STAC identity (also what source_metadata() and the
        provenance snapshot capture) and must stay stable regardless of how
        many times its assets get (re-)signed for reading."""
        href = item.assets[asset_key].href
        if not self.config.needs_signing:
            return href

        cache_key = (item.id, asset_key)
        with self._sign_lock:
            if not force_refresh:
                cached = self._signed_href_cache.get(cache_key)
                if cached is not None:
                    return cached
            import planetary_computer as pc

            signed = pc.sign(href)
            self._signed_href_cache[cache_key] = signed
            return signed

    def search_scenes(
        self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent,
        processing_version_policy="any",
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

        self.last_excluded_versions = []
        if spec.product_family == ESA_S2_L2A:
            candidates = _s2_version_candidates(items)
            selected = select_processing_versions(candidates, processing_version_policy)
            selected_ids = {id(it) for it in selected}
            self.last_excluded_versions = [
                {
                    "provider_item_id": it.id,
                    "upstream_product_id": it.properties.get("s2:product_uri"),
                    "processing_baseline": it.properties.get("s2:processing_baseline"),
                    "generation_time": it.properties.get("s2:generation_time"),
                    "reason": (
                        f"not selected under processing_version_policy="
                        f"{processing_version_policy!r}"
                    ),
                }
                for it in items
                if id(it) not in selected_ids
            ]
            items = selected

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
                    provenance=_build_provenance(self.name, it, spec),
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
            # Two independent, bounded retry budgets -- see __init__'s
            # docstring on max_transient_retries for why these are tracked
            # separately rather than sharing one counter. Never unbounded
            # either way: both loops terminate (raise) once their own
            # budget is exhausted, regardless of what the other failure
            # class has done.
            sign_retries_done = 0
            transient_retries_done = 0
            force_refresh = False
            while True:
                href = self._get_signed_href(item, asset_key, force_refresh=force_refresh)
                force_refresh = False
                try:
                    with rasterio.open(href) as src, WarpedVRT(
                        src,
                        crs=grid.crs,
                        transform=grid.transform,
                        width=grid.width,
                        height=grid.height,
                        resampling=resampling,
                    ) as vrt:
                        return vrt.read(1)
                except Exception as e:
                    if (
                        self.config.needs_signing
                        and _looks_like_expired_auth(e)
                        and sign_retries_done < self.max_sign_retries
                    ):
                        sign_retries_done += 1
                        force_refresh = True
                        print(
                            f"[{self.name}] {item.id}/{asset_key}: read failed "
                            f"(sign retry {sign_retries_done}/{self.max_sign_retries}), "
                            f"looks like an expired signed URL -- re-signing and retrying: {e}"
                        )
                        continue
                    if (
                        _looks_like_transient_network_error(e)
                        and transient_retries_done < self.max_transient_retries
                    ):
                        transient_retries_done += 1
                        backoff_s = 2 * transient_retries_done
                        print(
                            f"[{self.name}] {item.id}/{asset_key}: read failed "
                            f"(transient-network retry {transient_retries_done}/"
                            f"{self.max_transient_retries}), retrying in {backoff_s}s: {e}"
                        )
                        time.sleep(backoff_s)
                        continue
                    raise

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

"""USGS Landsat Collection 2 U.S. Analysis Ready Data (ARD), Surface
Reflectance -- direct from USGS, no AWS involvement.

Deliberately a SEPARATE provider/product from usgs_m2m's scene-based
USGS_C2_L2 product -- see product_contract.USGS_ARD_SR for why an ARD tile
is not interchangeable with a normal Collection 2 Level-2 scene.

ACCESS MODEL (all verified live 2026-09-18 against a real EROS account):

Discovery and read use two different USGS services, deliberately:

- **Discovery** goes through the public LandsatLook STAC API
  (https://landsatlook.usgs.gov/stac-server, collection `landsat-c2ard-sr`)
  because it is anonymous, fast (~0.8s for a 3.5-month AOI query), and
  returns richer, already-normalized per-tile metadata (full acquisition
  timestamp, ARD grid h/v, `landsat:scene_count`, projection WKT2) than
  M2M's `scene-search`, whose `temporalCoverage` is date-only. No
  credentials needed to enumerate what exists.
- **Reading** goes through the M2M API, which mints short-lived signed
  `landsatlook.usgs.gov/tile/...?requestSignature=...` URLs
  (`download-options` -> product `D771` "C2 ARD Tile Band Download" ->
  `download-request`). This is the direct-USGS read path and needs an EROS
  account + M2M application token.

Why reading can't just use the STAC asset href: LandsatLook's *unsigned*
`https://landsatlook.usgs.gov/tile/...` href redirects to an EROS login
page (HTTP 302 -> ers.cr.usgs.gov, verified live), and each STAC asset's
`alternate.s3.href` points at `s3://usgs-landsat-ard`, a **Requester Pays**
bucket (anonymous S3 GET returns `AccessDenied: Anonymous users cannot
invoke requests against Requester Pays buckets`) that bills a third-party
AWS account. The M2M signed URL is the only path that reads ARD bytes
directly from USGS without AWS billing. See `_signed_band_urls`.

INDIVIDUAL BANDS, NOT BUNDLES (the key finding): M2M's `D771` product
exposes each file of a tile as its own `secondaryDownloads` entry (42 for
a Landsat 9 tile: SR/TOA/BT/ST bands, QA bands, per-product STAC JSON,
angle bands, browse images). `download-request` accepts many of them in
one call and returns every URL immediately with an empty
`preparingDownloads` -- no staging/polling queue, unlike the scene-based
bundle flow in usgs_m2m.py. So DataLoader requests exactly the 7 files it
needs (6 SR bands + QA_PIXEL, ~213 MB/tile) rather than the ~750 MB of all
42, and never touches the `D773` "Surface Reflectance Bundle Download"
(a 213 MB .tar for the SR product alone).

Those signed URLs additionally serve `Accept-Ranges: bytes` and answer
range requests with HTTP 206, and the underlying files are real COGs
(256x256 internal tiles, 6 overview levels) -- so reads of a SUBSET of a
tile (or onto any other grid) are windowed through GDAL/rasterio's
/vsicurl/, transferring only the bytes covering the AOI, exactly like the
Planetary Computer path. This is a fundamentally lighter access pattern
than usgs_m2m.py's scene bundles (download whole .tar -> extract -> read).

FULL-TILE READS ON THE NATIVE GRID download whole band files instead (see
_read_native_tile). Measured 2026-10-02 on h003v004: /vsicurl reads one
full tile's 7 files in ~110 s (GDAL issues many small range requests,
one band after another, each paying round-trip latency), while plain
whole-file GETs of the same 7 files, in parallel, take ~5-6 s and decode
from memory in ~2 s -- bit-identical output, since a native-grid
"warp" is the identity. The signed URLs serve ~10-28 MB/s per stream
(CloudFront in front of landsatlook), so per-request latency, not
bandwidth, was the bottleneck.

Other live-verified facts:
- Bands `blue`/`green`/`red`/`nir08`/`swir16`/`swir22`/`qa_pixel` use the
  SAME canonical STAC asset keys as Planetary Computer/Earth Search, and
  (checked directly) are identical across TM/ETM (Landsat 4/5/7) and OLI
  (Landsat 8/9) ARD items. The underlying band FILES that M2M serves are
  not: they use each sensor's native band numbers, so reads go through a
  per-sensor suffix map (see BAND_FILE_SUFFIX_BY_SENSOR).
- QA_PIXEL is the standard Collection 2 bitmask, so
  `masking.landsat_qa_mask` applies unchanged.
- Grid: fixed national Albers Equal-Area Conic ARD grid, 5000x5000px at
  30m, addressed by `landsat:grid_horizontal`/`landsat:grid_vertical` and
  region (`CU` CONUS / `AK` / `HI`). `proj:epsg` is null (custom WKT2, no
  EPSG code); reads reproject from the source CRS via WarpedVRT, so the
  missing EPSG code costs nothing at read time.
- One ARD tile can mosaic several WRS-2 scenes from the same overpass
  (`landsat:scene_count`, commonly 2-3) -- seconds apart, not a multi-day
  composite (which is what makes it a `scene` temporal_product here, unlike
  GLAD ARD's genuine 16-day `fixed_composite`).
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timezone
from typing import Optional

import numpy as np

from data_loader.masking import landsat_qa_mask
from data_loader.product_contract import (
    SCENE,
    USGS_ARD_SR,
    AcquisitionProvenance,
    ProductIdentity,
    parse_version_policy,
)
from data_loader.providers.base import Grid, SceneRef

STAC_URL = "https://landsatlook.usgs.gov/stac-server"
COLLECTION = "landsat-c2ard-sr"

M2M_BASE_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"
M2M_DATASET = "landsat_ard_tile_c2"
# "C2 ARD Tile Band Download" -- the per-file product whose
# secondaryDownloads are individual COGs. NOT D773 ("...Surface
# Reflectance Bundle Download"), which is a single ~213 MB .tar.
BAND_DOWNLOAD_PRODUCT_CODE = "D771"

# M2M auth keys expire ~2h after last use; re-login well before that so a
# long run doesn't die mid-job (same policy as usgs_m2m.py).
RELOGIN_AFTER = 90 * 60

# Canonical DataLoader band name -> the file suffix of that band's ARD COG.
# ARD files are named <tile_product_id>_<suffix>, e.g.
# LC09_CU_003004_20230909_20230914_02_SR_B2.TIF. Band numbers are each
# sensor's NATIVE numbers, so the map is per sensor: OLI/OLI-2 (Landsat 8/9)
# use B2=blue..B7=swir2, while TM/ETM+ (Landsat 4/5/7) use B1=blue..B5=swir1,
# B7=swir2 and have no SR_B6 (TM/ETM+ band 6 is thermal, shipped as ST_B6).
# Verified live 2026-09-28 against M2M download-options file lists for
# LT04/LT05/LE07/LC08 tiles of h003v004. (An earlier version of this map
# assumed OLI numbering for every sensor, which made TM/ETM+ reads either
# fail on the missing SR_B6 or -- without swir1 -- silently return the
# wrong bands.)
_OLI_SUFFIX = {
    "blue": "SR_B2", "green": "SR_B3", "red": "SR_B4",
    "nir": "SR_B5", "swir1": "SR_B6", "swir2": "SR_B7",
    "qa": "QA_PIXEL",
}
_TM_ETM_SUFFIX = {
    "blue": "SR_B1", "green": "SR_B2", "red": "SR_B3",
    "nir": "SR_B4", "swir1": "SR_B5", "swir2": "SR_B7",
    "qa": "QA_PIXEL",
}
BAND_FILE_SUFFIX_BY_SENSOR = {
    "LT04": _TM_ETM_SUFFIX, "LT05": _TM_ETM_SUFFIX, "LE07": _TM_ETM_SUFFIX,
    "LC08": _OLI_SUFFIX, "LC09": _OLI_SUFFIX,
}


def band_file_suffixes(scene_or_tile_id: str) -> dict[str, str]:
    """Canonical band name -> ARD file suffix for the sensor that produced
    this tile (the first 4 characters of its id, e.g. "LT05")."""
    try:
        return BAND_FILE_SUFFIX_BY_SENSOR[scene_or_tile_id[:4]]
    except KeyError:
        raise ValueError(f"[usgs_ard] unknown Landsat sensor prefix in {scene_or_tile_id!r}") from None
SR_SCALE, SR_OFFSET = 2.75e-5, -0.2

# ARD tile product id, e.g. "LC09_CU_003004_20230909_20230914_02":
# sensor, grid region, grid h+v, acquisition date, processing date,
# collection number. STAC item ids append a "_SR" product suffix that the
# M2M entityId does not carry -- see _tile_product_id.
_ARD_PRODUCT_ID_RE = re.compile(r"^(L[A-Z]\d{2})_([A-Z]{2})_(\d{3})(\d{3})_(\d{8})_(\d{8})_(\d{2})")


def _tile_product_id(stac_item_id: str) -> str:
    """STAC item id -> the tile product id M2M uses as its entityId.
    `LC09_CU_003004_20230909_20230914_02_SR` -> `LC09_..._02`. The STAC
    catalog splits one physical tile into per-product collections (SR/ST/
    BT/TA) and suffixes the item id accordingly; M2M treats the tile as
    one entity carrying all of them."""
    m = _ARD_PRODUCT_ID_RE.match(stac_item_id)
    if not m:
        raise ValueError(f"[usgs_ard] unrecognized ARD item id: {stac_item_id!r}")
    return m.group(0)


def _is_rate_limit_error(body: dict) -> bool:
    """M2M's documented single-request-at-a-time account restriction
    surfaces as errorCode RATE_LIMIT (verified live under concurrent
    workers: HTTP 500, errorMessage "Your account does not support
    multiple requests at a time"). The client-side _m2m_call_lock (see
    UsgsArdProvider.__init__) should prevent this from this process's own
    concurrency, but a bounded retry-with-backoff is kept as defense in
    depth against another process/session sharing the same account."""
    return body.get("errorCode") == "RATE_LIMIT"


def _in_season(d: date, season_start: Optional[str], season_end: Optional[str]) -> bool:
    if not season_start or not season_end:
        return True
    md = (d.month, d.day)
    s = tuple(int(x) for x in season_start.split("-"))
    e = tuple(int(x) for x in season_end.split("-"))
    return s <= md <= e if s <= e else (md >= s or md <= e)


def _build_provenance(item) -> AcquisitionProvenance:
    props = item.properties
    m = _ARD_PRODUCT_ID_RE.match(item.id)
    platform = (props.get("platform") or "").lower().replace("_", "-") or None  # LANDSAT_8 -> landsat-8
    return AcquisitionProvenance(
        provider="usgs_ard",
        provider_item_id=item.id,
        upstream_product_id=item.id,
        acquisition_datetime=props.get("datetime"),
        platform=platform,
        product_family=USGS_ARD_SR,
        collection=COLLECTION,
        processing_baseline=None,  # ARD has no ESA-style reprocessing-baseline concept
        generation_time=m.group(6) if m else None,  # processing-date segment of the tile product id
        extra={
            k: v for k, v in {
                "ard_tile_product_id": m.group(0) if m else None,
                "grid_region": props.get("landsat:grid_region"),
                "grid_horizontal": props.get("landsat:grid_horizontal"),
                "grid_vertical": props.get("landsat:grid_vertical"),
                # How many WRS-2 scenes from this overpass were mosaicked
                # into this tile -- the structural difference from a scene.
                "scene_count": props.get("landsat:scene_count"),
                "cloud_shadow_cover": props.get("landsat:cloud_shadow_cover"),
                "snow_ice_cover": props.get("landsat:snow_ice_cover"),
                "fill_percent": props.get("landsat:fill"),
                "start_datetime": props.get("start_datetime"),
                "end_datetime": props.get("end_datetime"),
                "proj_wkt2": props.get("proj:wkt2"),
                "proj_shape": props.get("proj:shape"),
                "proj_transform": props.get("proj:transform"),
            }.items() if v is not None
        },
    )


class UsgsArdProvider:
    name = "usgs_ard"

    def __init__(self, username: Optional[str] = None, token: Optional[str] = None, retries: int = 4):
        import os

        self.username = username or os.environ.get("USGS_M2M_USERNAME")
        self.token = token or os.environ.get("USGS_M2M_TOKEN")
        self.retries = retries
        self._client = None
        self._session = None
        self._session_born = None
        # Guards M2M login (the shared session/token) -- intentionally ONE
        # global lock, since login is genuinely one shared piece of state
        # every thread reuses and must not race to refresh independently.
        self._auth_lock = threading.Lock()
        # Serializes the ACTUAL M2M network call in _m2m_post, globally,
        # across every endpoint and every tile -- verified live under
        # scene-level concurrency (2+ workers) that M2M itself rejects
        # concurrent requests from one account: `download-options` returns
        # HTTP 500 `RATE_LIMIT: Your account does not support multiple
        # requests at a time`. This is a hard server-side account
        # restriction, not a client tuning choice, so it cannot be
        # loosened by widening this lock -- unlike _tile_locks (which
        # exists precisely to let unrelated tiles proceed independently),
        # M2M calls for DIFFERENT tiles must still queue behind ONE
        # another. The per-tile lock above still avoids redundant minting
        # for the same tile; this lock separately ensures at most one M2M
        # HTTP call is in flight at all times. The actual COG byte reads
        # (rasterio/WarpedVRT against an already-minted signed URL) do NOT
        # go through M2M and are not serialized by this lock -- only
        # URL-minting queues; reads still proceed in parallel.
        self._m2m_call_lock = threading.Lock()
        # Per-tile locks for URL-minting, NOT one global lock: minting is a
        # network round trip (download-options + download-request), and
        # concurrent workers routinely read DIFFERENT tiles at once
        # (engine.config.workers) -- serializing that behind a single lock
        # would force every worker's M2M calls onto one thread at a time
        # regardless of which tile each is reading, making the provider
        # artificially non-concurrent from a client-side bug rather than
        # any real M2M limit. `_tile_locks_guard` only protects
        # get-or-create of a tile's own lock object (cheap, no I/O); the
        # actual network call in _signed_band_urls runs under that tile's
        # lock alone, so unrelated tiles' mints proceed fully in parallel
        # while repeated requests for the SAME tile still coalesce to one
        # network round trip.
        self._tile_locks_guard = threading.Lock()
        self._tile_locks: dict[str, threading.Lock] = {}
        # (tile_product_id, band_suffix) -> signed URL. Signed URLs are
        # short-lived, so this only avoids re-minting within one run's
        # reads of the same tile; a stale-URL read failure re-mints (see
        # read_scene_bands). Safe to read/write under the per-tile lock
        # since all writers for one tile serialize on that tile's lock.
        self._signed_url_cache: dict[tuple[str, str], str] = {}

    def capabilities(self) -> dict[str, ProductIdentity]:
        return {"landsat": ProductIdentity(sensor="landsat", product_family=USGS_ARD_SR, temporal_product=SCENE)}

    def processing_profile(self, sensor: str) -> dict:
        return {
            "sr_scale": SR_SCALE,
            "sr_offset": SR_OFFSET,
            "qa_policy": "landsat_qa_pixel",
            "reflectance_resampling": "bilinear",
            "categorical_resampling": "nearest",
            # Informative only (never used for contract matching) -- a
            # caller weighing ARD's fixed tiling needs to know the native
            # grid is not a per-scene UTM footprint.
            "nativeGrid": "Fixed national Albers Equal-Area Conic ARD tile grid (5000x5000px @ 30m)",
            "accessPath": "USGS M2M signed URLs (direct from USGS; no AWS)",
        }

    def source_metadata(self, scene: SceneRef) -> Optional[dict]:
        """The full LandsatLook STAC item, verbatim -- every ARD-specific
        property (tile grid, scene_count, projection WKT2, per-asset
        checksums) including fields not captured in the normalized
        AcquisitionProvenance. Same rationale as
        stac_common.StacProvider.source_metadata: the signed M2M read URLs
        are ephemeral and deliberately NOT part of durable identity, so
        they are not recorded here."""
        return scene.handle.to_dict()

    # -- discovery (public STAC, no credentials) ---------------------------

    def _open_client(self):
        if self._client is not None:
            return self._client
        from pystac_client import Client

        self._client = Client.open(STAC_URL)
        return self._client

    def search_scenes(
        self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent,
        processing_version_policy="any",
    ) -> list[SceneRef]:
        if sensor != "landsat":
            raise ValueError(f"usgs_ard only supports sensor='landsat' (got {sensor!r})")
        # Each ARD tile+date is produced once; no reprocessing-baseline
        # concept exists to pin (same posture as usgs_m2m/gee for Landsat).
        version_kind, _ = parse_version_policy(processing_version_policy)
        if version_kind == "pinned":
            raise ValueError(
                "[usgs_ard] processing_version_policy='pinned:...' is not "
                "supported -- ARD tiles have no reprocessing-baseline concept."
            )

        cat = self._open_client()
        items = None
        for attempt in range(self.retries):
            try:
                items = list(
                    cat.search(
                        collections=[COLLECTION],
                        bbox=bbox,
                        datetime=f"{start.isoformat()}/{end.isoformat()}",
                        query={"eo:cloud_cover": {"lt": max_cloud_percent}},
                    ).items()
                )
                break
            except Exception as e:  # LandsatLook's STAC endpoint can be flaky; retry with backoff
                print(f"[{self.name}] STAC search retry {attempt + 1}/{self.retries}: {e}")
                time.sleep(2 * (attempt + 1))
        if items is None:
            raise RuntimeError(f"[{self.name}] STAC search failed after {self.retries} attempts")

        refs = []
        for it in items:
            d = it.datetime.astimezone(timezone.utc).date() if it.datetime else None
            if d is None or not _in_season(d, season_start, season_end):
                continue
            refs.append(SceneRef(
                id=it.id, date=d, cloud_percent=it.properties.get("eo:cloud_cover"),
                handle=it, provenance=_build_provenance(it),
            ))
        return refs

    # -- read (M2M signed URLs, direct from USGS) --------------------------

    def _ensure_login(self):
        with self._auth_lock:
            if self._session is not None and time.time() - self._session_born < RELOGIN_AFTER:
                return self._session
            if not self.username or not self.token:
                raise RuntimeError(
                    "[usgs_ard] reading ARD bands needs USGS EROS credentials "
                    "-- pass provider_options: {usgs_username: \"...\", "
                    "usgs_token: \"...\"}, or set USGS_M2M_USERNAME / "
                    "USGS_M2M_TOKEN. Generate an application token at "
                    "https://ers.cr.usgs.gov (M2M API access must also be "
                    "approved on the account). Discovery (search_scenes) "
                    "needs no credentials; only reading bytes does."
                )
            import requests

            session = self._session or requests.Session()
            # login-token is an M2M request like any other: it must queue
            # behind _m2m_call_lock, and a RATE_LIMIT answer (another
            # process on the same account) is retried. Found live
            # 2026-10-02: the 90-min re-login raced in-flight calls from
            # other threads, got RATE_LIMIT, and failed 9 reads outright.
            for attempt in range(self.retries):
                with self._m2m_call_lock:
                    resp = session.post(
                        M2M_BASE_URL + "login-token",
                        json={"username": self.username, "token": self.token},
                        timeout=180,
                    )
                body = resp.json()
                if not _is_rate_limit_error(body):
                    break
                print(f"[{self.name}] M2M login-token rate-limit retry {attempt + 1}/{self.retries}: "
                      f"{body.get('errorCode')}: {body.get('errorMessage')}")
                time.sleep(2 * (attempt + 1))
            if body.get("errorCode") or not resp.ok:
                raise RuntimeError(
                    f"[usgs_ard] M2M login-token failed (HTTP {resp.status_code}): "
                    f"{body.get('errorCode')}: {body.get('errorMessage')}"
                )
            session.headers.update({"X-Auth-Token": body["data"]})
            self._session = session
            self._session_born = time.time()
            return self._session

    def _m2m_post(self, endpoint: str, payload: dict) -> dict:
        # m2m.cr.usgs.gov intermittently refuses connections outright
        # (observed live: ConnectionRefused for ~a minute, then recovery),
        # so transport-level failures get a bounded backoff retry. API-level
        # errors (errorCode in a valid JSON body) are NOT retried -- those
        # are deterministic and would just fail again.
        import requests

        # Missing credentials is a configuration error, not a transient
        # one -- surface it directly rather than letting the retry loop
        # below bury it behind an "unreachable after N attempts" message.
        if not self.username or not self.token:
            self._ensure_login()

        last_exc = None
        for attempt in range(self.retries):
            try:
                session = self._ensure_login()
                # Only one M2M HTTP call in flight at a time, globally --
                # see _m2m_call_lock's docstring in __init__. This is the
                # actual fix for the account-level RATE_LIMIT restriction;
                # the retry below is defense in depth, not the primary
                # mechanism.
                with self._m2m_call_lock:
                    resp = session.post(M2M_BASE_URL + endpoint, json=payload, timeout=180)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                # A refused connection can also mean the cached session is
                # stale; drop it so the next attempt re-logs in.
                with self._auth_lock:
                    self._session = None
                    self._session_born = None
                print(f"[{self.name}] M2M {endpoint} transport retry {attempt + 1}/{self.retries}: {e}")
                time.sleep(3 * (attempt + 1))
                continue

            try:
                body = resp.json()
            except ValueError:
                raise RuntimeError(
                    f"[usgs_ard] M2M {endpoint}: non-JSON response "
                    f"(HTTP {resp.status_code}): {resp.text[:500]}"
                )
            if _is_rate_limit_error(body):
                last_exc = RuntimeError(f"{body.get('errorCode')}: {body.get('errorMessage')}")
                print(f"[{self.name}] M2M {endpoint} rate-limit retry {attempt + 1}/{self.retries}: {last_exc}")
                time.sleep(2 * (attempt + 1))
                continue
            if body.get("errorCode") or not resp.ok:
                raise RuntimeError(
                    f"[usgs_ard] M2M {endpoint} failed (HTTP {resp.status_code}): "
                    f"{body.get('errorCode')}: {body.get('errorMessage')}"
                )
            return body["data"]
        raise RuntimeError(
            f"[usgs_ard] M2M {endpoint} unreachable/rate-limited after {self.retries} attempts: {last_exc}"
        )

    def _signed_band_urls(self, tile_product_id: str, band_suffixes: list[str]) -> dict[str, str]:
        """Mint signed read URLs for specific bands of one ARD tile, in ONE
        batched `download-request` (verified live: 7 bands returned in
        ~1.6s with an empty `preparingDownloads`, i.e. no staging queue).

        Only the requested files are ever requested -- this is what makes
        the direct-USGS path per-band rather than per-bundle."""
        options = self._m2m_post(
            "download-options",
            {"datasetName": M2M_DATASET, "entityIds": [tile_product_id]},
        )
        band_option = next(
            (o for o in options if o.get("productCode") == BAND_DOWNLOAD_PRODUCT_CODE), None
        )
        if band_option is None:
            raise RuntimeError(
                f"[usgs_ard] tile {tile_product_id}: no per-band download "
                f"product ({BAND_DOWNLOAD_PRODUCT_CODE}) in download-options "
                f"({[o.get('productCode') for o in options]}) -- only bundle "
                "products would be available, which this provider "
                "deliberately does not use."
            )

        wanted = {f"{tile_product_id}_{suffix}.TIF": suffix for suffix in band_suffixes}
        selected = [
            sd for sd in band_option.get("secondaryDownloads", [])
            if sd.get("entityId") in wanted
        ]
        missing = set(wanted) - {sd["entityId"] for sd in selected}
        if missing:
            raise RuntimeError(
                f"[usgs_ard] tile {tile_product_id}: requested band file(s) "
                f"{sorted(missing)} not offered by M2M for this tile."
            )

        data = self._m2m_post("download-request", {
            "downloads": [{"entityId": sd["entityId"], "productId": sd["id"]} for sd in selected],
            # M2M rejects labels over 50 chars (INPUT_INVALID), and a full
            # ARD tile product id alone is 34 -- so key the label on a hash
            # of the tile id plus a timestamp rather than the id itself.
            "label": f"dl_ard_{abs(hash(tile_product_id)) % 10**8}_{int(time.time())}",
        })
        urls = {
            wanted[a["entityId"]]: a["url"]
            for a in data.get("availableDownloads", [])
            if a.get("entityId") in wanted
        }
        still_missing = set(band_suffixes) - set(urls)
        if still_missing:
            # `preparingDownloads` would mean USGS queued a staging job --
            # not observed for D771 band files, so surface it loudly rather
            # than silently polling.
            raise RuntimeError(
                f"[usgs_ard] tile {tile_product_id}: M2M did not return "
                f"immediate URLs for {sorted(still_missing)} "
                f"(preparingDownloads={len(data.get('preparingDownloads') or [])}). "
                "Band downloads are expected to be immediately available."
            )
        return urls

    def _lock_for_tile(self, tile_product_id: str) -> threading.Lock:
        with self._tile_locks_guard:
            lock = self._tile_locks.get(tile_product_id)
            if lock is None:
                lock = threading.Lock()
                self._tile_locks[tile_product_id] = lock
            return lock

    def _band_url(self, tile_product_id: str, suffix: str, needed: list[str], *, force_refresh: bool = False) -> str:
        # Only this tile's lock is held during the network call -- a
        # concurrent read of a DIFFERENT tile acquires a different lock via
        # _lock_for_tile and proceeds independently. See __init__.
        with self._lock_for_tile(tile_product_id):
            key = (tile_product_id, suffix)
            if not force_refresh and key in self._signed_url_cache:
                return self._signed_url_cache[key]
            # Mint every band this read will need in one round trip, not
            # one call per band.
            urls = self._signed_band_urls(tile_product_id, needed)
            for s, u in urls.items():
                self._signed_url_cache[(tile_product_id, s)] = u
            return self._signed_url_cache[key]

    def read_scene_bands(self, scene: SceneRef, sensor: str, bands, grid: Grid, pixel_cloud_mask: bool):
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.vrt import WarpedVRT

        from data_loader.providers.stac_common import _looks_like_expired_auth

        tile_product_id = _tile_product_id(scene.id)
        suffix_for = band_file_suffixes(tile_product_id)
        needed = [suffix_for[b] for b in bands]
        if pixel_cloud_mask:
            needed = needed + [suffix_for["qa"]]

        def read_asset(suffix, resampling):
            # One re-mint attempt: signed URLs are short-lived, so a long
            # job can hit an expired signature mid-run -- the same failure
            # class the Planetary Computer provider handles, with the same
            # bounded (never unlimited) retry posture.
            for attempt in range(2):
                url = self._band_url(tile_product_id, suffix, needed, force_refresh=attempt > 0)
                try:
                    with rasterio.open("/vsicurl/" + url) as src, WarpedVRT(
                        src, crs=grid.crs, transform=grid.transform,
                        width=grid.width, height=grid.height, resampling=resampling,
                    ) as vrt:
                        return vrt.read(1)
                except Exception as e:
                    if attempt == 0 and _looks_like_expired_auth(e):
                        print(
                            f"[{self.name}] {tile_product_id}/{suffix}: read failed, "
                            f"looks like an expired signed URL -- re-minting and retrying: {e}"
                        )
                        continue
                    raise

        resampling = {suffix_for[b]: Resampling.bilinear for b in bands}
        if pixel_cloud_mask:
            resampling[suffix_for["qa"]] = Resampling.nearest
        if _is_native_tile_grid(scene, grid):
            dn_by_suffix = self._read_native_tile(tile_product_id, needed, grid, resampling)
            read = dn_by_suffix.__getitem__
        else:
            read = lambda suffix: read_asset(suffix, resampling[suffix])

        out: dict[str, np.ndarray] = {}
        for b in bands:
            dn = read(suffix_for[b])
            out[b] = dn.astype("f4") * SR_SCALE + SR_OFFSET

        if pixel_cloud_mask:
            qa = read(suffix_for["qa"])
            bad = landsat_qa_mask(qa)
            for arr in out.values():
                arr[bad] = np.nan
        return out


    # -- full-tile reads on the native grid --------------------------------

    def _download_band(self, tile_product_id: str, suffix: str, needed: list[str]) -> bytes:
        """One whole band file over its signed URL. Re-mints once on an
        auth-shaped failure (expired signature), and retries transient
        transport failures with backoff -- both bounded."""
        import requests

        refreshed = False
        attempt = 0
        while True:
            url = self._band_url(tile_product_id, suffix, needed, force_refresh=refreshed and attempt == 0)
            try:
                r = requests.get(url, timeout=(30, 300))
                if r.status_code in (401, 403) and not refreshed:
                    print(f"[{self.name}] {tile_product_id}/{suffix}: HTTP {r.status_code}, "
                          "looks like an expired signed URL -- re-minting and retrying")
                    refreshed, attempt = True, 0
                    continue
                r.raise_for_status()
                expected = r.headers.get("Content-Length")
                if expected is not None and int(expected) != len(r.content):
                    raise requests.ConnectionError(
                        f"truncated body: {len(r.content)} of {expected} bytes")
                return r.content
            except (requests.ConnectionError, requests.Timeout) as e:
                err = e
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code < 500:
                    raise
                err = e
            attempt += 1
            if attempt >= self.retries:
                raise RuntimeError(
                    f"[{self.name}] {tile_product_id}/{suffix}: download failed after "
                    f"{attempt} attempts: {err}") from err
            print(f"[{self.name}] {tile_product_id}/{suffix} transport retry {attempt}/{self.retries}: {err}")
            time.sleep(2 ** attempt)

    def _read_native_tile(self, tile_product_id: str, needed: list[str], grid: Grid,
                          resampling: dict) -> dict[str, np.ndarray]:
        """Whole-file GETs of every needed band, in parallel, decoded from
        memory. Each decoded file's grid is checked against `grid`; any
        mismatch (which the STAC proj:* precheck should already rule out)
        falls back to warping the in-memory file, so the result never
        depends on that precheck being right."""
        from rasterio.crs import CRS
        from rasterio.io import MemoryFile
        from rasterio.vrt import WarpedVRT

        # Mint every band's URL here, on the caller's thread, so the
        # download threads only hit the cache (and callers that time
        # minting per thread still see it).
        self._band_url(tile_product_id, needed[0], needed)
        with ThreadPoolExecutor(len(needed)) as ex:
            blobs = dict(zip(needed, ex.map(
                lambda s: self._download_band(tile_product_id, s, needed), needed)))

        out = {}
        for suffix, blob in blobs.items():
            with MemoryFile(blob) as mem, mem.open() as src:
                if (src.width, src.height) == (grid.width, grid.height) \
                        and src.transform.almost_equals(grid.transform) \
                        and src.crs == CRS.from_user_input(grid.crs):
                    out[suffix] = src.read(1)
                else:
                    with WarpedVRT(src, crs=grid.crs, transform=grid.transform, width=grid.width,
                                   height=grid.height, resampling=resampling[suffix]) as vrt:
                        out[suffix] = vrt.read(1)
            blobs[suffix] = None  # free each file's bytes once decoded
        return out


def _is_native_tile_grid(scene: SceneRef, grid: Grid) -> bool:
    """Does `grid` cover exactly this ARD tile at its native resolution,
    per the STAC item's proj:shape/proj:transform? (Cheap precheck that
    selects the whole-file read path; CRS is verified after decoding.)"""
    from rasterio.transform import Affine

    props = getattr(scene.handle, "properties", None) or {}
    shape, transform = props.get("proj:shape"), props.get("proj:transform")
    if not shape or not transform or len(transform) < 6:
        return False
    return (tuple(shape) == (grid.height, grid.width)
            and Affine(*transform[:6]).almost_equals(grid.transform))


def make_provider(usgs_username: Optional[str] = None, usgs_token: Optional[str] = None, **kwargs) -> UsgsArdProvider:
    return UsgsArdProvider(username=usgs_username, token=usgs_token)

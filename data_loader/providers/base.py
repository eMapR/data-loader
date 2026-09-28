"""Common provider contract.

Every backend (STAC-based or GEE) exposes the same two required methods —
enumerate scenes matching an AOI/date/cloud filter, then read specific
bands for one scene onto a shared pixel grid — so engine.py can drive both
`scene` and `annual_composite` temporal modes identically regardless of
source. A provider may also implement `read_annual_composite` as a fast
path (e.g. GEE's server-side median reduce); engine.py falls back to
search+read+locally-reduce when a provider doesn't offer one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Protocol

from rasterio.transform import Affine

from data_loader.product_contract import AcquisitionProvenance, ProductIdentity


@dataclass(frozen=True)
class Grid:
    """Target pixel grid: CRS + affine transform + size, shared by every
    scene read in one engine run so bands/scenes stack without resampling
    surprises."""

    crs: str
    transform: Affine
    width: int
    height: int


@dataclass(frozen=True)
class SceneRef:
    """One scene as returned by a provider's search — enough to read its
    bands later without re-querying. `handle` is provider-specific (a
    signed pystac Item for STAC providers, a small dict for GEE).

    `provenance` is optional (defaults to None) so existing positional
    construction keeps working; providers should populate it wherever the
    source metadata allows -- see data_loader.product_contract.
    AcquisitionProvenance for what it captures and manifest.py/engine.py
    for how it ends up in the output manifest."""

    id: str
    date: date
    cloud_percent: Optional[float]
    handle: Any
    provenance: Optional[AcquisitionProvenance] = None


class Provider(Protocol):
    name: str

    def capabilities(self) -> dict[str, ProductIdentity]:
        """sensor name -> the ProductIdentity (product family + temporal
        structure) this provider actually supplies for that sensor. Static
        and free of I/O -- callable without credentials or network access.
        engine.py uses this to resolve/validate each request's product
        contract (data_loader.product_contract.resolve_contract) instead of
        assuming any provider serving a given `sensor` string is
        interchangeable with any other."""
        ...

    def processing_profile(self, sensor: str) -> dict:
        """Best-effort dict of DataLoader-processing facts this provider
        applies for `sensor` -- e.g. sr_scale/sr_offset, qa_policy,
        reflectance/categorical resampling method. Recorded in manifest.py
        as provenance, not used for contract matching. Keys a provider
        doesn't have a fact for are simply omitted."""
        ...

    def source_metadata(self, scene: "SceneRef") -> Optional[dict]:
        """The provider's own full source metadata record for this exact
        already-selected scene (e.g. a STAC Item's complete dict, a GEE
        image's getInfo() result, an M2M scene-search result) -- JSON-
        serializable, preserved verbatim by
        data_loader.metadata_snapshot.write_snapshot rather than reshaped.
        Called by engine.py once per scene actually included in output
        (not per search candidate), so a provider that needs a network
        call to build this (GEE) only pays it for selected scenes. Return
        None if no source record is available."""
        ...

    def search_scenes(
        self,
        bbox: tuple[float, float, float, float],
        sensor: str,
        start: date,
        end: date,
        season_start: Optional[str],
        season_end: Optional[str],
        max_cloud_percent: float,
        processing_version_policy: str = "any",
    ) -> list[SceneRef]:
        """Scenes matching bbox/date/season/cloud filters, newest info
        first isn't required — engine.py sorts/groups as needed.

        `processing_version_policy` (see data_loader.product_contract)
        governs how a provider that can return multiple upstream processing
        versions of the same acquisition (e.g. Earth Search's Sentinel-2
        reprocessing duplicates) resolves that down to one per acquisition.
        Providers that never return duplicates may accept and ignore it."""
        ...

    def read_scene_bands(
        self,
        scene: SceneRef,
        sensor: str,
        bands: list[str],
        grid: Grid,
        pixel_cloud_mask: bool,
    ) -> dict[str, "np.ndarray"]:
        """Canonical band name -> float32 array on `grid`, scaled to
        physical reflectance, with bad pixels set to NaN if
        `pixel_cloud_mask`."""
        ...

    def read_annual_composite(
        self,
        bbox: tuple[float, float, float, float],
        sensor: str,
        year: int,
        season_start: Optional[str],
        season_end: Optional[str],
        bands: list[str],
        grid: Grid,
        max_cloud_percent: float,
        pixel_cloud_mask: bool,
        reduce: str,
    ) -> Optional[dict[str, "np.ndarray"]]:
        """Optional fast path for one year's composite computed natively
        by the backend (e.g. server-side). Return None to signal "not
        supported" so engine.py falls back to its generic
        search_scenes + read_scene_bands + local reduce path."""
        return None

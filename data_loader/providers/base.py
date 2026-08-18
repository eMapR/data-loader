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
    signed pystac Item for STAC providers, a small dict for GEE)."""

    id: str
    date: date
    cloud_percent: Optional[float]
    handle: Any


class Provider(Protocol):
    name: str

    def search_scenes(
        self,
        bbox: tuple[float, float, float, float],
        sensor: str,
        start: date,
        end: date,
        season_start: Optional[str],
        season_end: Optional[str],
        max_cloud_percent: float,
    ) -> list[SceneRef]:
        """Scenes matching bbox/date/season/cloud filters, newest info
        first isn't required — engine.py sorts/groups as needed."""
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

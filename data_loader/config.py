"""Load a YAML or JSON run config into validated dataclasses. See
examples/annual_composite.yaml and examples/scene_mode.yaml for the shape.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

DEFAULT_RESOLUTION_M = {"landsat": 30.0, "sentinel2": 10.0}


@dataclass(frozen=True)
class AOI:
    """Named corners rather than a positional 4-number list — a
    [west,south,east,north] vs. [west,north,east,south] mix-up is an easy,
    hard-to-spot mistake; upper_left/lower_right removes the ambiguity."""

    upper_left: tuple[float, float]  # (lon, lat) — EPSG:4326
    lower_right: tuple[float, float]  # (lon, lat) — EPSG:4326

    def __post_init__(self):
        west, north = self.upper_left
        east, south = self.lower_right
        if west >= east:
            raise ValueError(
                f"aoi.upper_left longitude ({west}) must be less than "
                f"aoi.lower_right longitude ({east})"
            )
        if south >= north:
            raise ValueError(
                f"aoi.lower_right latitude ({south}) must be less than "
                f"aoi.upper_left latitude ({north})"
            )

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) — the order STAC/rasterio expect."""
        west, north = self.upper_left
        east, south = self.lower_right
        return (west, south, east, north)


@dataclass(frozen=True)
class SensorSpec:
    name: str  # "landsat" | "sentinel2"
    resolution_m: Optional[float] = None

    def resolution(self) -> float:
        if self.resolution_m is not None:
            return self.resolution_m
        if self.name not in DEFAULT_RESOLUTION_M:
            raise ValueError(f"Unknown sensor {self.name!r}, no default resolution")
        return DEFAULT_RESOLUTION_M[self.name]


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    season_start: Optional[str] = None  # "MM-DD"
    season_end: Optional[str] = None  # "MM-DD"

    def __post_init__(self):
        if (self.season_start is None) != (self.season_end is None):
            raise ValueError("season_start and season_end must be set together")


@dataclass(frozen=True)
class Filters:
    max_cloud_percent: float = 60.0
    pixel_cloud_mask: bool = True


@dataclass(frozen=True)
class OutputSpec:
    bands: tuple[str, ...] = ()
    indices: tuple[str, ...] = ()
    dir: str = "output"
    target_epsg: Optional[str] = None
    format: str = "geotiff"
    write_files: bool = True

    def __post_init__(self):
        if not self.bands and not self.indices:
            raise ValueError("output.bands and/or output.indices must be non-empty")
        if self.format != "geotiff":
            raise ValueError(f"Unsupported output.format {self.format!r} — only 'geotiff' for now")


@dataclass(frozen=True)
class Config:
    aoi: AOI
    provider: str
    sensors: list[SensorSpec]
    date_range: DateRange
    output: OutputSpec
    filters: Filters = field(default_factory=Filters)
    temporal_mode: str = "annual_composite"  # "annual_composite" | "scene"
    reduce: str = "median"  # "median" | "mean" — only used for annual_composite
    provider_options: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.temporal_mode not in ("annual_composite", "scene"):
            raise ValueError(f"Unknown temporal_mode {self.temporal_mode!r}")
        if self.reduce not in ("median", "mean"):
            raise ValueError(f"Unknown reduce {self.reduce!r}")


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, int):
        return date(value, 1, 1)
    return date.fromisoformat(str(value))


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(raw)
    elif path.suffix == ".json":
        data = json.loads(raw)
    else:
        raise ValueError(f"Unsupported config extension {path.suffix!r} — use .yaml/.yml/.json")

    aoi = AOI(
        upper_left=tuple(data["aoi"]["upper_left"]),
        lower_right=tuple(data["aoi"]["lower_right"]),
    )
    sensors = [SensorSpec(**s) for s in data["sensors"]]

    dr = data["date_range"]
    date_range = DateRange(
        start=_parse_date(dr["start"]),
        end=_parse_date(dr.get("end", dr["start"])),
        season_start=dr.get("season_start"),
        season_end=dr.get("season_end"),
    )

    filters = Filters(**data.get("filters", {}))
    out = data["output"]
    output = OutputSpec(
        bands=tuple(out.get("bands", ())),
        indices=tuple(out.get("indices", ())),
        dir=out.get("dir", "output"),
        target_epsg=out.get("target_epsg"),
        format=out.get("format", "geotiff"),
        write_files=out.get("write_files", True),
    )

    return Config(
        aoi=aoi,
        provider=data["provider"],
        sensors=sensors,
        date_range=date_range,
        filters=filters,
        temporal_mode=data.get("temporal_mode", "annual_composite"),
        reduce=data.get("reduce", "median"),
        output=output,
        provider_options=data.get("provider_options", {}),
    )

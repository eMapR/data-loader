"""Run configuration: a YAML/JSON file -> validated, frozen dataclasses.

Schema version 1 (see docs/configuration.md for every key). Validation is
strict: unknown keys are errors at every level (with a "did you mean"
suggestion), so a typo can never silently fall back to a default. Checks
that depend on the chosen provider (does it support tiles, native
encoding, a QA band?) live in engine.check_request, which `data-loader
validate` also runs -- still without any network access.
"""
from __future__ import annotations

import calendar
import difflib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1

SENSORS = ("landsat", "sentinel2")
DEFAULT_RESOLUTION_M = {"landsat": 30.0, "sentinel2": 10.0}
CANONICAL_BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")
TEMPORAL_MODES = ("scene", "annual_composite")
REDUCERS = ("median", "mean")
ENCODINGS = ("float32", "native")


class ConfigError(ValueError):
    """A config file that can't be run as written. The message says which
    key is wrong and how to fix it."""


# -- small parsing helpers --------------------------------------------------

def _mapping(value, where: str, allowed: tuple, required: tuple = ()) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    for key in value:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise ConfigError(f"{where}: unknown key {key!r}{hint}; allowed keys: {', '.join(allowed)}")
    for key in required:
        if key not in value:
            raise ConfigError(f"{where}: missing required key {key!r}")
    return value


def _choice(value, where: str, choices: tuple) -> str:
    if value not in choices:
        close = difflib.get_close_matches(str(value), choices, n=1)
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        raise ConfigError(f"{where}: {value!r} is not one of {', '.join(choices)}{hint}")
    return value


def _int(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected a whole number, got {value!r}")
    return value


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _date(value, where: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(f"{where}: expected a date as YYYY-MM-DD, got {value!r}") from None


def _str_list(value, where: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ConfigError(f"{where}: expected a list, got {value!r}")
    return tuple(str(v) for v in value)


def _month_day(value, where: str) -> str:
    """'MM-DD' (zero-padded). 02-29 is allowed and means the last day of
    February in non-leap years."""
    s = str(value)
    try:
        if len(s) != 5 or s[2] != "-":
            raise ValueError
        m, d = (int(x) for x in s.split("-"))
        date(2000, m, d)  # 2000 is a leap year, so 02-29 validates
    except ValueError:
        raise ConfigError(f"{where}: expected a month-day as MM-DD (e.g. \"06-01\"), got {value!r}") from None
    return s


def month_day_in_year(month_day: str, year: int) -> date:
    m, d = (int(x) for x in month_day.split("-"))
    return date(year, m, min(d, calendar.monthrange(year, m)[1]))


# -- config sections ---------------------------------------------------------

@dataclass(frozen=True)
class SensorSpec:
    name: str  # "landsat" | "sentinel2"
    # Pin the upstream product (data_loader.product_contract): a provider
    # that supplies a different product family for this sensor is an
    # error instead of a silent substitution. None = accept what the
    # provider supplies.
    product_family: Optional[str] = None
    # "any" | "latest" | "allow_mixed" | "pinned:<baseline>" -- see
    # product_contract.parse_version_policy. Only meaningful where a
    # provider can return several processing versions of one acquisition
    # (Sentinel-2 on Earth Search).
    processing_version_policy: str = "any"


@dataclass(frozen=True)
class AOI:
    """Either a bbox (named corners, [lon, lat] in EPSG:4326 -- no
    west/south/east/north ordering to get wrong) or a list of provider
    tile ids (e.g. USGS ARD "h003v004")."""

    upper_left: Optional[tuple[float, float]] = None
    lower_right: Optional[tuple[float, float]] = None
    tiles: tuple[str, ...] = ()

    def __post_init__(self):
        if self.tiles:
            if self.upper_left is not None or self.lower_right is not None:
                raise ConfigError("aoi: give either upper_left/lower_right or tiles, not both")
            if len(set(self.tiles)) != len(self.tiles):
                raise ConfigError(f"aoi.tiles: duplicate tile ids in {list(self.tiles)}")
            return
        if self.upper_left is None or self.lower_right is None:
            raise ConfigError("aoi: needs upper_left and lower_right ([lon, lat]), or tiles")
        west, north = self.upper_left
        east, south = self.lower_right
        if west >= east:
            raise ConfigError(f"aoi: upper_left longitude ({west}) must be less than lower_right longitude ({east})")
        if south >= north:
            raise ConfigError(f"aoi: lower_right latitude ({south}) must be less than upper_left latitude ({north})")
        if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
            raise ConfigError("aoi: corners must be [longitude, latitude] in degrees (EPSG:4326)")

    @property
    def kind(self) -> str:
        return "tiles" if self.tiles else "bbox"

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) -- the order STAC/rasterio expect."""
        if self.tiles:
            raise ValueError("a tile AOI has no single bbox; ask the provider's tile grid")
        west, north = self.upper_left
        east, south = self.lower_right
        return (west, south, east, north)


@dataclass(frozen=True)
class Season:
    start: str  # "MM-DD"
    end: str  # "MM-DD"

    @property
    def wraps(self) -> bool:
        """True for a window that crosses New Year, e.g. 11-01 to 02-28."""
        return self.start > self.end


@dataclass(frozen=True)
class TimeWindow:
    """One season (or calendar year) of the request. `season_year` is the
    year the season STARTS in; `start`/`end` are the actual dates, already
    clipped to the request and to today."""

    season_year: int
    start: date
    end: date

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True)
class TimeSpec:
    """Years (start_year/end_year) or exact dates (start_date/end_date),
    optionally narrowed to a season inside every year. A missing end means
    "through today": the request is open-ended and re-running it later
    picks up newer imagery."""

    start_year: Optional[int] = None
    end_year: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    season: Optional[Season] = None

    def __post_init__(self):
        years = self.start_year is not None or self.end_year is not None
        dates = self.start_date is not None or self.end_date is not None
        if years and dates:
            raise ConfigError("time: use start_year/end_year or start_date/end_date, not both")
        if years:
            if self.start_year is None:
                raise ConfigError("time: end_year needs start_year")
            if self.end_year is not None and self.end_year < self.start_year:
                raise ConfigError(f"time: end_year {self.end_year} is before start_year {self.start_year}")
        elif dates:
            if self.start_date is None:
                raise ConfigError("time: end_date needs start_date")
            if self.end_date is not None and self.end_date < self.start_date:
                raise ConfigError(f"time: end_date {self.end_date} is before start_date {self.start_date}")
        else:
            raise ConfigError("time: needs start_year (and optionally end_year) or start_date (and optionally end_date)")

    @property
    def open_ended(self) -> bool:
        return self.end_year is None and self.end_date is None

    def _season_bounds(self, year: int) -> tuple[date, date]:
        if self.season is None:
            return date(year, 1, 1), date(year, 12, 31)
        start = month_day_in_year(self.season.start, year)
        end = month_day_in_year(self.season.end, year + 1 if self.season.wraps else year)
        return start, end

    def windows(self, today: Optional[date] = None) -> list[TimeWindow]:
        """Every season window of the request, in order, clipped to the
        request's dates and to `today`. Windows that haven't started yet
        are dropped; the current one ends today."""
        today = today or date.today()
        if self.start_year is not None:
            lo = None
            hi = today
            years = range(self.start_year, (self.end_year if self.end_year is not None else today.year) + 1)
        else:
            lo = self.start_date
            hi = min(self.end_date, today) if self.end_date is not None else today
            years = range(self.start_date.year - 1, hi.year + 1)
        out = []
        for y in years:
            start, end = self._season_bounds(y)
            if lo is not None:
                start = max(start, lo)
            end = min(end, hi)
            if start <= end:
                out.append(TimeWindow(season_year=y, start=start, end=end))
        return out

    def span(self, today: Optional[date] = None) -> tuple[date, date]:
        ws = self.windows(today)
        if not ws:
            raise ConfigError("time: the requested period has no dates up to today")
        return ws[0].start, ws[-1].end

    def window_for(self, d: date, today: Optional[date] = None) -> Optional[TimeWindow]:
        for w in self.windows(today):
            if w.contains(d):
                return w
        return None


@dataclass(frozen=True)
class Filters:
    # 100 = no scene-level cloud filter. DataLoader keeps observations
    # unless asked not to; lower it to skip cloudy scenes at discovery.
    max_cloud_percent: float = 100.0
    # Off by default: source pixels are kept and the QA band (output.qa_band)
    # lets downstream code decide. True replaces cloud/shadow/cirrus pixels
    # with nodata using the provider's QA band (rules recorded in the manifest).
    pixel_cloud_mask: bool = False

    def __post_init__(self):
        if not 0 <= self.max_cloud_percent <= 100:
            raise ConfigError(f"filters.max_cloud_percent must be 0-100, got {self.max_cloud_percent}")


@dataclass(frozen=True)
class GridSpec:
    # "auto" = a UTM zone picked from the AOI centroid; "EPSG:xxxx" = that
    # CRS; "native" = the provider's own tile grid, no resampling (needs
    # aoi.tiles).
    crs: str = "auto"
    # Pixel size in meters; None = each sensor's native size (Landsat 30,
    # Sentinel-2 10). Ignored for crs: native.
    resolution_m: Optional[float] = None

    def resolution(self, sensor: str) -> float:
        return self.resolution_m if self.resolution_m is not None else DEFAULT_RESOLUTION_M[sensor]


@dataclass(frozen=True)
class OutputSpec:
    dir: str
    # "float32": reflectance, NaN nodata. "native": the provider's original
    # integer values (e.g. Landsat C2 SR uint16 DN), scale/offset in the
    # manifest and GeoTIFF tags, 0 nodata.
    encoding: str = "float32"
    bands: tuple[str, ...] = ()
    indices: tuple[str, ...] = ()
    # Keep the provider's raw QA band (Landsat QA_PIXEL, Sentinel-2 SCL) as
    # the last band of each bands file.
    qa_band: bool = False


@dataclass(frozen=True)
class Config:
    provider: str
    sensors: tuple[SensorSpec, ...]
    aoi: AOI
    time: TimeSpec
    temporal_mode: str
    output: OutputSpec
    reduce: str = "median"  # annual_composite only
    filters: Filters = field(default_factory=Filters)
    grid: GridSpec = field(default_factory=GridSpec)
    # Acquisitions processed at once (threads). The scene/tile is the unit
    # of work; reads within one are not split further.
    workers: int = 1
    # Give up on one acquisition after this many failed attempts, counted
    # across re-runs of the same output directory.
    max_attempts: int = 3
    provider_options: dict = field(default_factory=dict)
    version: int = SCHEMA_VERSION

    def __post_init__(self):
        _choice(self.temporal_mode, "temporal_mode", TEMPORAL_MODES)
        _choice(self.reduce, "reduce", REDUCERS)
        _choice(self.output.encoding, "output.encoding", ENCODINGS)
        if not self.sensors:
            raise ConfigError("sensors: list at least one sensor")
        names = [s.name for s in self.sensors]
        if len(set(names)) != len(names):
            raise ConfigError(f"sensors: each sensor may appear once, got {names}")
        for s in self.sensors:
            _choice(s.name, "sensors[].name", SENSORS)
        for b in self.output.bands:
            _choice(b, "output.bands[]", CANONICAL_BANDS)
        from data_loader.indices import INDEX_FUNCS
        for i in self.output.indices:
            _choice(i, "output.indices[]", tuple(INDEX_FUNCS))
        if not self.output.bands and not self.output.indices:
            raise ConfigError("output: list output.bands and/or output.indices")
        if self.output.qa_band and not self.output.bands:
            raise ConfigError("output.qa_band: the QA band is stored in the bands file, so list output.bands too")
        if self.output.encoding == "native":
            if self.output.indices:
                raise ConfigError("output.encoding: native stores source integers; indices need float32 "
                                  "(remove output.indices or use encoding: float32)")
            if self.temporal_mode != "scene":
                raise ConfigError("output.encoding: native is for scene mode; composites are float32")
        if self.temporal_mode == "annual_composite" and self.output.qa_band:
            raise ConfigError("output.qa_band: a composite has no single QA band; use temporal_mode: scene")
        if self.aoi.tiles and self.grid.crs != "native":
            raise ConfigError("grid.crs: a tile AOI is read on the provider's native tile grid; "
                              "set grid.crs: native (or omit grid)")
        if self.grid.crs == "native" and not self.aoi.tiles:
            raise ConfigError("grid.crs: native needs aoi.tiles; for a bbox use auto or an EPSG code")
        if self.grid.crs not in ("auto", "native") and not str(self.grid.crs).upper().startswith("EPSG:"):
            raise ConfigError(f"grid.crs: expected auto, native or EPSG:<code>, got {self.grid.crs!r}")
        if self.grid.resolution_m is not None and self.grid.resolution_m <= 0:
            raise ConfigError(f"grid.resolution_m must be positive, got {self.grid.resolution_m}")
        if self.workers < 1:
            raise ConfigError(f"workers must be >= 1, got {self.workers}")
        if self.max_attempts < 1:
            raise ConfigError(f"max_attempts must be >= 1, got {self.max_attempts}")
        from data_loader.product_contract import parse_version_policy
        for s in self.sensors:
            try:
                parse_version_policy(s.processing_version_policy)
            except ValueError as e:
                raise ConfigError(f"sensors[].processing_version_policy: {e}") from None

    def to_dict(self) -> dict:
        """JSON-ready form, as recorded in request.yaml and the manifest.
        provider_options values are never echoed (they could be secrets)."""
        def clean(v):
            if isinstance(v, date):
                return v.isoformat()
            if isinstance(v, (tuple, list)):
                return [clean(x) for x in v]
            if isinstance(v, dict):
                return {k: clean(x) for k, x in v.items()}
            return v
        d = clean(asdict(self))
        d["provider_options"] = sorted(self.provider_options)
        return d


# Request fields that may change between runs into the same output
# directory without making it a different dataset: run tuning, where the
# directory lives, and how far the request extends in time (extending it is
# how a dataset is updated).
_MUTABLE = {("output", "dir"), ("workers",), ("max_attempts",), ("provider_options",),
            ("time", "end_year"), ("time", "end_date")}


def request_identity(cfg: dict) -> dict:
    """The parts of a Config.to_dict() that define WHAT the dataset is."""
    out = {}
    for k, v in cfg.items():
        if (k,) in _MUTABLE:
            continue
        if isinstance(v, dict):
            v = {k2: v2 for k2, v2 in v.items() if (k, k2) not in _MUTABLE}
        out[k] = v
    return out


# -- loading -----------------------------------------------------------------

_TOP = ("version", "provider", "sensors", "aoi", "time", "temporal_mode", "reduce", "filters",
        "grid", "output", "workers", "max_attempts", "provider_options")

_PRE_V1_HINT = (
    "this config has no `version:` key. DataLoader 1.0 changed the config format: "
    "add `version: 1`, replace date_range with time (start_year/end_year or "
    "start_date/end_date, plus season: {start, end}), move output.target_epsg to grid.crs "
    "and sensors[].resolution_m to grid.resolution_m, and add temporal_mode. See "
    "docs/configuration.md (\"Migrating from pre-1.0 configs\")."
)


def config_from_dict(data: dict, *, source: str = "config") -> Config:
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    if "version" not in data:
        raise ConfigError(f"{source}: {_PRE_V1_HINT}")
    _mapping(data, source, _TOP, required=("version", "provider", "sensors", "aoi", "time",
                                            "temporal_mode", "output"))
    if data["version"] != SCHEMA_VERSION:
        raise ConfigError(f"version: this DataLoader reads config version {SCHEMA_VERSION}, got {data['version']!r}")

    sensors = []
    raw_sensors = data["sensors"]
    if not isinstance(raw_sensors, list):
        raise ConfigError("sensors: expected a list, e.g. [{name: landsat}]")
    for i, s in enumerate(raw_sensors):
        if isinstance(s, str):
            s = {"name": s}
        _mapping(s, f"sensors[{i}]", ("name", "product_family", "processing_version_policy"), ("name",))
        sensors.append(SensorSpec(**s))

    a = _mapping(data["aoi"], "aoi", ("upper_left", "lower_right", "tiles"))
    corners = {}
    for key in ("upper_left", "lower_right"):
        if key in a:
            v = a[key]
            if not isinstance(v, (list, tuple)) or len(v) != 2 or not all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
                raise ConfigError(f"aoi.{key}: expected [lon, lat] numbers, got {v!r}")
            corners[key] = (float(v[0]), float(v[1]))
    aoi = AOI(tiles=_str_list(a["tiles"], "aoi.tiles") if "tiles" in a else (), **corners)

    t = _mapping(data["time"], "time", ("start_year", "end_year", "start_date", "end_date", "season"))
    season = None
    if t.get("season") is not None:
        s = _mapping(t["season"], "time.season", ("start", "end"), ("start", "end"))
        season = Season(_month_day(s["start"], "time.season.start"), _month_day(s["end"], "time.season.end"))
    time = TimeSpec(
        start_year=_int(t["start_year"], "time.start_year") if t.get("start_year") is not None else None,
        end_year=_int(t["end_year"], "time.end_year") if t.get("end_year") is not None else None,
        start_date=_date(t["start_date"], "time.start_date") if t.get("start_date") is not None else None,
        end_date=_date(t["end_date"], "time.end_date") if t.get("end_date") is not None else None,
        season=season,
    )

    temporal_mode = _choice(data["temporal_mode"], "temporal_mode", TEMPORAL_MODES)
    if "reduce" in data and temporal_mode != "annual_composite":
        raise ConfigError("reduce: only applies to temporal_mode: annual_composite")

    f = _mapping(data.get("filters") or {}, "filters", ("max_cloud_percent", "pixel_cloud_mask"))
    max_cloud = f.get("max_cloud_percent", 100.0)
    if isinstance(max_cloud, bool) or not isinstance(max_cloud, (int, float)):
        raise ConfigError(f"filters.max_cloud_percent: expected a number 0-100, got {max_cloud!r}")
    filters = Filters(
        max_cloud_percent=float(max_cloud),
        pixel_cloud_mask=_bool(f.get("pixel_cloud_mask", False), "filters.pixel_cloud_mask"),
    )

    g = _mapping(data.get("grid") or {}, "grid", ("crs", "resolution_m"))
    grid = GridSpec(
        crs=str(g.get("crs", "native" if aoi.tiles else "auto")),
        resolution_m=float(g["resolution_m"]) if g.get("resolution_m") is not None else None,
    )

    o = _mapping(data["output"], "output", ("dir", "encoding", "bands", "indices", "qa_band"), ("dir",))
    output = OutputSpec(
        dir=str(o["dir"]),
        encoding=_choice(o.get("encoding", "float32"), "output.encoding", ENCODINGS),
        bands=_str_list(o["bands"], "output.bands") if o.get("bands") is not None else (),
        indices=_str_list(o["indices"], "output.indices") if o.get("indices") is not None else (),
        qa_band=_bool(o.get("qa_band", False), "output.qa_band"),
    )

    provider_options = data.get("provider_options") or {}
    if not isinstance(provider_options, dict):
        raise ConfigError("provider_options: expected a mapping")

    return Config(
        provider=str(data["provider"]),
        sensors=tuple(sensors),
        aoi=aoi,
        time=time,
        temporal_mode=temporal_mode,
        reduce=_choice(data.get("reduce", "median"), "reduce", REDUCERS),
        filters=filters,
        grid=grid,
        output=output,
        workers=_int(data.get("workers", 1), "workers"),
        max_attempts=_int(data.get("max_attempts", 3), "max_attempts"),
        provider_options=dict(provider_options),
    )


def load_config(path, *, output_dir: Optional[str] = None) -> Config:
    """Read a .yaml/.yml/.json config. `output_dir` overrides output.dir
    (the CLI's --output-dir), so one config can be run into different
    locations on different machines."""
    path = Path(path)
    raw = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(raw)
    elif path.suffix == ".json":
        data = json.loads(raw)
    else:
        raise ConfigError(f"{path}: unsupported extension {path.suffix!r}; use .yaml, .yml or .json")
    if output_dir is not None and isinstance(data, dict) and isinstance(data.get("output"), dict):
        data["output"]["dir"] = str(output_dir)
    return config_from_dict(data, source=str(path))

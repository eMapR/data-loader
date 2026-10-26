"""The library on disk: a root, its products, and one DataLoader dataset per tile.

    <root>/LIBRARY.yaml                                     what this library is, who maintains it
    <root>/products/<product>/<version>/PRODUCT.yaml        the product spec (one per product version)
    <root>/products/<product>/<version>/<grid>/<tile>/      an ordinary DataLoader dataset
    <root>/catalog/current -> <build>/                      generated catalog (see catalog.py)
    <root>/admin/                                           maintainer logs: imports, verification

Imagery is organised by what it is (product, version, grid, tile), never
by the project that asked for it. A tile directory is exactly what
`data-loader run` writes for a config whose aoi is that one tile, so the
acquisition engine reads, resumes and updates it with no library code.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import yaml

LAYOUT_VERSION = 1
LIBRARY_FILE = "LIBRARY.yaml"
PRODUCT_FILE = "PRODUCT.yaml"
PRODUCTS_DIR = "products"
CATALOG_DIR = "catalog"
ADMIN_DIR = "admin"
LINEAGE_FILE = "LINEAGE.json"

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*")      # product, version, grid system
_TILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")  # tile ids: h003v004, AK_h016v005, 10TEK

# PRODUCT.yaml keys that describe a product without changing what is acquired
_DESCRIPTIVE = {"title", "description", "notes", "published_tiles"}
_LIBRARY_KEYS = {"name", "description", "layout_version", "maintainers", "contact", "storage"}
_PRODUCT_KEYS = {"product", "version", "title", "description", "grid_systems", "acquisition", "notes",
                 "published_tiles"}


class LibraryError(RuntimeError):
    pass


def _strict(d: dict, allowed: set, where: str) -> None:
    if not isinstance(d, dict):
        raise LibraryError(f"{where}: expected a mapping")
    extra = sorted(set(d) - allowed)
    if extra:
        raise LibraryError(f"{where}: unknown key(s) {', '.join(extra)} (allowed: {', '.join(sorted(allowed))})")


def _name(value, what: str, where: str, pattern=_NAME_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LibraryError(f"{where}: {what} {value!r} must match {pattern.pattern}")
    return value


def atomic_write(path: Path, data) -> None:
    """Temp file + fsync + rename, so readers see the old or the new file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def software() -> dict:
    """Version + git commit of the code doing the work (like DataLoader's)."""
    from data_loader._version import __version__

    here = Path(__file__).resolve().parent

    def git(*a):
        try:
            return subprocess.run(["git", "-C", str(here), *a], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    commit = git("rev-parse", "HEAD") or None
    dirty = bool(git("status", "--porcelain", "--", ".")) if commit else None
    return {"dataloaderVersion": __version__, "gitCommit": commit, "gitDirty": dirty}


# -- products ----------------------------------------------------------------

def _resolved_request(request: dict, products: Optional[dict] = None) -> dict:
    """A recorded request with each sensor's product_family filled from the
    manifest's `products` where the request left it unset."""
    req = copy.deepcopy(request)
    for sd in req.get("sensors", []):
        if isinstance(sd, dict) and sd.get("product_family") is None and products:
            sd["product_family"] = products.get(sd.get("name"), {}).get("productFamily")
    return req


def spec_identity(request: dict) -> dict:
    """What makes two tile datasets the same product: DataLoader's request
    identity (time-range end, workers, retries, output dir excluded) minus
    the tile list itself."""
    from data_loader.config import request_identity

    ident = request_identity(copy.deepcopy(request))
    aoi = dict(ident.get("aoi") or {})
    aoi.pop("tiles", None)
    ident["aoi"] = aoi
    return ident


def spec_hash(identity: dict) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Product:
    """One product version: its spec (PRODUCT.yaml) and its tile datasets."""

    root: Path                 # <library>/products/<product>/<version>
    name: str
    version: str
    title: str
    description: str
    grid_systems: list
    acquisition: dict          # DataLoader request template: everything except aoi and output.dir
    notes: Optional[str] = None
    published_tiles_file: Optional[str] = None   # tiles the source publishes, per grid system (optional)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        return f"{self.name}/{self.version}"

    @classmethod
    def from_dict(cls, d: dict, root: Path, where: str) -> "Product":
        _strict(d, _PRODUCT_KEYS, where)
        for k in ("product", "version", "title", "grid_systems", "acquisition"):
            if k not in d:
                raise LibraryError(f"{where}: missing required key {k!r}")
        grids = d["grid_systems"]
        if not isinstance(grids, list) or not grids:
            raise LibraryError(f"{where}: grid_systems must be a non-empty list")
        acq = d["acquisition"]
        if not isinstance(acq, dict):
            raise LibraryError(f"{where}: acquisition must be a DataLoader request mapping")
        for k in ("aoi",):
            if k in acq:
                raise LibraryError(f"{where}: acquisition must not set {k!r}; the library sets it per tile")
        if "dir" in (acq.get("output") or {}):
            raise LibraryError(f"{where}: acquisition.output must not set 'dir'; the library sets it per tile")
        for s in acq.get("sensors") or []:
            if not isinstance(s, dict) or not s.get("product_family"):
                raise LibraryError(f"{where}: every acquisition sensor must name its product_family, "
                                   "so the product can never silently change")
        return cls(root=root, name=_name(d["product"], "product", where), version=_name(d["version"], "version", where),
                   title=str(d["title"]), description=str(d.get("description") or ""),
                   grid_systems=[_name(g, "grid system", where) for g in grids], acquisition=acq,
                   notes=d.get("notes"), published_tiles_file=d.get("published_tiles"), raw=d)

    def published_tiles(self) -> Optional[dict]:
        """{grid_system: set of tile ids} the source publishes, or None if
        the product has no list (then every grid position counts)."""
        if not self.published_tiles_file:
            return None
        p = self.root / self.published_tiles_file
        if not p.exists():
            raise LibraryError(f"{self.key}: published_tiles file {p} is missing")
        return {g: set(ts) for g, ts in json.loads(p.read_text())["tiles"].items()}

    def locate(self, tile: str) -> tuple[str, str]:
        """(grid_system, canonical tile id): one spelling per tile, so the
        same tile can never get two directories (CU_h003v004 -> h003v004)."""
        from imagery_library.grids import grid_system_of

        return grid_system_of(tile, self.grid_systems)

    def tile_path(self, grid_system: str, tile: str) -> Path:
        if grid_system not in self.grid_systems:
            raise LibraryError(f"{self.key}: grid system {grid_system!r} is not one of {self.grid_systems}")
        return self.root / grid_system / _name(tile, "tile", self.key, _TILE_RE)

    def tiles(self) -> Iterator[tuple[str, str, Path]]:
        """(grid_system, tile, path) of every tile dataset present."""
        for g in self.grid_systems:
            gdir = self.root / g
            if not gdir.is_dir():
                continue
            for p in sorted(gdir.iterdir()):
                if p.is_dir() and not p.name.startswith(".") and (p / "manifest.json").exists():
                    yield g, p.name, p

    def config_for_tile(self, tile: str, grid_system: str) -> dict:
        """The DataLoader config that creates or updates this tile's dataset."""
        d = copy.deepcopy(self.acquisition)
        d.setdefault("version", 1)
        d["aoi"] = {"tiles": [tile]}
        d.setdefault("output", {})["dir"] = str(self.tile_path(grid_system, tile))
        return d

    def spec_identity(self, tile: str, grid_system: str) -> dict:
        from data_loader.config import config_from_dict

        cfg = config_from_dict(self.config_for_tile(tile, grid_system), source=f"{self.key} PRODUCT.yaml")
        return spec_identity(cfg.to_dict())

    def spec_hash(self, tile: str, grid_system: str) -> str:
        return spec_hash(self.spec_identity(tile, grid_system))


# -- the library -------------------------------------------------------------

class Library:
    def __init__(self, root):
        self.root = Path(root)
        lf = self.root / LIBRARY_FILE
        if not lf.exists():
            raise LibraryError(f"{self.root}: no {LIBRARY_FILE} -- not an imagery library "
                               "(create one with `imagery-library init`)")
        info = yaml.safe_load(lf.read_text()) or {}
        _strict(info, _LIBRARY_KEYS, str(lf))
        if int(info.get("layout_version", 0)) != LAYOUT_VERSION:
            raise LibraryError(f"{lf}: layout_version {info.get('layout_version')} is not {LAYOUT_VERSION}")
        self.info = info

    @property
    def name(self) -> str:
        return str(self.info.get("name", self.root.name))

    @property
    def catalog_dir(self) -> Path:
        return self.root / CATALOG_DIR

    @property
    def admin_dir(self) -> Path:
        return self.root / ADMIN_DIR

    def products(self) -> list[Product]:
        out = []
        pdir = self.root / PRODUCTS_DIR
        for spec in sorted(pdir.glob(f"*/*/{PRODUCT_FILE}")) if pdir.is_dir() else []:
            d = yaml.safe_load(spec.read_text()) or {}
            p = Product.from_dict(d, spec.parent, str(spec))
            if (p.name, p.version) != (spec.parent.parent.name, spec.parent.name):
                raise LibraryError(f"{spec}: says {p.key} but sits in {spec.parent.parent.name}/{spec.parent.name}")
            out.append(p)
        return out

    def product(self, name: Optional[str] = None, version: Optional[str] = None) -> Product:
        found = [p for p in self.products() if (name is None or p.name == name)
                 and (version is None or p.version == version)]
        if len(found) != 1:
            have = ", ".join(p.key for p in self.products()) or "none"
            want = f"{name or '*'}/{version or '*'}"
            raise LibraryError(f"{want}: matches {len(found)} products in {self.root} (have: {have})")
        return found[0]

    def tiles(self, product: Optional[str] = None, version: Optional[str] = None):
        """(Product, grid_system, tile, path) for every tile dataset."""
        for p in self.products():
            if (product is None or p.name == product) and (version is None or p.version == version):
                for g, t, path in p.tiles():
                    yield p, g, t, path

    def query(self, aoi, **kw):
        from imagery_library.query import query

        return query(self, aoi, **kw)


def open_library(root) -> Library:
    return Library(root)


def init_library(root, *, name: str, description: str, maintainers: list, contact: Optional[str] = None) -> Library:
    root = Path(root)
    if (root / LIBRARY_FILE).exists():
        raise LibraryError(f"{root}: already a library")
    root.mkdir(parents=True, exist_ok=True)
    for d in (PRODUCTS_DIR, CATALOG_DIR, ADMIN_DIR):
        (root / d).mkdir(exist_ok=True)
    info = {"name": name, "description": description, "layout_version": LAYOUT_VERSION,
            "maintainers": list(maintainers)}
    if contact:
        info["contact"] = contact
    atomic_write(root / LIBRARY_FILE, yaml.safe_dump(info, sort_keys=False))
    return Library(root)


def add_product(lib: Library, spec_path) -> Product:
    """Install a PRODUCT.yaml (validated, including as a DataLoader config)."""
    from data_loader.config import ConfigError, config_from_dict

    d = yaml.safe_load(Path(spec_path).read_text()) or {}
    root = lib.root / PRODUCTS_DIR / str(d.get("product")) / str(d.get("version"))
    p = Product.from_dict(d, root, str(spec_path))
    extra = Path(spec_path).parent / p.published_tiles_file if p.published_tiles_file else None
    if extra is not None and not extra.exists():
        raise LibraryError(f"{spec_path}: published_tiles file {extra} not found")
    try:  # the template must be a valid DataLoader request once a tile is filled in
        acq = copy.deepcopy(p.acquisition)
        acq.setdefault("version", 1)
        acq["aoi"] = {"tiles": ["h000v000"]} if p.acquisition.get("provider") == "usgs_ard" else {"tiles": ["x"]}
        acq.setdefault("output", {})["dir"] = "/nonexistent"
        config_from_dict(acq, source=str(spec_path))
    except ConfigError as e:
        raise LibraryError(f"{spec_path}: acquisition is not a valid DataLoader request: {e}") from None
    if (root / PRODUCT_FILE).exists():
        old = yaml.safe_load((root / PRODUCT_FILE).read_text()) or {}
        if {k: v for k, v in old.items() if k not in _DESCRIPTIVE} != {k: v for k, v in d.items() if k not in _DESCRIPTIVE}:
            raise LibraryError(f"{p.key} already exists with a different spec; a changed product is a new version "
                               f"(only {', '.join(sorted(_DESCRIPTIVE))} may be updated in place)")
    root.mkdir(parents=True, exist_ok=True)
    if extra is not None:
        atomic_write(root / p.published_tiles_file, extra.read_text())
    atomic_write(root / PRODUCT_FILE, Path(spec_path).read_text())
    return p

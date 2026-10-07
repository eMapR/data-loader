"""A DataLoader dataset on disk: the output contract downstream programs read.

    <dir>/manifest.json        dataset-level description (schema "dataloader-manifest")
    <dir>/items.jsonl          one row per acquisition (or composite) per grid, with status
    <dir>/request.yaml         the resolved request that produced the dataset
    <dir>/metadata/<provider>/<item>.json   verbatim provider metadata records
    <dir>/<sensor>/<grid>/...  the GeoTIFFs
    <dir>/.state/              run bookkeeping (journal, lock) -- not part of the contract

`DatasetWriter` is the engine's side: it owns the directory during a run
(an OS file lock keeps two runs out of one directory), journals every
finished unit as it completes, and rewrites items.jsonl/manifest.json
atomically. `Dataset` (via `open_dataset`) is the reader for downstream
code and for `data-loader status`/`verify`.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from data_loader.config import request_identity

MANIFEST_SCHEMA = "dataloader-manifest"
MANIFEST_VERSION = "1.0"
STATE_DIR = ".state"
STATUSES = ("acquired", "failed", "pending", "filtered")


class DatasetError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # a line cut off by a kill mid-write; that unit is simply redone
    return rows


def _diff_keys(a, b, prefix="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff_keys(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else k)
        return out
    return [] if a == b else [prefix]


class DatasetWriter:
    def __init__(self, root, request: dict):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = self.root / STATE_DIR
        self.state.mkdir(exist_ok=True)
        self._lock_file = open(self.state / "lock", "w")
        try:
            import fcntl

            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise DatasetError(f"{self.root}: another DataLoader run is using this directory") from None
        except ImportError:  # pragma: no cover - non-POSIX
            pass
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

        self.request = request
        manifest_path = self.root / "manifest.json"
        self.created = _now()
        if manifest_path.exists():
            old = json.loads(manifest_path.read_text())
            if old.get("schema") != MANIFEST_SCHEMA:
                self.close()
                raise DatasetError(f"{self.root}: manifest.json is not a DataLoader {MANIFEST_VERSION} manifest")
            diff = _diff_keys(request_identity(old.get("request", {})), request_identity(request))
            if diff:
                self.close()
                raise DatasetError(
                    f"{self.root} already holds a different dataset (request differs in: {', '.join(diff)}). "
                    "Use a new output.dir; only the end of the time range, workers, max_attempts and "
                    "provider_options may change between runs into the same directory.")
            self.created = old.get("dataset", {}).get("created", self.created)
        elif any(p for p in self.root.iterdir() if p.name != STATE_DIR):
            self.close()
            raise DatasetError(f"{self.root} is not empty and has no manifest.json; "
                               "choose an empty or new output.dir")

        self._rows: dict[str, dict] = {r["key"]: r for r in _read_jsonl(self.root / "items.jsonl")}
        for r in _read_jsonl(self.state / "journal.jsonl"):
            self._rows[r["key"]] = r
        self._journal = open(self.state / "journal.jsonl", "a")
        self._mutex = threading.Lock()
        self._since_catalog = 0

    # -- per-unit state ----------------------------------------------------

    def row(self, key: str) -> Optional[dict]:
        return self._rows.get(key)

    def attempts(self, key: str) -> int:
        r = self._rows.get(key)
        return int(r.get("attempts", 0)) if r else 0

    def is_acquired(self, key: str) -> bool:
        """Recorded as acquired AND every file still on disk at its recorded
        size (a deleted or truncated file means redo it)."""
        r = self._rows.get(key)
        if not r or r.get("status") != "acquired":
            return False
        for f in r.get("files", []):
            p = self.root / f["path"]
            if not p.exists() or p.stat().st_size != f.get("bytes"):
                return False
        return True

    def record(self, row: dict) -> None:
        """A unit finished (acquired or failed): remember it durably now."""
        row = dict(row, updated=_now())
        with self._mutex:
            self._rows[row["key"]] = row
            self._journal.write(json.dumps(row, default=str) + "\n")
            self._journal.flush()
            self._since_catalog += 1

    def note(self, row: dict) -> None:
        """Discovery-time state (pending/filtered) -- unless the unit already
        has a real outcome. Not journaled; it is re-derived every run."""
        with self._mutex:
            old = self._rows.get(row["key"])
            if old is None or old.get("status") in ("pending", "filtered"):
                self._rows[row["key"]] = row

    @property
    def since_catalog(self) -> int:
        return self._since_catalog

    # -- catalog -------------------------------------------------------------

    def rows(self) -> list[dict]:
        with self._mutex:
            return sorted(self._rows.values(), key=lambda r: (r.get("sensor", ""), r.get("grid", ""),
                                                              r.get("date") or r.get("windowStart") or "",
                                                              r["key"]))

    def counts(self) -> dict:
        c = {s: 0 for s in STATUSES}
        for r in self.rows():
            c[r.get("status", "pending")] = c.get(r.get("status", "pending"), 0) + 1
        return c

    def write_catalog(self, manifest_core: dict, *, complete: bool) -> Path:
        """Rewrite items.jsonl + manifest.json (+ request.yaml) atomically,
        then empty the journal (its rows are now in items.jsonl)."""
        import yaml

        rows = self.rows()
        with self._mutex:
            _atomic_write(self.root / "items.jsonl", "".join(json.dumps(r, default=str) + "\n" for r in rows))
            self._journal.close()
            self._journal = open(self.state / "journal.jsonl", "w")
            self._since_catalog = 0
        counts = {s: 0 for s in STATUSES}
        for r in rows:
            counts[r.get("status", "pending")] += 1
        acquired = [r for r in rows if r.get("status") == "acquired"]
        dates = sorted(r.get("date") or r.get("windowStart") for r in acquired if r.get("date") or r.get("windowStart"))
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "schemaVersion": MANIFEST_VERSION,
            "dataset": {**manifest_core.get("dataset", {}), "created": self.created, "updated": _now(),
                        "complete": complete},
            **{k: v for k, v in manifest_core.items() if k not in ("dataset", "coverage")},
            "coverage": {**manifest_core.get("coverage", {}), "counts": counts,
                         "firstAcquired": dates[0] if dates else None,
                         "lastAcquired": dates[-1] if dates else None,
                         "bytes": sum(f.get("bytes", 0) for r in acquired for f in r.get("files", []))},
            "items": "items.jsonl",
        }
        _atomic_write(self.root / "manifest.json", json.dumps(manifest, indent=2, default=str) + "\n")
        _atomic_write(self.root / "request.yaml", yaml.safe_dump(self.request, sort_keys=False))
        return self.root / "manifest.json"

    def close(self) -> None:
        j = getattr(self, "_journal", None)
        if j is not None and not j.closed:
            j.close()
        if not self._lock_file.closed:
            self._lock_file.close()


# -- reading ----------------------------------------------------------------

class Dataset:
    """Read side of the contract:

        ds = open_dataset("/path/to/dataset")
        ds.manifest["bandSets"]["bands"]          # band order, dtype, scale/offset, nodata
        for path, item, f in ds.files(grid="h003v004", start="2000-01-01"):
            ...                                    # path is absolute; item is the items.jsonl row
    """

    def __init__(self, root):
        self.root = Path(root)
        mp = self.root / "manifest.json"
        if not mp.exists():
            raise DatasetError(f"{self.root}: no manifest.json -- not a DataLoader dataset (or no run has finished yet)")
        self.manifest = json.loads(mp.read_text())
        if self.manifest.get("schema") != MANIFEST_SCHEMA:
            raise DatasetError(f"{mp}: not a {MANIFEST_SCHEMA} manifest")
        major = str(self.manifest.get("schemaVersion", "0")).split(".")[0]
        if major != MANIFEST_VERSION.split(".")[0]:
            raise DatasetError(f"{mp}: manifest schema {self.manifest.get('schemaVersion')} is not readable by "
                               f"this DataLoader (reads {MANIFEST_VERSION})")

    def _rows(self) -> dict[str, dict]:
        rows = {r["key"]: r for r in _read_jsonl(self.root / self.manifest.get("items", "items.jsonl"))}
        for r in _read_jsonl(self.root / STATE_DIR / "journal.jsonl"):  # a run in progress
            rows[r["key"]] = r
        return rows

    def items(self, status: Optional[str] = "acquired", *, sensor: Optional[str] = None,
              grid: Optional[str] = None, start=None, end=None, kind: Optional[str] = None) -> Iterator[dict]:
        """items.jsonl rows (newest state, including a run in progress),
        filtered. status=None returns every status. start/end are dates or
        YYYY-MM-DD strings, inclusive, matched against the acquisition date
        (or a composite's window)."""
        start = start.isoformat() if isinstance(start, date) else start
        end = end.isoformat() if isinstance(end, date) else end
        for r in sorted(self._rows().values(), key=lambda r: (r.get("date") or r.get("windowStart") or "", r["key"])):
            if status is not None and r.get("status") != status:
                continue
            if sensor is not None and r.get("sensor") != sensor:
                continue
            if grid is not None and r.get("grid") != grid:
                continue
            if kind is not None and r.get("type") != kind:
                continue
            d0 = r.get("date") or r.get("windowStart")
            d1 = r.get("date") or r.get("windowEnd")
            if start is not None and (d1 or "") < start:
                continue
            if end is not None and (d0 or "") > end:
                continue
            yield r

    def files(self, band_set: Optional[str] = "bands", **filters) -> Iterator[tuple[Path, dict, dict]]:
        """(absolute path, item row, file entry) for acquired files."""
        for r in self.items(status="acquired", **filters):
            for f in r.get("files", []):
                if band_set is None or f.get("bandSet") == band_set:
                    yield self.root / f["path"], r, f

    def status(self) -> dict:
        by_status: dict[str, int] = {s: 0 for s in STATUSES}
        by_grid: dict[str, dict[str, int]] = {}
        by_year: dict[str, dict[str, int]] = {}
        failed_examples = []
        for r in self._rows().values():
            s = r.get("status", "pending")
            by_status[s] = by_status.get(s, 0) + 1
            g = by_grid.setdefault(f"{r.get('sensor')}/{r.get('grid')}", {})
            g[s] = g.get(s, 0) + 1
            y = str(r.get("seasonYear", "?"))
            by_year.setdefault(y, {})
            by_year[y][s] = by_year[y].get(s, 0) + 1
            if s == "failed" and len(failed_examples) < 10:
                failed_examples.append({"key": r["key"], "attempts": r.get("attempts"), "error": r.get("error")})
        return {"byStatus": by_status, "byGrid": dict(sorted(by_grid.items())),
                "bySeasonYear": dict(sorted(by_year.items())), "failedExamples": failed_examples}

    def verify(self, *, checksums: bool = True) -> list[str]:
        """Problems found: missing files, size or sha256 mismatches. Empty
        list = every acquired file is present and intact."""
        from data_loader.geotiff import sha256

        problems = []
        for path, r, f in self.files(band_set=None):
            if not path.exists():
                problems.append(f"missing: {f['path']}")
            elif path.stat().st_size != f.get("bytes"):
                problems.append(f"size mismatch: {f['path']} ({path.stat().st_size} != {f.get('bytes')})")
            elif checksums and f.get("sha256") and sha256(path) != f["sha256"]:
                problems.append(f"checksum mismatch: {f['path']}")
        return problems


def open_dataset(path) -> Dataset:
    return Dataset(path)

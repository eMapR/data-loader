"""Upgrade a dataset's manifest.json + items.jsonl to the current schema
(`data-loader upgrade DIR`). Metadata only: no GeoTIFF is opened for
writing, nothing is downloaded, and every change is derivable from what
the dataset already records.

1.0 -> 1.1:
- files[].bandScaleOffset for every band of every `bands` file. In 1.0 it
  was written only for bands whose scale/offset differed from the band
  set's; absent meant "the band set's value", so filling it in is exact.
- QA band `nodata` in bandSets set to the file's nodata (a GeoTIFF has one
  nodata value per file; 1.0 said null).
- `software` on rows that lack it, only when the caller says which
  software produced which period (`runs`): a row is attributed to the run
  that started last before the row was written (its `updated` time), and
  marked as attributed by upgrade. Without `runs`, rows are left without
  `software`; dataset.runs then records that the history is unknown.
- Error messages redacted twice ("?<redacted><redacted>") are cleaned.

The dataset lock is taken for the whole upgrade (except with dry_run), so
it cannot run while a `data-loader run` is writing the same directory.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from data_loader.dataset import MANIFEST_VERSION, DatasetError, DatasetWriter, _major, _now, open_dataset

ATTRIBUTED_BY = "data-loader upgrade, from run start times"


@dataclass
class UpgradeReport:
    from_version: str
    to_version: str = MANIFEST_VERSION
    rows: int = 0
    files_scale_offset_added: int = 0
    rows_software_attributed: dict = field(default_factory=dict)  # gitCommit -> rows
    rows_without_software: int = 0
    errors_cleaned: int = 0
    qa_nodata_set: list = field(default_factory=list)  # (sensor, value)
    checked_files: int = 0
    check_problems: list = field(default_factory=list)
    written: bool = False


def parse_run(spec: str) -> dict:
    """"2026-10-08T03:13:22Z=5fd1970..." -> {"started", "gitCommit"}."""
    started, sep, commit = spec.partition("=")
    if not sep or not commit:
        raise ValueError(f"--run {spec!r}: expected START=COMMIT, e.g. 2026-10-08T03:13:22Z=5fd1970")
    _parse_time(started)
    return {"started": started, "gitCommit": commit}


def _parse_time(s: str) -> datetime:
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not an ISO 8601 time: {s!r}") from None
    if t.tzinfo is None:
        raise ValueError(f"time {s!r} needs a UTC offset (e.g. ...Z or ...+00:00)")
    return t


def _band_defaults(manifest: dict, sensor: str) -> dict:
    """band name -> [scale, offset] from the manifest's band set."""
    bands = manifest.get("bandSets", {}).get(sensor, {}).get("bands", [])
    return {b["name"]: [b.get("scale", 1.0), b.get("offset", 0.0)] for b in bands}


def _upgrade_manifest(manifest: dict, report: UpgradeReport) -> None:
    for sensor, bs in manifest.get("bandSets", {}).items():
        bands = bs.get("bands", [])
        sr = [b for b in bands if b.get("role") != "qa"]
        for b in bands:
            if b.get("role") == "qa" and b.get("nodata") is None:
                b["nodata"] = sr[0].get("nodata") if sr else ("NaN" if b.get("dataType") == "float32" else 0)
                b["note"] = (b.get("note", "") + "; the file's nodata value also applies to this band. Use the QA "
                             "meaning above, not the nodata mask, to find source fill")
                report.qa_nodata_set.append((sensor, b["nodata"]))
            if "scaleOffsetVaries" in b:
                b["scaleOffsetVaries"] = ("per file: use files[].bandScaleOffset, which is authoritative; "
                                          "the offset depends on the processing baseline and catalog")


def _upgrade_row(row: dict, manifest: dict, runs: list, report: UpgradeReport) -> None:
    report.rows += 1
    defaults = _band_defaults(manifest, row.get("sensor", ""))
    float32 = manifest.get("processing", {}).get("encoding") != "native" or row.get("type") == "composite"
    for f in row.get("files", []):
        if f.get("bandSet") != "bands":
            continue
        have = dict(f.get("bandScaleOffset") or {})
        full = {n: ([1.0, 0.0] if float32 else list(so)) for n, so in defaults.items()}
        full.update(have)  # 1.0 recorded only the bands that differed; those win
        if full != have:
            f["bandScaleOffset"] = full
            report.files_scale_offset_added += 1
    err = row.get("error")
    if isinstance(err, str) and "<redacted><redacted>" in err:
        while "<redacted><redacted>" in err:
            err = err.replace("<redacted><redacted>", "<redacted>")
        row["error"] = err
        report.errors_cleaned += 1
    if "software" in row or row.get("status") not in ("acquired", "failed"):
        return
    updated = row.get("updated")
    run = None
    if runs and updated:
        t = _parse_time(updated)
        for r in runs:
            if _parse_time(r["started"]) <= t:
                run = r
    if run is None:
        report.rows_without_software += 1
        return
    row["software"] = {"dataloaderVersion": run["dataloaderVersion"], "gitCommit": run["gitCommit"],
                       "gitDirty": None, "attributedBy": ATTRIBUTED_BY}
    report.rows_software_attributed[run["gitCommit"]] = report.rows_software_attributed.get(run["gitCommit"], 0) + 1


def _check_files(root: Path, rows: list, manifest: dict, n: int, report: UpgradeReport) -> None:
    """Open a sample of acquired files and compare GDAL band scale/offset
    and nodata with what the upgraded metadata says."""
    import rasterio

    files = [(r, f) for r in rows if r.get("status") == "acquired"
             for f in r.get("files", []) if f.get("bandSet") == "bands"]
    for r, f in random.Random(0).sample(files, min(n, len(files))):
        report.checked_files += 1
        with rasterio.open(root / f["path"]) as src:
            names = list(src.descriptions)
            for i, name in enumerate(names):
                want = f.get("bandScaleOffset", {}).get(name)
                if want is None or abs(src.scales[i] - want[0]) > 1e-12 or abs(src.offsets[i] - want[1]) > 1e-12:
                    report.check_problems.append(f"{f['path']} band {name}: file scale/offset "
                                                 f"{src.scales[i]}/{src.offsets[i]} vs metadata {want}")
            bands = manifest["bandSets"][r["sensor"]]["bands"]
            for b in bands:
                nd = b.get("nodata")
                if nd == "NaN":
                    ok = src.nodata is not None and src.nodata != src.nodata
                else:
                    ok = src.nodata == nd
                if not ok:
                    report.check_problems.append(f"{f['path']} band {b['name']}: file nodata {src.nodata} vs "
                                                 f"manifest {nd}")


def upgrade_dataset(root, *, runs: Optional[list] = None, dry_run: bool = False,
                    check_files: int = 0) -> UpgradeReport:
    """Upgrade `root` in place (see module docstring). `runs`: dicts from
    parse_run(), the dataset's run history, used to attribute `software`
    to rows written before 1.1."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema") != "dataloader-manifest":
        raise DatasetError(f"{root}: not a DataLoader dataset")
    version = str(manifest.get("schemaVersion"))
    if _major(version) != _major(MANIFEST_VERSION):
        raise DatasetError(f"{root}: schema {version} cannot be upgraded by this DataLoader ({MANIFEST_VERSION})")
    report = UpgradeReport(from_version=version)
    old_version = manifest.get("dataset", {}).get("dataloaderVersion")
    runs = sorted(({**r, "dataloaderVersion": r.get("dataloaderVersion") or old_version} for r in runs or []),
                  key=lambda r: _parse_time(r["started"]))

    writer = None if dry_run else DatasetWriter(root, manifest.get("request", {}))  # takes the lock
    try:
        rows = list(writer._rows.values()) if writer else list(open_dataset(root)._rows().values())
        _upgrade_manifest(manifest, report)
        for row in rows:
            _upgrade_row(row, manifest, runs, report)
        if check_files:
            _check_files(root, rows, manifest, check_files, report)
        if writer is None:
            return report
        changed = (version != MANIFEST_VERSION or report.files_scale_offset_added or report.qa_nodata_set
                   or report.rows_software_attributed or report.errors_cleaned)
        if not changed:
            return report  # already current: nothing to write
        if report.check_problems:
            raise DatasetError(f"{root}: not upgraded, sampled files disagree with the metadata:\n  "
                               + "\n  ".join(report.check_problems[:20]))
        ds = manifest.get("dataset", {})
        if not writer.runs:
            writer.runs = [{"started": r["started"], "finished": None, "dataloaderVersion": r["dataloaderVersion"],
                            "gitCommit": r["gitCommit"], "gitDirty": None, "reconstructedBy": ATTRIBUTED_BY}
                           for r in runs] or [{"started": ds.get("created"), "finished": None,
                                               "dataloaderVersion": old_version, "gitCommit": None,
                                               "note": "run history before schema 1.1 not recorded"}]
        from data_loader.engine import software_info

        core = {k: v for k, v in manifest.items() if k not in ("schema", "schemaVersion", "items")}
        core["dataset"] = {k: v for k, v in ds.items() if k not in ("created", "updated", "complete", "runs")}
        core["dataset"]["upgrades"] = list(ds.get("upgrades") or []) + [
            {"at": _now(), "from": version, "to": MANIFEST_VERSION, **software_info()}]
        writer.write_catalog(core, complete=bool(ds.get("complete")))
        report.written = True
        return report
    finally:
        if writer is not None:
            writer.close()

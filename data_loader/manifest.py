"""manifest.json read/write — the provider-agnostic contract that lets any
downstream program discover what's in an output directory without
guessing filenames or band order.
"""
from __future__ import annotations

import json
from pathlib import Path


def write_manifest(path: Path, entries: list[dict], **top_level) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "nodataConvention": "float32 NaN (no numeric sentinel)",
        "files": entries,
        **top_level,
    }
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, default=str)

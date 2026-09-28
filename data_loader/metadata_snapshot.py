"""Writes one JSON snapshot of a provider's full source metadata record per
unique selected acquisition, under <output_dir>/metadata/<provider>/.

This is the third tier of the provenance chain (see product_contract.py's
AcquisitionProvenance.source_metadata_ref): the concise normalized fields
already in the manifest point here for the full, unmodified upstream
record, so DataLoader never has to guess in advance which of a source
record's many fields will matter later -- it keeps all of them, verbatim,
under "sourceRecord". Nothing inside "sourceRecord" is renamed or
reshaped; only the small "snapshot" wrapper is DataLoader's own.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def sanitize_filename(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", item_id)


def snapshot_ref(provider: str, item_id: str) -> str:
    """Path relative to the run's output directory -- this is exactly what
    gets stored in AcquisitionProvenance.source_metadata_ref."""
    return f"metadata/{provider}/{sanitize_filename(item_id)}.json"


def write_snapshot(output_dir: Path, provider: str, item_id: str, source_record: dict) -> str:
    """Idempotent: if a snapshot for this (provider, item_id) already
    exists on disk -- from earlier in this run, or from an earlier run
    writing to the same output_dir -- it is left untouched rather than
    rewritten. Returns the relative ref regardless of whether a write
    happened."""
    rel_path = snapshot_ref(provider, item_id)
    full_path = Path(output_dir) / rel_path
    if not full_path.exists():
        full_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "snapshot": {
                "capturedBy": "DataLoader",
                "provider": provider,
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "providerItemId": item_id,
            },
            "sourceRecord": source_record,
        }
        full_path.write_text(json.dumps(doc, indent=2, default=str))
    return rel_path

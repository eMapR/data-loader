"""DataLoader: config-driven satellite imagery acquisition.

Describe the imagery a pipeline needs in a YAML/JSON config (provider,
sensor, AOI or tiles, years/season, bands, encoding); DataLoader finds it,
downloads it, and writes GeoTIFFs plus a versioned manifest.json and
items.jsonl that say exactly what was acquired, from where, and what was
done to it.

    data-loader run examples/quickstart.yaml          # command line

    from data_loader import load_config, run, open_dataset
    summary = run(load_config("examples/quickstart.yaml"))
    ds = open_dataset(summary.output_dir)
    for path, item, f in ds.files():
        ...

See README.md and docs/ for the config reference and output contract.
"""
from __future__ import annotations

from data_loader._version import __version__
from data_loader.config import Config, ConfigError, config_from_dict, load_config
from data_loader.dataset import Dataset, DatasetError, open_dataset
from data_loader.engine import RunSummary, check_request, run

__all__ = [
    "__version__",
    "Config",
    "ConfigError",
    "Dataset",
    "DatasetError",
    "RunSummary",
    "check_request",
    "config_from_dict",
    "load_config",
    "open_dataset",
    "run",
]

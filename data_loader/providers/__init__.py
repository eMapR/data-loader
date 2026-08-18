"""Provider registry: config's `provider: <name>` string -> factory."""
from __future__ import annotations

from data_loader.providers import aws_earth_search, gee, planetary_computer, usgs_m2m

FACTORIES = {
    "planetary_computer": planetary_computer.make_provider,
    "aws_earth_search": aws_earth_search.make_provider,
    "gee": gee.make_provider,
    "usgs_m2m": usgs_m2m.make_provider,
}


def get_provider(name: str, **provider_options):
    if name not in FACTORIES:
        raise ValueError(f"Unknown provider {name!r}. Available: {sorted(FACTORIES)}")
    return FACTORIES[name](**provider_options)

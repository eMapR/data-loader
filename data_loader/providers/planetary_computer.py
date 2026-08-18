"""Microsoft Planetary Computer STAC provider. Free, no account needed to
search/read. Asset keys verified live against
https://planetarycomputer.microsoft.com/api/stac/v1 :
landsat-c2-l2 -> blue/green/red/nir08/swir16/swir22/qa_pixel,
sentinel-2-l2a -> B02/B03/B04/B08/B11/B12/SCL.
"""
from __future__ import annotations

from data_loader.providers.stac_common import SensorStacSpec, StacConfig, StacProvider

CONFIG = StacConfig(
    name="planetary_computer",
    stac_url="https://planetarycomputer.microsoft.com/api/stac/v1",
    needs_signing=True,
    sensors={
        "landsat": SensorStacSpec(
            collection="landsat-c2-l2",
            band_map={
                "blue": "blue", "green": "green", "red": "red",
                "nir": "nir08", "swir1": "swir16", "swir2": "swir22",
                "qa": "qa_pixel",
            },
            qa_kind="landsat_qa_pixel",
            sr_scale=2.75e-5, sr_offset=-0.2,
            platform_filter=("landsat-5", "landsat-7", "landsat-8", "landsat-9"),
        ),
        "sentinel2": SensorStacSpec(
            collection="sentinel-2-l2a",
            band_map={
                "blue": "B02", "green": "B03", "red": "B04",
                "nir": "B08", "swir1": "B11", "swir2": "B12",
                "qa": "SCL",
            },
            qa_kind="sentinel2_scl",
            sr_scale=0.0001, sr_offset=0.0,
        ),
    },
)


def make_provider() -> StacProvider:
    return StacProvider(CONFIG)

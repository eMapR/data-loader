"""AWS Open Data via Element84's Earth Search STAC API. Asset keys
verified live against https://earth-search.aws.element84.com/v1 :
landsat-c2-l2 -> blue/green/red/nir08/swir16/swir22/qa_pixel (same
convention as Planetary Computer's mirror of the same USGS collection),
sentinel-2-l2a -> blue/green/red/nir/swir16/swir22/scl (plain names here,
unlike PC's raw B02/B03/... band codes).

Sentinel-2 here is genuinely free/anonymous (plain https, public bucket,
verified live). Landsat is NOT — its assets live in s3://usgs-landsat, a
Requester Pays bucket, so it needs a real AWS account with credentials
configured and incurs small per-request charges; use
provider: planetary_computer for free Landsat instead. See
`requires_aws_credentials` below and stac_common.py's upfront check.
"""
from __future__ import annotations

from data_loader.product_contract import ESA_S2_L2A, USGS_C2_L2
from data_loader.providers.stac_common import SensorStacSpec, StacConfig, StacProvider

CONFIG = StacConfig(
    name="aws_earth_search",
    stac_url="https://earth-search.aws.element84.com/v1",
    needs_signing=False,
    sensors={
        "landsat": SensorStacSpec(
            collection="landsat-c2-l2",
            product_family=USGS_C2_L2,
            band_map={
                "blue": "blue", "green": "green", "red": "red",
                "nir": "nir08", "swir1": "swir16", "swir2": "swir22",
                "qa": "qa_pixel",
            },
            qa_kind="landsat_qa_pixel",
            sr_scale=2.75e-5, sr_offset=-0.2,
            platform_filter=("landsat-5", "landsat-7", "landsat-8", "landsat-9"),
            requires_aws_credentials=True,
        ),
        "sentinel2": SensorStacSpec(
            collection="sentinel-2-l2a",
            product_family=ESA_S2_L2A,
            band_map={
                "blue": "blue", "green": "green", "red": "red",
                "nir": "nir", "swir1": "swir16", "swir2": "swir22",
                "qa": "scl",
            },
            qa_kind="sentinel2_scl",
            sr_scale=0.0001, sr_offset=0.0,
        ),
    },
)


def make_provider() -> StacProvider:
    return StacProvider(CONFIG)

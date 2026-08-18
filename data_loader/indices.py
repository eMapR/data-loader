"""Spectral indices as numpy functions over a {band_name: array} dict of
already-scaled surface reflectance (not raw DN). Tasseled Cap coefficients
copied from GeoTimeSeriesApp3's gee_export/export_timeseries.py (Crist 1985,
TM-based reflectance, applied uniformly across sensors — same simplification
that script already makes) — copied rather than imported since that script
stays a standalone deployable file.
"""
from __future__ import annotations

import numpy as np

TASSELED_CAP_COEFFICIENTS = {
    "tcb": {"blue": 0.2043, "green": 0.4158, "red": 0.5524,
            "nir": 0.5741, "swir1": 0.3124, "swir2": 0.2303},
    "tcg": {"blue": -0.1603, "green": -0.2819, "red": -0.4934,
            "nir": 0.7940, "swir1": -0.0002, "swir2": -0.1446},
    "tcw": {"blue": 0.0315, "green": 0.2021, "red": 0.3102,
            "nir": 0.1594, "swir1": -0.6806, "swir2": -0.6109},
}

REQUIRED_BANDS = {
    "nbr": ("nir", "swir2"),
    "ndvi": ("nir", "red"),
    "tcb": tuple(TASSELED_CAP_COEFFICIENTS["tcb"]),
    "tcg": tuple(TASSELED_CAP_COEFFICIENTS["tcg"]),
    "tcw": tuple(TASSELED_CAP_COEFFICIENTS["tcw"]),
}


def _normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    denom = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (a - b) / denom
    out[denom == 0] = np.nan
    return out


def nbr(bands: dict[str, np.ndarray]) -> np.ndarray:
    return _normalized_difference(bands["nir"], bands["swir2"])


def ndvi(bands: dict[str, np.ndarray]) -> np.ndarray:
    return _normalized_difference(bands["nir"], bands["red"])


def _tasseled_cap(bands: dict[str, np.ndarray], name: str) -> np.ndarray:
    coeffs = TASSELED_CAP_COEFFICIENTS[name]
    out = None
    for band_name, coeff in coeffs.items():
        term = bands[band_name].astype("f4") * coeff
        out = term if out is None else out + term
    return out


def tcb(bands: dict[str, np.ndarray]) -> np.ndarray:
    return _tasseled_cap(bands, "tcb")


def tcg(bands: dict[str, np.ndarray]) -> np.ndarray:
    return _tasseled_cap(bands, "tcg")


def tcw(bands: dict[str, np.ndarray]) -> np.ndarray:
    return _tasseled_cap(bands, "tcw")


INDEX_FUNCS = {"nbr": nbr, "ndvi": ndvi, "tcb": tcb, "tcg": tcg, "tcw": tcw}


def compute_index(name: str, bands: dict[str, np.ndarray]) -> np.ndarray:
    return INDEX_FUNCS[name](bands)


def required_raw_bands(index_names) -> set[str]:
    needed: set[str] = set()
    for name in index_names:
        needed.update(REQUIRED_BANDS[name])
    return needed

"""Minimal usage examples for the config-driven data loader.

Run: python example.py
"""
from data_loader import load_config, pixel_index, run

LON, LAT = -123.845, 45.889
TARGET_EPSG = "EPSG:32610"

if __name__ == "__main__":
    config = load_config("examples/annual_composite.yaml")
    result = run(config)

    landsat = result["landsat"]
    r, c = pixel_index(landsat["grid"].transform, LON, LAT, TARGET_EPSG)

    print(f"\n{len(landsat['composites'])} year(s), grid "
          f"{landsat['grid'].width}x{landsat['grid'].height}")
    for year, composite in sorted(landsat["composites"].items()):
        v = composite["indices"]["nbr"][r, c]
        print(f"{year}  {v * 1000:7.0f}" if v == v else f"{year}     nan")

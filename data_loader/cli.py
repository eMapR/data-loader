"""`python -m data_loader --config path/to/config.yaml`"""
from __future__ import annotations

import argparse

from data_loader.config import load_config
from data_loader.engine import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to a YAML or JSON run config")
    args = parser.parse_args()

    config = load_config(args.config)
    result = run(config)

    for sensor, sensor_result in result.items():
        if "composites" in sensor_result:
            n = len(sensor_result["composites"])
            print(f"{sensor}: {n} annual composite(s) — years {sorted(sensor_result['composites'])}")
        else:
            n = len(sensor_result["scenes"])
            print(f"{sensor}: {n} scene(s)")

    if config.output.write_files:
        print(f"\nWrote GeoTIFFs + manifest.json to {config.output.dir}/")


if __name__ == "__main__":
    main()

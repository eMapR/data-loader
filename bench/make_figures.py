#!/usr/bin/env python
"""Generate tracked figures in docs/figures/ from the compact benchmark
summaries in docs/benchmarks/data/ (run bench/summarize_landtrendr.py first).

Usage:
    python bench/make_figures.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "docs" / "benchmarks" / "data" / "landtrendr_scaling.json"
FIG_DIR = REPO / "docs" / "figures"

# Categorical slots 1-2 of the validated default chart palette; the text
# uses ink colors, never the series color.
SERIES = {"usgs_ard": ("#2a78d6", "USGS ARD"), "planetary_computer": ("#eb6834", "Planetary Computer")}
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def per_unit_vs_window(summary: list[dict]) -> Path:
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for prov, (color, label) in SERIES.items():
        pts = sorted((e["providers"][prov]["grid_mpx"], e["providers"][prov]["per_unit_read_s"], e["smoke"], e["aoi"])
                     for e in summary if prov in e["providers"])
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        ax.plot(xs, ys, color=color, lw=2, label=label, zorder=2)
        for x, y, smoke, aoi in pts:
            ax.scatter([x], [y], s=64, color=SURFACE if smoke else color, edgecolors=color,
                       linewidths=2, zorder=3)
        ax.annotate(f"{label}  {ys[-1]:.0f} s", (xs[-1], ys[-1]), xytext=(8, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK)
    tiles = sorted({(e["providers"][p]["grid_mpx"], e["aoi"].split("_")[0])
                    for e in summary for p in e["providers"]})
    ax.set_xscale("log")
    ax.set_xticks([t[0] for t in tiles])
    ax.set_xticklabels([f"{t[1].replace('tile', ' tiles')}\n{t[0]:g} Mpx" for t in tiles], fontsize=9, color=INK_2)
    ax.minorticks_off()
    ax.set_ylim(bottom=0)
    ax.set_ylabel("Read time per unit (s)", color=INK_2, fontsize=9)
    ax.set_title("Per-unit read cost vs. AOI window size (LandTrendr-ready pipeline)",
                 loc="left", fontsize=11, color=INK)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.legend(frameon=False, fontsize=9, loc="upper left", labelcolor=INK)
    if any(e["smoke"] for e in summary):
        fig.text(0.01, 0.01, "Hollow markers: smoke-test sample (subset of dates), not a full run.",
                 fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.03, 0.93, 1))
    out = FIG_DIR / "landtrendr_per_unit_vs_window.png"
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return out


def main():
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    summary = json.loads(DATA.read_text())
    print("wrote", per_unit_vs_window(summary).relative_to(REPO))


if __name__ == "__main__":
    main()

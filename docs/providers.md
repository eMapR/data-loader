# Providers

A provider is where DataLoader gets imagery. Every provider sits behind
the same interface (`data_loader/providers/base.py`), so switching sources
is a one-line config change, and every provider produces the same output
layout and manifest.

Providers can serve *different scientific products* for the same sensor.
DataLoader names each one with a **product family**, records it in the
manifest, and refuses to substitute one for another when
`sensors[].product_family` is pinned.

## At a glance

| Provider | Sensor → product family | Years | Account / cost | Native encoding + QA band | Tiles |
|---|---|---|---|---|---|
| `usgs_ard` | Landsat 4, 5, 7, 8, 9 → `usgs_ard_sr` (C2 U.S. ARD surface reflectance) | 1982–present | Free; USGS EROS account + M2M token | yes | yes (CONUS ARD grid) |
| `planetary_computer` | Landsat 5, 7, 8, 9 → `usgs_c2_l2`; Sentinel-2 → `esa_s2_l2a` | Landsat 1984–, S2 2015– | Free, anonymous | yes | no |
| `aws_earth_search` | Sentinel-2 → `esa_s2_l2a`; Landsat → `usgs_c2_l2` | as above | S2 free, anonymous. Landsat is **Requester Pays**: needs AWS credentials and bills that account | yes | no |
| `gee` | Landsat → `usgs_c2_l2`; Sentinel-2 → `esa_s2_l2a_harmonized` | | Earth Engine project | no (float32 only) | no |
| `glad_ard` | Landsat → `glad_ard` (16-day normalized composites) | 2020– on the public mirror | Free, anonymous | no | no |
| `usgs_m2m` | Landsat → `usgs_c2_l2` (scene bundles) | | USGS EROS + M2M | no | no |

## When to use which

**Landsat time series, archives, anything long-term: `usgs_ard`.** This is
eMapR's preferred Landsat route:

- **Authoritative source.** USGS distributes the data directly, which is
  the least likely to change or disappear.
- **Complete.** It includes Landsat 4; DataLoader's Planetary Computer
  Landsat path reads Landsat 5–9 only.
- **Analysis-ready tiles.** Each observation is already mosaicked onto a
  fixed 5000×5000 px Albers tile. There are no partial same-date scenes to
  merge, and full-tile reads need no resampling at all.

On speed, eMapR's benchmarks (see [development](development/README.md))
found ARD and Planetary Computer roughly comparable. They were nearly tied
at 2 tiles, ARD was 9.5% faster at 4 tiles, and an 8-tile smoke test of
only 6 dates projected Planetary Computer faster, with wide uncertainty.
The choice of ARD rests on the points above, not on speed. The two
products agree closely on shared dates.

**Quick Landsat or Sentinel-2 pulls without an account:
`planetary_computer`.** Good for trying things out and for small AOIs.

**Sentinel-2: `planetary_computer` or `aws_earth_search`.** Both serve ESA
L2A. Earth Search sometimes lists two processing versions of the same
acquisition; `processing_version_policy` picks one (see
[configuration](configuration.md#provider-and-sensors)). Direct access
from ESA's Copernicus Data Space Ecosystem is planned and will slot in as
another provider.

**Exploration in Earth Engine: `gee`.** It suits small AOIs only: each
request is capped at about 48 MB. Its Sentinel-2 is Google's *harmonized*
collection, a different product family from raw L2A.

**`glad_ard`**: only if you specifically want GLAD's normalized 16-day
composites.

**`usgs_m2m`**: experimental. Authentication and search are verified
live, but the scene-bundle download path has not been exercised end to
end. Prefer `usgs_ard`.

## Provider notes

### `usgs_ard`: Landsat Collection 2 U.S. ARD, direct from USGS

- **Discovery:** the public LandsatLook STAC API (`landsat-c2ard-sr`),
  anonymous.
- **Download:** short-lived signed URLs minted through the M2M API (product
  "C2 ARD Tile Band Download"). DataLoader requests only the band files it
  needs, about 7 files per observation, never whole bundles. It doesn't
  use AWS, because the ARD S3 bucket is Requester Pays.
- **Full-tile reads** on the native grid download each band file whole, in
  parallel, and decode it in memory: about 10 s per observation versus
  about 110 s for range reads, with identical output. Bbox reads use
  windowed range requests.
- **Edge slivers:** neighboring WRS-2 paths often clip a tile's edge.
  Those observations are mostly fill, but they're real data. Tile requests
  include them. In Oregon they are about a third of all observations.
- **M2M limits:** one API call at a time per account, and sessions expire
  (DataLoader re-logs in every 90 min). See
  [getting started](getting-started.md#usgs-eros-account-and-m2m-token-usgs_ard-usgs_m2m).
- **Values:** SR uint16 DN, reflectance = DN × 2.75e-5 − 0.2, 0 = fill.
  QA is the Collection 2 `QA_PIXEL` bitmask.

### `planetary_computer`

Microsoft's STAC API. Asset URLs are signed automatically and re-signed if
they expire mid-run. Its Landsat collection includes Landsat 5, 7, 8 and 9
only.

### `aws_earth_search`

Element 84's Earth Search STAC API over AWS Open Data. Sentinel-2 L2A COGs
are public. Landsat lives in `s3://usgs-landsat`, a Requester Pays bucket.

### Sentinel-2 reflectance offset (both STAC providers)

ESA processing baseline 04.00 (in use since 2022-01-25) adds −1000 DN to
L2A surface reflectance. The two catalogs handle it differently, and
DataLoader handles each:

- **Planetary Computer** serves ESA's values unchanged. DataLoader applies
  offset −0.1 for baseline ≥ 04.00 and 0 before that.
- **Earth Search** has already removed the offset from the pixels of items
  marked `earthsearch:boa_offset_applied: true`, so DataLoader applies
  offset 0. Their `raster:bands` metadata still says −0.1; applying it
  would be wrong.

Both cases were checked against dark open-ocean pixels (tile 10TDQ, July
2021 and July 2023). The offset used is recorded per item
(`provenance.extra.sr_offset`). With `encoding: native` the stored DN are
untouched, and the per-file offset is in `items.jsonl`
(`files[].bandScaleOffset`) and in each GeoTIFF's band metadata. Versions
before 1.0 applied no offset, so their 2022+ Planetary Computer Sentinel-2
reflectance is about 0.1 too high.

## Adding a provider

Implement `search_scenes` and `read_scene_bands`, and declare
`capabilities()`; that's enough for float32 output. For native encoding
and QA bands, add `read_scene_native` + `native_encoding`. For named
tiles, add `tile_grid` + `search_tile`. Then register the provider in
`data_loader/providers/__init__.py`. [Development](development/README.md)
covers the interface in detail.

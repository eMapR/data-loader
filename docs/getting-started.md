# Getting started

## 1. Install

You need Python 3.9 or newer and pip; nothing else. GDAL is bundled with
the `rasterio` wheel.

```bash
git clone https://github.com/eMapR/data-loader.git
cd data-loader
python3 -m venv .venv
source .venv/bin/activate
pip install -e .              # the data-loader command + the data_loader Python package
pip install -e '.[dev]'       # + pytest, to run the tests
pip install -e '.[gee]'       # + earthengine-api, only for provider: gee
```

Check the install:

```bash
data-loader --version
pytest                        # ~5 s, no network or credentials needed
```

`python -m data_loader ...` is equivalent to `data-loader ...`.

## 2. Credentials

Credentials are read from environment variables and never from config
files. That way a config can be shared or committed safely, and the
manifest never records them. [`.env.example`](../.env.example) lists them.
Copy it to `.env` (git-ignored), fill in what you need, and load it before
running:

```bash
cp .env.example .env          # then edit .env
set -a; source .env; set +a
```

| Provider | What it needs |
|---|---|
| `planetary_computer` | Nothing. |
| `aws_earth_search` | Nothing for Sentinel-2. Landsat is in a *Requester Pays* bucket: AWS credentials are required and requests are billed to that AWS account. |
| `usgs_ard` | USGS EROS username + M2M application token (below). Discovery and `data-loader plan` work without them; downloading needs them. |
| `usgs_m2m` | Same as `usgs_ard`. |
| `gee` | An Earth Engine-enabled Google Cloud project (`GEE_PROJECT`), and `earthengine authenticate` once. |
| `glad_ard` | Nothing. |

`data-loader validate CONFIG` warns if the configured provider is missing
credentials.

### USGS EROS account and M2M token (`usgs_ard`, `usgs_m2m`)

USGS serves Landsat ARD directly. Access is free, but it needs an account
with machine-to-machine (M2M) API access:

1. **Create an EROS account** at <https://ers.cr.usgs.gov/register>.
2. **Request M2M API access** from your ERS profile (the access-request
   page). USGS reviews these requests by hand; allow a few days.
3. **Generate an application token** for M2M from your ERS profile. Use
   the token, not your password.
4. Set the variables:

   ```bash
   export USGS_M2M_USERNAME=your_ers_username
   export USGS_M2M_TOKEN=the_application_token
   ```

**What to know about M2M:**

- **One request at a time per account.** USGS rejects concurrent M2M
  calls from one account. DataLoader queues its own M2M calls behind one
  lock, so `workers: 4` still helps: the actual file downloads run in
  parallel and only the short URL-signing calls wait in line. Two
  *separate* DataLoader runs on the same account, or the same account
  used from another machine, will slow each other down. Avoid that for
  long jobs.
- **Sessions expire.** DataLoader logs in again every 90 minutes
  automatically, so long runs aren't affected.
- **`AUTH_INVALID` errors.** The usual cause is the token: generate a new
  one. See [troubleshooting](troubleshooting.md).
- **Discovery is anonymous.** It uses the public LandsatLook STAC API, so
  `data-loader plan` works before your M2M access is approved.

## 3. First runs

```bash
data-loader validate examples/quickstart.yaml
data-loader run examples/quickstart.yaml
data-loader status output/quickstart
data-loader verify output/quickstart
```

Then try one of the other examples:

| Example | Provider | What it does |
|---|---|---|
| [`quickstart.yaml`](../examples/quickstart.yaml) | Planetary Computer | Landsat scenes for one month over a small AOI, plus NDVI and QA |
| [`seasonal_composite.yaml`](../examples/seasonal_composite.yaml) | Planetary Computer | One masked median composite per summer, 2019–2023 |
| [`sentinel2_scenes.yaml`](../examples/sentinel2_scenes.yaml) | Earth Search | Sentinel-2 scenes at 10 m, with the SCL band |
| [`usgs_ard_aoi.yaml`](../examples/usgs_ard_aoi.yaml) | USGS ARD | Landsat ARD for a small AOI, reprojected to EPSG:5070 |
| [`usgs_ard_tile_archive.yaml`](../examples/usgs_ard_tile_archive.yaml) | USGS ARD | One full ARD tile, one year, stored as USGS distributes it |
| [`oregon_landsat_ard_archive.yaml`](../examples/oregon_landsat_ard_archive.yaml) | USGS ARD | eMapR's Oregon archive: 23 tiles, 1990 to present |

`--output-dir` overrides `output.dir`, so you can run an example into a
location of your choice without editing it.

Next: [configuration](configuration.md) to write your own request, and
[outputs](outputs.md) to read the results.

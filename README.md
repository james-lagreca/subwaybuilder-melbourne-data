# subwaybuilder-melbourne-data

Map + demand data pipeline producing a **Railyard map** for [Subway Builder Modded](https://subwaybuildermodded.com/).

Greater Melbourne + Geelong + full Mornington Peninsula + Phillip Island, working bbox `[144.25, -38.55, 145.65, -37.55]` — Lara/Geelong → Pakenham, Craigieburn → Cape Schanck/Penguin Parade. Modelled on Will Barouch's [Sydney](https://github.com/WillBarouch/subwaybuilder-sydney) pipeline.

The generated artefacts are bundled into `melbourne.zip` and uploaded as a GitHub Release asset. The Railyard map manager (in-game) installs from this release into `metro-maker4/cities/data/MEL/`. No standalone TypeScript mod required — Railyard's built-in `mapLoader` registers the city.

## Install

In-game, open **Library → Install Maps → search "Melbourne"** once this map is in the Railyard registry. Until then, use **Import Asset → Choose ZIP** and select `melbourne.zip` from the latest [GitHub Release](https://github.com/james-lagreca/subwaybuilder-melbourne-data/releases/latest).

## Output artefacts

Each tagged GitHub Release ships these files inside `melbourne.zip` (flat structure, no subfolders):

| File | Produced by | Purpose |
|---|---|---|
| `MEL.pmtiles` | `build_basemap.py` (depot) | Vector basemap tiles (basemap layers + 3D building extrusions) |
| `buildings_index.json` | `build_basemap.py` (depot) | Per-building footprints used for station placement |
| `roads.geojson` | `build_basemap.py` (depot) | Road network for surface routing |
| `runways_taxiways.geojson` | `build_basemap.py` (depot) | Airport runways/taxiways |
| `demand_data.json` | `build_base_demand.py` + `demand_generator.py` + `peak_penalty.py` | Population, jobs, schools, attractions, bases — 13,395 demand points, 71k+ OSRM-routed commute flows |
| `config.json` | hand-authored | Railyard map metadata |

The individual files are also uploaded loose alongside the zip for direct-URL access.

## License

[MIT](./LICENSE). The generated map artefacts derive from third-party data sources (OSM, Overture, ABS 2021 Census, VIC Department of Education); see [LICENSE](./LICENSE) for full attribution and the licences that propagate to those derivative files.

## One-time setup

**Automated (recommended on Windows 11):**

Two scripts in [`setup/`](./setup) install everything except the two
downloads that need a manual EULA / signin (Docker Desktop EULA and the
VIC schools XLSX).

1. From an **elevated PowerShell** on Windows:
   ```powershell
   cd D:\SubwayBuilder_Mods\subwaybuilder-melbourne-data\setup
   Set-ExecutionPolicy -Scope Process Bypass
   .\setup-prereqs.ps1
   ```
   This installs Docker Desktop via winget and the Ubuntu WSL2 distro,
   then stages `setup-prereqs.sh` into `/opt/subwaybuilder-melbourne/`
   inside Ubuntu.

2. Launch Docker Desktop once, accept its EULA, and toggle on
   **Settings → Resources → WSL Integration → Ubuntu**. Restart Docker.

3. Open Ubuntu (`wsl -d Ubuntu`) and complete first-time user setup,
   then run the Linux-side installer:
   ```bash
   sudo bash /opt/subwaybuilder-melbourne/setup-prereqs.sh
   ```
   It installs `tippecanoe`, `osmium`, `mapshaper`, `pmtiles`, `java`,
   `planetiler`, `jq`, `sqlite3`, Miniconda, and the depot conda env
   with the `depot` package itself installed editable. Re-runnable.

4. Download manually into this directory (the scripts don't fetch these):
   - Demand-Adder binary from
     https://github.com/rslurry/Demand-Adder/releases →
     `create_new_demand_points_windows_x86-64.exe`
   - VIC schools data from
     https://discover.data.vic.gov.au/dataset/all-schools-list-and-school-locations →
     `dv403-AllSchoolsEnrolments-2025.csv` and `dv402-SchoolLocations2025.csv`.
     Covers Government, Catholic, and Independent — single source for the
     whole sector. Joined on `School_No`; `X`/`Y` are longitude/latitude.

**Manual install** (if you want to skip the scripts): every tool the
scripts touch is just `apt install` / `npm i -g mapshaper` / `go install`
/ `conda env create -f depot/environment.yml`. Read
[`setup/setup-prereqs.sh`](./setup/setup-prereqs.sh) — it's the
reference.

## Step 1 — Clip Melbourne OSM extract

```bash
# Australia-wide (~1.5 GB).
curl -O https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf

# Clip to Greater Melbourne.
osmium extract \
  -b 144.25,-38.55,145.65,-37.55 \
  australia-latest.osm.pbf \
  -o melbourne.osm.pbf
```

## Step 2 — Build the basemap

```bash
conda activate depot
python build_basemap.py
```

Outputs land in `./MEL/` (depot's default — subdir named after the city code). On first run, uncomment the `mapgen.check_labels()` call in `build_basemap.py` to confirm the `place=*` tag coverage in OSM Melbourne matches the `cities` / `suburbs` / `neighborhoods` lists; adjust then re-run.

Verify the result by opening `MEL/MEL.pmtiles` in [pmtiles.io](https://pmtiles.io) — confirm Melbourne CBD renders with road labels in English and suburb labels appear at the expected zoom.

## Step 3 — Build the OSRM routing graph

```bash
# These run in-place against melbourne.osm.pbf and emit melbourne.osrm + sidecars.
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/melbourne.osm.pbf
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-partition /data/melbourne.osrm
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-customize /data/melbourne.osrm
```

Smoke test (run once the demand pipeline starts the container):

```bash
curl 'http://localhost:5000/route/v1/driving/144.9631,-37.8136;145.0458,-37.8770'
```

Expect a routes array with a sensible duration (a few minutes Flinders St → MCG).

## Step 4 — Generate demand

```bash
python demand_generator.py --config melbourne.json --pbf melbourne.osrm
```

The script:
1. Reads the VIC school XLSX, filters to bbox + minimum enrolment, injects each surviving school as a `universities[]` demand point in `melbourne_with_schools.json`.
2. Boots the `osrm_melbourne` Docker container (skipped if already healthy on :5000).
3. Runs `create_new_demand_points_windows_x86-64.exe` with the merged config, which writes `demand_data_out.json`.

Rename the output to `demand_data.json` before uploading to the GitHub Release.

## Step 5 — Release

Tag and upload:

```bash
git tag v0.1.0
git push --tags
gh release create v0.1.0 \
  out/melbourne.pmtiles \
  out/melbourne_foundations.pmtiles \
  out/buildings_index.json \
  out/roads.geojson \
  out/runways_taxiways.geojson \
  demand_data.json \
  thumbnail.png
```

The URLs in `subwaybuilder-melbourne/src/main.ts` resolve to these assets — bump `DATA_VERSION` in that file when you cut a new tag.

## Data sources & licences

- OSM data © OpenStreetMap contributors, ODbL.
- VIC school data: Victorian Department of Education open data, licensed CC BY 4.0.
- Census-derived population/jobs come from ABS Mesh Blocks 2021 / Destination Zones 2021, consumed inside the demand-adder binary.
- Generated map artefacts are derivative works of the above. Distribute under licences compatible with each upstream.

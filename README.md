# Melbourne for Subway Builder Modded

A custom **Railyard map** of Greater Melbourne, Geelong, the Mornington Peninsula, and Phillip Island for [Subway Builder Modded](https://subwaybuildermodded.com/).

Bounding box: `[144.25, -38.55, 145.65, -37.55]` — Lara/Geelong → Pakenham, Craigieburn → Cape Schanck / Phillip Island Penguin Parade. Modelled on Will Barouch's [Sydney](https://github.com/WillBarouch/subwaybuilder-sydney) mod.

---

## Install

### Option 1 — Railyard registry (when this map is approved)

In **Subway Builder Modded** → **Library** → **Install Maps** → search **Melbourne** → click Install. Railyard handles everything from there.

### Option 2 — Manual import from a GitHub Release ZIP

Until the registry submission lands (or if you're testing a newer version):

1. Download `melbourne.zip` from the [latest GitHub Release](https://github.com/james-lagreca/subwaybuilder-melbourne-data/releases/latest).
2. Open **Subway Builder Modded**.
3. Go to the **Library** (Maps screen) and click **Import Asset** (top-right).
4. Choose **Map** → **Choose ZIP** → select the downloaded `melbourne.zip`.
5. Railyard extracts it into `metro-maker4/cities/data/MEL/` and registers MEL in `installed_maps.json` automatically.
6. **Quit and relaunch the game.** Select **MEL — Melbourne** from the city picker.

> If after relaunch the basemap is blank, make sure the Railyard desktop app is running — it serves the vector tiles on a localhost port that `mapLoader` reads. Closing Railyard kills the tile server.

---

## What's in this map

**Demand layer** — 13,395 demand points, 71,382 OSRM-routed commute flows:

| Layer | Points | Source |
|---|---|---|
| Residential SA1s | 12,034 | ABS 2021 Census G01 `Tot_P_P` |
| Schools (primary / secondary / combined / special) | 1,356 | VIC Department of Education (Government, Catholic, Independent) |
| Universities | 8 | Hand-curated (UniMelb, Monash, RMIT, Deakin, Swinburne, La Trobe, VU, ACU) |
| Airports | 3 | MEL Tullamarine, AVV Avalon, MEB Essendon |
| Entertainment venues | 38 | MCG, Marvel Stadium, AAMI Park, Crown, Federation Square, Royal Botanic Gardens, MCEC, NGV, Queen Vic Market, Albert Park, Shrine of Remembrance, Royal Exhibition Building, Chadstone, Highpoint, Eastland, Doncaster, Southland, Northland, Knox, Fountain Gate, Luna Park, Melbourne / Werribee Open Range / Healesville Sanctuary zoos, SEA LIFE, Scienceworks, Flemington / Caulfield / Moonee Valley / Sandown racecourses, GMHBA Stadium, Westfield Geelong, Phillip Island Penguin Parade, South Melbourne / Prahran / Footscray markets, Mt Dandenong, Brighton / Williamstown / Sorrento beaches |
| Hospitals | 14 | RMH, Royal Children's, Alfred, MMC Clayton, Austin, St Vincent's, Western, Box Hill, Northern Epping, Sunshine, Frankston, Casey, Maroondah, Epworth, Cabrini, Mercy, Royal Women's |
| Defence bases | 8 | HMAS Cerberus, RAAF Williams Laverton, RAAF Williams Point Cook, Victoria Barracks, Simpson, Watsonia, Maygar, Albert Park |

**Basemap** — depot-generated PMTiles with 3D building extrusions, road network, place labels for cities/towns/suburbs/villages/neighbourhoods/hamlets/localities/quarters.

**Demand modelling** — gravity model (β = 1.4, 5 km close-distance plateau, 8 destinations per residence, 80 km cap, 45% employment rate); SA2 Place-of-Work jobs distributed by inverse SA1 area (activity-centre concentration); distance-graduated peak-hour driving penalty (×1.1 / ×1.3 / ×1.5 / ×1.6 by trip length).

---

## Release contents

Each tagged release ships a single `melbourne.zip` (flat layout, no subfolders — Railyard requires this):

| File | Size | Purpose |
|---|---|---|
| `config.json` | <1 KB | Railyard metadata (city code, name, initial view, version) |
| `MEL.pmtiles` | ~172 MB | Vector basemap + 3D building layer |
| `buildings_index.json` | ~386 MB | Per-building polygons + heights for station collision |
| `roads.geojson` | ~75 MB | Surface road network |
| `runways_taxiways.geojson` | ~1 MB | Airport runways/taxiways |
| `demand_data.json` | ~17 MB | All 13k demand points + 71k commute pops |

The individual files are also uploaded alongside the ZIP for direct-URL access.

---

## Build it yourself

If you want to regenerate the data — change the bbox, tune the gravity model, swap in a different census year, add custom venues — clone this repo and run the pipeline.

### One-time prerequisites (Windows 11 + WSL2)

Two scripts in [`setup/`](./setup) install almost everything. There are two manual downloads at the end (Demand-Adder binary + VIC schools CSVs).

```powershell
# From elevated PowerShell on Windows:
cd <repo>\setup
Set-ExecutionPolicy -Scope Process Bypass
.\setup-prereqs.ps1
```

This installs **Docker Desktop** via winget and **WSL2 Ubuntu**, then stages a Linux-side installer.

Then launch Docker Desktop once, accept the EULA, and toggle **Settings → Resources → WSL Integration → Ubuntu** on. Restart Docker.

Open Ubuntu (`wsl -d Ubuntu`), complete first-time user setup, and run:

```bash
sudo bash /opt/subwaybuilder-melbourne/setup-prereqs.sh
```

That installs `tippecanoe`, `osmium`, `mapshaper`, `pmtiles`, `java`, `planetiler`, `jq`, `sqlite3`, Miniconda, and the [`depot`](https://github.com/Subway-Builder-Modded/depot) conda env. Re-runnable.

Finally, download these two manually into the repo root (the scripts don't fetch them):

- **Demand-Adder binary**: https://github.com/rslurry/Demand-Adder/releases → save `create_new_demand_points_windows_x86-64.exe`
- **VIC schools data**: https://discover.data.vic.gov.au/dataset/all-schools-list-and-school-locations → save `dv403-AllSchoolsEnrolments-2025.csv` and `dv402-SchoolLocations2025.csv`

(For non-Windows users: every tool the installer touches is `apt install` / `npm i -g` / `conda env create` — read [`setup/setup-prereqs.sh`](./setup/setup-prereqs.sh) as a reference and translate to your OS.)

### Step 1 — Clip a Melbourne OSM extract

```bash
curl -O https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf
osmium extract -b 144.25,-38.55,145.65,-37.55 australia-latest.osm.pbf -o melbourne.osm.pbf
```

### Step 2 — Build the basemap

```bash
conda activate depot
python build_basemap.py
```

Outputs land in `./MEL/` — `MEL.pmtiles`, `buildings_index.json`, `roads.geojson`, `runways_taxiways.geojson`. ~30–90 min depending on CPU. On first run, uncomment `mapgen.check_labels()` in `build_basemap.py` to confirm the `place=*` tag coverage matches the `cities` / `suburbs` / `neighborhoods` lists.

Verify by opening `MEL/MEL.pmtiles` in [pmtiles.io](https://pmtiles.io).

### Step 3 — Build the OSRM routing graph

```bash
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/melbourne.osm.pbf
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-partition /data/melbourne.osrm
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-customize /data/melbourne.osrm
```

Smoke test once the demand pipeline starts the container:

```bash
curl 'http://localhost:5000/route/v1/driving/144.9631,-37.8136;145.0458,-37.8770'
```

### Step 4 — Build the base demand layer (ABS 2021 Census)

Requires four ABS files in the repo root — see [`build_base_demand.py`](./build_base_demand.py) for filenames:

- Mesh Block 2021 GDA2020 shapefile
- DZN 2021 GDA2020 shapefile
- `2021Census_G01_VIC_SA1.csv` (from the VIC GCP DataPack)
- `2021Census_W01A_VIC_POW_SA2.csv` (from the VIC WPP DataPack)

Then:

```bash
python build_base_demand.py
```

Generates `demand_data_fixed.json` with ~12k SA1 residential + jobs points and ~60k OSRM-routed gravity commute flows.

### Step 5 — Layer attractions on top

```bash
python demand_generator.py --config melbourne.json --pbf melbourne.osrm
```

Injects VIC schools, runs Demand-Adder, layers airports / universities / entertainment / bases. Outputs `demand_data_out.json` — rename to `demand_data.json`.

### Step 6 — Apply peak-hour driving penalty

```bash
python peak_penalty.py
```

Distance-graduated multiplier on every pop's `drivingSeconds` to model AM/PM congestion. In-place rewrite of `demand_data.json`.

### Step 7 — Package + release

```powershell
# from PowerShell on Windows
Compress-Archive -Path config.json,MEL\MEL.pmtiles,MEL\buildings_index.json,MEL\roads.geojson,MEL\runways_taxiways.geojson,demand_data.json `
  -DestinationPath melbourne.zip -CompressionLevel Optimal

gh release create v1.0.0 `
  --title "v1.0.0 — Melbourne" `
  --notes-file RELEASE_NOTES.md `
  melbourne.zip `
  MEL\MEL.pmtiles `
  MEL\buildings_index.json `
  MEL\roads.geojson `
  MEL\runways_taxiways.geojson `
  demand_data.json
```

Then submit a **Publish New Map** issue to the [Railyard registry](https://github.com/Subway-Builder-Modded/registry/issues/new?template=publish-map.yml) using the GitHub browser form.

---

## Tuning knobs

Most of the demand calibration lives in two files:

[`build_base_demand.py`](./build_base_demand.py):
- `GRAVITY_BETA` — distance decay exponent. Lower = more radial CBD pull.
- `CLOSE_DISTANCE_FLOOR_M` — minimum effective distance. Raising reduces local commuting.
- `DESTINATIONS_PER_RESIDENCE` — commute samples per origin.
- `EMPLOYMENT_RATE` — fraction of residents who commute (default 0.45).
- `MAX_COMMUTE_M` — hard cap on commute distance.

[`peak_penalty.py`](./peak_penalty.py):
- `TIERS` — distance breakpoints + multipliers for the peak-hour driving inflation.

[`melbourne.json`](./melbourne.json) is the Demand-Adder config — hand-add entries to `entertainment`, `airport`, `universities`, or `bases` to layer in extra demand points.

---

## Licence

[MIT](./LICENSE). The generated map artefacts derive from third-party data sources (OpenStreetMap, Overture Maps, ABS 2021 Census, VIC Department of Education); see [LICENSE](./LICENSE) for full attribution and the licences that propagate to those derivative files.

## Credits

- [`depot`](https://github.com/Subway-Builder-Modded/depot) — Subway Builder Modded map-generation library.
- [`Demand-Adder`](https://github.com/rslurry/Demand-Adder) by rslurry — attraction/base/university demand layering.
- [`subwaybuilder-sydney`](https://github.com/WillBarouch/subwaybuilder-sydney) by Will Barouch — pipeline structure adapted from this mod.
- [`OSRM`](https://github.com/Project-OSRM/osrm-backend) — routing engine.
- ABS, VIC Department of Education, OpenStreetMap, Overture Maps — data.

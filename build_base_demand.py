"""
Build Melbourne base demand layer from ABS 2021 Census data.

Produces `demand_data_fixed.json` with:
  - One point per SA1 statistical area (~6,500 points in our bbox).
    Each point has real residents (from Mesh Block counts) and real jobs
    (from Destination Zone Place-of-Work counts, spatially joined to SA1).
  - Commute flows ("pops") generated via gravity model, then real-routed
    through the running OSRM container for drivingDistance / drivingSeconds.

Then Demand-Adder layers attractions/bases/schools on top of this file.

Required input files in the cwd (download from ABS — see README):
  MB_2021_AUST_GDA2020.shp           Mesh block boundaries (+ .dbf/.shx/.prj)
  2021Census_G01_VIC_SA1.csv         GCP Selected Person Characteristics by SA1
  DZN_2021_AUST_GDA2020.shp          Destination zone boundaries (+ sidecars)
  2021Census_W01*_AUST_DZN.csv       DZN Place-of-Work totals (W01A or W01B)

Required service: OSRM running on http://localhost:5000 (already started
by your earlier demand_generator.py run — check with `docker ps`).

Run inside the depot conda env so geopandas is available:
    conda activate depot
    python build_base_demand.py
"""

import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BBOX = (144.25, -38.55, 145.65, -37.55)
OSRM_BASE   = "http://localhost:5000"
OUTPUT_FILE = "demand_data_fixed.json"

# File-discovery candidates (different ABS download bundles use slightly
# different names; this lets you not have to rename your downloads).
MB_FILE_NAMES   = ["MB_2021_AUST_GDA2020.shp", "MB_2021_AUST_GDA94.shp",
                   "MB_2021_AUST_GDA2020.gpkg", "MB_2021_AUST_GDA94.gpkg"]
# GCP at SA1 level — Tot_P_P is total persons usually resident in each SA1.
SA1_GCP_NAMES   = ["2021Census_G01_VIC_SA1.csv",
                   "2021Census_G01_AUST_SA1.csv",
                   "2021Census_G01_AUS_SA1.csv"]
# SA2-level Working Population Profile (W01A). DZN-level isn't available
# through the ABS DataPacks UI, so we fall back to SA2 and proportionally
# distribute each SA2's jobs to its constituent SA1s.
SA2_POW_NAMES   = ["2021Census_W01A_VIC_POW_SA2.csv",
                   "2021Census_W01A_AUS_POW_SA2.csv",
                   "2021Census_W01A_AUST_POW_SA2.csv"]

PERSON_COLS = ["Tot_P_P", "Person", "Persons",
               "Persons_Usually_Resident_no", "PERSON"]
# Place-of-work totals in W01A are split by sex × age × employment status.
# There's no single "all persons" column; we compute it as M_Tot_Tot + F_Tot_Tot.
SA2_CODE_COLS = ["POW_SA2_CODE_2021", "SA2_CODE_2021", "SA2_CODE"]

# Gravity-model parameters.
DESTINATIONS_PER_RESIDENCE = 5     # Sampled commute destinations per residence
GRAVITY_BETA               = 2.0   # Distance decay exponent
EMPLOYMENT_RATE            = 0.45  # Fraction of residents that commute
MAX_COMMUTE_M              = 80_000

# OSRM concurrency. 16 threads + container on localhost handles ~500 req/s.
OSRM_THREADS = 16
OSRM_TIMEOUT = 10

# ---------------------------------------------------------------------------
# File / column discovery helpers
# ---------------------------------------------------------------------------

def find_file(names: list[str]) -> Path:
    for n in names:
        p = Path(n)
        if p.exists():
            return p
    sys.exit(
        f"ERROR: Couldn't find any of: {names}\n"
        f"       Working dir: {Path.cwd()}\n"
        f"       See README \"Step 1.5\" for ABS download links."
    )


def find_col(df: pd.DataFrame, candidates: list[str], label: str) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    sys.exit(
        f"ERROR: Couldn't find a '{label}' column.\n"
        f"       Tried: {candidates}\n"
        f"       Columns in file: {list(df.columns)[:30]}"
    )


def check_osrm() -> None:
    try:
        url = f"{OSRM_BASE}/nearest/v1/driving/144.9671,-37.8183"
        with urllib.request.urlopen(url, timeout=3) as r:
            if r.status != 200:
                raise RuntimeError(f"OSRM returned {r.status}")
    except Exception as e:
        sys.exit(
            f"ERROR: OSRM not reachable at {OSRM_BASE}.\n"
            f"       {e}\n"
            f"       Start it with `python demand_generator.py --config "
            f"melbourne_with_schools.json --pbf melbourne.osrm "
            f"--skip-schools` (it bails out after starting OSRM if input "
            f"file is empty), or run the docker command from README §3."
        )


# ---------------------------------------------------------------------------
# Load + aggregate
# ---------------------------------------------------------------------------

def load_sa1() -> gpd.GeoDataFrame:
    """Read mesh blocks, dissolve to SA1, attach GCP G01 Tot_P_P."""
    mb_path  = find_file(MB_FILE_NAMES)
    gcp_path = find_file(SA1_GCP_NAMES)

    print(f"[1/6] Loading mesh block boundaries from {mb_path.name}...")
    mb = gpd.read_file(mb_path, bbox=BBOX)
    print(f"      {len(mb):,} MBs in bbox")
    mb = mb.rename(columns={
        "MB_CODE21":  "MB_CODE_2021",
        "SA1_CODE21": "SA1_CODE_2021",
    })
    if mb.crs is not None and mb.crs.to_epsg() != 4326:
        mb = mb.to_crs(epsg=4326)
    mb["SA1_CODE_2021"] = mb["SA1_CODE_2021"].astype(str)

    print(f"[2/6] Dissolving to SA1 (the slow step — ~1-3 min)...")
    t0 = time.time()
    sa1 = mb.dissolve(by="SA1_CODE_2021").reset_index()
    sa1 = sa1[["SA1_CODE_2021", "geometry"]]
    print(f"      Dissolve took {time.time() - t0:.1f}s; "
          f"{len(sa1):,} SA1s")

    print(f"[3/6] Joining residents from {gcp_path.name}...")
    gcp = pd.read_csv(gcp_path, dtype={"SA1_CODE_2021": str})
    person_col = find_col(gcp, PERSON_COLS, "person")
    gcp = gcp.rename(columns={person_col: "residents"})
    gcp["residents"] = pd.to_numeric(gcp["residents"], errors="coerce")\
        .fillna(0).astype(int)

    sa1 = sa1.merge(gcp[["SA1_CODE_2021", "residents"]],
                    on="SA1_CODE_2021", how="left")
    sa1["residents"] = sa1["residents"].fillna(0).astype(int)
    print(f"      {sa1['residents'].sum():,} residents matched to SA1s")
    return sa1


def add_jobs(sa1: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Read SA2 POW counts, distribute proportionally to each SA2's
    constituent SA1s. ABS spec: SA2_CODE_2021 = first 9 chars of
    SA1_CODE_2021, so the SA1->SA2 mapping is purely code-derived."""
    pow_path = find_file(SA2_POW_NAMES)

    print(f"[4/6] Loading SA2 POW counts from {pow_path.name}...")
    pow_df = pd.read_csv(pow_path)

    # Find the SA2 code column (named POW_SA2_CODE_2021 in W01A).
    sa2_code_col = None
    for c in SA2_CODE_COLS:
        if c in pow_df.columns:
            sa2_code_col = c
            break
    if sa2_code_col is None:
        sys.exit(f"ERROR: Couldn't find SA2 code column.\n"
                 f"       Tried: {SA2_CODE_COLS}\n"
                 f"       Have:  {list(pow_df.columns)[:10]}")

    if "M_Tot_Tot" not in pow_df.columns or "F_Tot_Tot" not in pow_df.columns:
        sys.exit(f"ERROR: W01A file missing M_Tot_Tot or F_Tot_Tot.\n"
                 f"       Have: {[c for c in pow_df.columns if 'Tot' in c][:10]}")

    pow_df["jobs"] = (pd.to_numeric(pow_df["M_Tot_Tot"], errors="coerce").fillna(0)
                      + pd.to_numeric(pow_df["F_Tot_Tot"], errors="coerce").fillna(0))\
        .astype(int)
    pow_df["SA2_CODE_2021"] = pow_df[sa2_code_col].astype(str)
    print(f"      {len(pow_df):,} SA2s in file; "
          f"{pow_df['jobs'].sum():,} total VIC POW")

    # Derive each SA1's parent SA2 from the SA1 code (first 9 digits).
    sa1["SA2_CODE_2021"] = sa1["SA1_CODE_2021"].astype(str).str[:9]
    sa1s_per_sa2 = sa1.groupby("SA2_CODE_2021").size()\
        .reset_index(name="sa1_count")

    pow_df = pow_df[["SA2_CODE_2021", "jobs"]].merge(
        sa1s_per_sa2, on="SA2_CODE_2021", how="inner")
    pow_df["jobs_per_sa1"] = (pow_df["jobs"]
                              / pow_df["sa1_count"].clip(lower=1)).round().astype(int)

    print(f"[5/6] Distributing SA2 jobs evenly across constituent SA1s...")
    sa1 = sa1.merge(pow_df[["SA2_CODE_2021", "jobs_per_sa1"]],
                    on="SA2_CODE_2021", how="left")
    sa1["jobs"] = sa1["jobs_per_sa1"].fillna(0).astype(int)
    sa1 = sa1.drop(columns=["jobs_per_sa1"])
    print(f"      {sa1['jobs'].sum():,} jobs distributed across "
          f"{(sa1['jobs'] > 0).sum():,} SA1s in bbox")
    return sa1


# ---------------------------------------------------------------------------
# Points + gravity model
# ---------------------------------------------------------------------------

def build_points(sa1: gpd.GeoDataFrame) -> list[dict]:
    sa1 = sa1[(sa1["residents"] > 0) | (sa1["jobs"] > 0)].copy()
    sa1["centroid"] = sa1.geometry.centroid
    sa1["lon"] = sa1["centroid"].x
    sa1["lat"] = sa1["centroid"].y

    points = []
    for _, row in sa1.iterrows():
        points.append({
            "id":        f"SA1_{row['SA1_CODE_2021']}",
            "location":  [round(row["lon"], 6), round(row["lat"], 6)],
            "residents": int(row["residents"]),
            "jobs":      int(row["jobs"]),
            "popIds":    [],
        })
    return points


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = (np.sin(dlat / 2) ** 2
         + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2))
         * np.sin(dlon / 2) ** 2)
    return 2 * R * np.arcsin(np.sqrt(a))


def gravity_commutes(points: list[dict], rng_seed: int = 42):
    """Sample DESTINATIONS_PER_RESIDENCE commute destinations per residence.
    Returns list of (origin_idx, dest_idx, size) tuples."""
    rng  = np.random.default_rng(rng_seed)
    locs = np.array([p["location"] for p in points])  # [lon, lat]
    res  = np.array([p["residents"] for p in points])
    jobs = np.array([p["jobs"] for p in points], dtype=float)

    flows = []
    for i in range(len(points)):
        if res[i] == 0:
            continue
        d = haversine_m(locs[i, 1], locs[i, 0], locs[:, 1], locs[:, 0])
        with np.errstate(invalid="ignore", divide="ignore"):
            w = jobs / (d ** GRAVITY_BETA + 1.0)
        w[d > MAX_COMMUTE_M] = 0
        w[i] = 0  # never commute to self
        s = w.sum()
        if s == 0:
            continue
        probs = w / s

        n_commuters = int(res[i] * EMPLOYMENT_RATE)
        n_dests     = int(min(DESTINATIONS_PER_RESIDENCE, np.sum(w > 0)))
        if n_dests == 0 or n_commuters == 0:
            continue

        # Without-replacement sample, weighted by probs.
        dest_idxs = rng.choice(len(points), size=n_dests,
                               replace=False, p=probs)

        # Allocate commuters across the chosen destinations proportionally
        # to the gravity weights of just those destinations.
        chosen_w = w[dest_idxs]
        chosen_w = chosen_w / chosen_w.sum()
        sizes    = np.maximum(1, (n_commuters * chosen_w).astype(int))

        for j, sz in zip(dest_idxs, sizes):
            flows.append((i, int(j), int(sz)))

    return flows


# ---------------------------------------------------------------------------
# OSRM routing (concurrent)
# ---------------------------------------------------------------------------

def _osrm_route(origin, dest):
    url = (f"{OSRM_BASE}/route/v1/driving/"
           f"{origin[0]},{origin[1]};{dest[0]},{dest[1]}?overview=false")
    try:
        with urllib.request.urlopen(url, timeout=OSRM_TIMEOUT) as r:
            data = json.load(r)
        if data.get("code") != "Ok":
            return None
        route = data["routes"][0]
        return int(route["distance"]), int(route["duration"])
    except (urllib.error.URLError, json.JSONDecodeError,
            KeyError, IndexError):
        return None


def route_all(points, flows):
    print(f"[6/6] Routing {len(flows):,} flows via OSRM "
          f"({OSRM_THREADS} threads)...")
    results = [None] * len(flows)

    def task(idx):
        oi, di, _ = flows[idx]
        return idx, _osrm_route(points[oi]["location"],
                                points[di]["location"])

    last_print = 0
    with ThreadPoolExecutor(max_workers=OSRM_THREADS) as pool:
        futures = [pool.submit(task, i) for i in range(len(flows))]
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result
            if idx - last_print >= 2000:
                done = sum(1 for r in results if r is not None)
                print(f"      ~{done:,}/{len(flows):,}")
                last_print = idx

    failures = sum(1 for r in results if r is None)
    print(f"      Done. {failures:,} failures (will be dropped).")
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  Melbourne base demand generator (ABS 2021 Census)")
    print("=" * 60)
    check_osrm()
    print("  OSRM reachable.")
    print()

    sa1    = load_sa1()
    sa1    = add_jobs(sa1)
    points = build_points(sa1)
    print()
    print(f"Total points:    {len(points):,}")
    print(f"Total residents: {sum(p['residents'] for p in points):,}")
    print(f"Total jobs:      {sum(p['jobs'] for p in points):,}")
    print()

    flows = gravity_commutes(points)
    print(f"Generated {len(flows):,} candidate commute flows")
    print()

    routes = route_all(points, flows)

    pops = []
    for idx, ((oi, di, sz), r) in enumerate(zip(flows, routes)):
        if r is None:
            continue
        dist, dur = r
        pop_id = f"FLOW_{idx}"
        pops.append({
            "id":              pop_id,
            "residenceId":     points[oi]["id"],
            "jobId":           points[di]["id"],
            "size":            sz,
            "drivingDistance": dist,
            "drivingSeconds":  dur,
        })
        points[oi]["popIds"].append(pop_id)
        points[di]["popIds"].append(pop_id)

    out = {"points": points, "pops": pops}
    Path(OUTPUT_FILE).write_text(json.dumps(out, indent=2))

    print()
    print("=" * 60)
    print(f"  Wrote {OUTPUT_FILE}")
    print(f"  Points: {len(points):,}")
    print(f"  Pops:   {len(pops):,}")
    print(f"  Total commute size: {sum(p['size'] for p in pops):,}")
    print("=" * 60)


if __name__ == "__main__":
    main()

"""
Build Melbourne base demand layer from ABS 2021 Census data.

Produces `demand_data_fixed.json` with:
  - One point per SA1 statistical area inside the demand-extent polygon
    (extent/mel_extent_v1_1.geojson — run make_extent.py first), with
    outer-fringe SA1s collapsed to one point per parent SA2 (~6,000
    points total). Each point has real residents (from Mesh Block counts)
    and real jobs (from SA2 Place-of-Work counts distributed to SA1).
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

BBOX = (144.25, -38.42, 145.55, -37.55)  # envelope of the extent polygon
OSRM_BASE   = "http://localhost:5000"
OUTPUT_FILE = "demand_data_fixed.json"

# Demand-extent polygon (non-rectangular). SA1s whose centroid falls
# outside it get no demand at all — the basemap still renders there, so
# Phillip Island etc. stay visible as scenery. Generated + validated by
# make_extent.py.
EXTENT_POLYGON = "extent/mel_extent_v1_1.geojson"

# Fringe aggregation: outside these radii, SA1s with few jobs merge into
# one point per parent SA2. Keeps activity centres (Dandenong South,
# Frankston CBD, East Werribee, Avalon) at full SA1 granularity while
# halving the origin-point count on the rural/outer fringe.
CBD_LONLAT              = (144.9631, -37.8136)
GEELONG_LONLAT          = (144.357, -38.147)
FRINGE_CBD_RADIUS_M     = 25_000
FRINGE_GEELONG_RADIUS_M = 12_000
FRINGE_KEEP_JOBS        = 2_000   # SA1s with >= this many jobs never merge

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
# β=2.0 gives a very steep distance decay that confines almost all commuting
# to a tight radius — outer suburbs barely commute to CBD. Real Melbourne
# commute patterns are radial-heavy (Frankston/Werribee/Geelong → CBD) and
# fit better with β≈1.3-1.5.
#
# DESTINATIONS_PER_RESIDENCE is the main pop-count lever: each origin
# spawns up to this many FLOW pops. v1.0.x used 8, which (x ~12k origins)
# produced ~96k FLOW pops — ~4x what stock cities simulate and the
# dominant in-game performance cost. v1.1.0 uses 4; consolidate_pops.py
# then merges same-origin flows toward stock pop sizes.
#
# The close-distance plateau (CLOSE_DISTANCE_FLOOR_M) is the key knob for
# stopping every residence from picking 6 neighbours and 2 anywhere-else.
# Treating all distances under this threshold as equal effectively says
# "for trips below the floor, only job-density matters, not micro-distance"
# — which mirrors how people actually choose between 4 jobs in their own
# suburb (they don't care if it's 700 m or 1.5 km, they care about pay).
DESTINATIONS_PER_RESIDENCE = 4       # Sampled commute destinations per residence
GRAVITY_BETA               = 1.4     # Distance decay exponent (above floor)
# Fraction of ABS Persons Usually Resident that has a daily structured
# commute trip (employed workers + students + commute-equivalent travel).
# Applied at point construction so `point.residents` represents commute
# origins (the Railyard schema interpretation), not total population.
# VIC LFS gives ~67% labour-force participation × ~95% employed × ~80%
# working-age share of total population ≈ 51%; add students (~10% beyond
# overlap) -> ~0.55 weekday commute participation.
EMPLOYMENT_RATE            = 0.55
MAX_COMMUTE_M              = 80_000  # No commute beyond this radius
CLOSE_DISTANCE_FLOOR_M     = 5_000   # Treat all distances < this as = this

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


def load_extent_polygon():
    """Load the demand-extent polygon written by make_extent.py."""
    from shapely.geometry import shape

    p = Path(EXTENT_POLYGON)
    if not p.exists():
        sys.exit(
            f"ERROR: {EXTENT_POLYGON} not found.\n"
            f"       Run `python make_extent.py` first — it generates and "
            f"validates the demand-extent polygon."
        )
    gj = json.loads(p.read_text(encoding="utf-8"))
    if gj.get("type") == "FeatureCollection":
        geom = gj["features"][0]["geometry"]
    elif gj.get("type") == "Feature":
        geom = gj["geometry"]
    else:
        geom = gj
    return shape(geom)


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

    print(f"[1/7] Loading mesh block boundaries from {mb_path.name}...")
    mb = gpd.read_file(mb_path, bbox=BBOX)
    print(f"      {len(mb):,} MBs in bbox")
    mb = mb.rename(columns={
        "MB_CODE21":  "MB_CODE_2021",
        "SA1_CODE21": "SA1_CODE_2021",
    })
    if mb.crs is not None and mb.crs.to_epsg() != 4326:
        mb = mb.to_crs(epsg=4326)
    mb["SA1_CODE_2021"] = mb["SA1_CODE_2021"].astype(str)

    print(f"[2/7] Dissolving to SA1 (the slow step — ~1-3 min)...")
    t0 = time.time()
    sa1 = mb.dissolve(by="SA1_CODE_2021").reset_index()
    sa1 = sa1[["SA1_CODE_2021", "geometry"]]
    print(f"      Dissolve took {time.time() - t0:.1f}s; "
          f"{len(sa1):,} SA1s")

    # Non-rectangular demand extent: keep only SA1s whose centroid falls
    # inside the polygon. Boundary SA1s are kept/dropped wholesale — the
    # centroid test is the same convention build_points uses for the
    # point location itself.
    poly = load_extent_polygon()
    n_before = len(sa1)
    sa1 = sa1[sa1.geometry.centroid.within(poly)].copy()
    print(f"      Extent polygon kept {len(sa1):,}/{n_before:,} SA1s")

    print(f"[3/7] Joining residents from {gcp_path.name}...")
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
    """Read SA2 POW counts and distribute to each SA2's constituent SA1s
    using **inverse-area weighting**.

    Activity centres (Box Hill, Glen Waverley, Footscray, Geelong CBD, etc.)
    concentrate jobs in tiny CBD-like SA1s — the station-plus-mall block —
    while the surrounding residential SA1s are physically much larger and
    contain few jobs each. Uniform-by-SA1-count distribution dilutes those
    activity-centre SA1s into the average and weakens the radial pull from
    nearby residences. Inverse-area gives the small SA1 a larger share of
    its SA2's POW total, matching real urban form.

    ABS spec: SA2_CODE_2021 = first 9 chars of SA1_CODE_2021, so the
    SA1->SA2 mapping is purely code-derived."""
    pow_path = find_file(SA2_POW_NAMES)

    print(f"[4/7] Loading SA2 POW counts from {pow_path.name}...")
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

    print(f"[5/7] Distributing SA2 jobs by inverse SA1 area "
          f"(concentrates in activity-centre SA1s)...")

    # Project to Australian Albers equal-area (EPSG:3577) for accurate
    # area-in-square-metres at Melbourne latitude.
    sa1_proj = sa1.to_crs(epsg=3577)
    sa1["area_m2"] = sa1_proj.geometry.area
    sa1["inv_area"] = 1.0 / sa1["area_m2"].clip(lower=1.0)

    # Derive parent SA2 from SA1 code and normalise inverse-area within
    # each SA2 so per-SA1 weights sum to 1.
    sa1["SA2_CODE_2021"] = sa1["SA1_CODE_2021"].astype(str).str[:9]
    sa2_total = (sa1.groupby("SA2_CODE_2021")["inv_area"]
                    .sum()
                    .rename("sa2_total_inv_area")
                    .reset_index())
    sa1 = sa1.merge(sa2_total, on="SA2_CODE_2021", how="left")
    sa1["sa2_weight"] = (sa1["inv_area"]
                         / sa1["sa2_total_inv_area"].clip(lower=1e-30))

    # Apply SA2 POW counts via the per-SA1 weight.
    sa1 = sa1.merge(
        pow_df[["SA2_CODE_2021", "jobs"]].rename(columns={"jobs": "sa2_jobs"}),
        on="SA2_CODE_2021", how="left",
    )
    sa1["sa2_jobs"] = sa1["sa2_jobs"].fillna(0).astype(int)
    sa1["jobs"] = (sa1["sa2_jobs"] * sa1["sa2_weight"]).round().astype(int)

    sa1 = sa1.drop(columns=[
        "area_m2", "inv_area", "sa2_total_inv_area", "sa2_weight", "sa2_jobs",
    ])
    print(f"      {sa1['jobs'].sum():,} jobs concentrated across "
          f"{(sa1['jobs'] > 0).sum():,} SA1s in bbox")
    return sa1


# ---------------------------------------------------------------------------
# Points + gravity model
# ---------------------------------------------------------------------------

def aggregate_fringe(sa1: gpd.GeoDataFrame) -> pd.DataFrame:
    """[6/7] Collapse outer-fringe SA1s to one point per parent SA2.

    An SA1 merges into its SA2 aggregate when it is outside BOTH the CBD
    and Geelong radii AND has fewer than FRINGE_KEEP_JOBS jobs. Job-heavy
    fringe SA1s (activity centres, Avalon Airport) survive at full
    granularity. Aggregate location = residents-weighted centroid of the
    member SA1 centroids (jobs-weighted if the group has no residents).

    Returns a plain DataFrame [point_id, lon, lat, residents, jobs] mixing
    SA1_<code> core rows with SA2_<code> aggregate rows.
    """
    cent = sa1.geometry.centroid
    df = pd.DataFrame({
        "sa1":       sa1["SA1_CODE_2021"].astype(str).to_numpy(),
        "sa2":       sa1["SA2_CODE_2021"].astype(str).to_numpy(),
        "lon":       cent.x.to_numpy(),
        "lat":       cent.y.to_numpy(),
        "residents": sa1["residents"].to_numpy(),
        "jobs":      sa1["jobs"].to_numpy(),
    })

    d_cbd = haversine_m(df["lat"].to_numpy(), df["lon"].to_numpy(),
                        CBD_LONLAT[1], CBD_LONLAT[0])
    d_glg = haversine_m(df["lat"].to_numpy(), df["lon"].to_numpy(),
                        GEELONG_LONLAT[1], GEELONG_LONLAT[0])
    fringe = ((d_cbd > FRINGE_CBD_RADIUS_M)
              & (d_glg > FRINGE_GEELONG_RADIUS_M)
              & (df["jobs"].to_numpy() < FRINGE_KEEP_JOBS))

    core = df[~fringe].copy()
    core["point_id"] = "SA1_" + core["sa1"]

    rows = []
    for sa2, g in df[fringe].groupby("sa2"):
        w = g["residents"].to_numpy(dtype=float)
        if w.sum() <= 0:
            w = g["jobs"].to_numpy(dtype=float)
        if w.sum() <= 0:
            w = np.ones(len(g))
        rows.append({
            "point_id":  f"SA2_{sa2}",
            "lon":       float(np.average(g["lon"].to_numpy(), weights=w)),
            "lat":       float(np.average(g["lat"].to_numpy(), weights=w)),
            "residents": int(g["residents"].sum()),
            "jobs":      int(g["jobs"].sum()),
        })
    cols = ["point_id", "lon", "lat", "residents", "jobs"]
    agg = pd.DataFrame(rows, columns=cols)

    print(f"[6/7] Fringe aggregation: {int(fringe.sum()):,} fringe SA1s -> "
          f"{len(agg):,} SA2 points; {len(core):,} core SA1s kept")
    return pd.concat([core[cols], agg[cols]], ignore_index=True)


def build_points(df: pd.DataFrame) -> list[dict]:
    df = df[(df["residents"] > 0) | (df["jobs"] > 0)]

    points = []
    for row in df.itertuples():
        # Apply commute-participation discount HERE so `point.residents`
        # represents commute origins (the Railyard schema interpretation),
        # not ABS Persons Usually Resident. gravity_commutes() then treats
        # res[i] as the definitive commuter count without further scaling.
        commuters = int(round(float(row.residents) * EMPLOYMENT_RATE))
        points.append({
            "id":        row.point_id,
            "location":  [round(row.lon, 6), round(row.lat, 6)],
            "residents": commuters,
            "jobs":      int(row.jobs),
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
        # Apply the close-distance plateau before the gravity decay so
        # near-neighbours don't accumulate runaway weight from low d^β.
        d_eff = np.maximum(d, CLOSE_DISTANCE_FLOOR_M)
        with np.errstate(invalid="ignore", divide="ignore"):
            w = jobs / (d_eff ** GRAVITY_BETA)
        w[d > MAX_COMMUTE_M] = 0
        w[i] = 0  # never commute to self
        s = w.sum()
        if s == 0:
            continue
        probs = w / s

        # point.residents already represents commute origins (discount
        # applied in build_points) — use it directly.
        n_commuters = int(res[i])
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
    print(f"[7/7] Routing {len(flows):,} flows via OSRM "
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
    pts_df = aggregate_fringe(sa1)
    points = build_points(pts_df)
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

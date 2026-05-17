"""
Melbourne demand pipeline for Subway Builder Modded.

Three steps:
  1. Inject Victorian government + non-government schools into the config
     (universities[]/univ_loc[]/students[]/...) as demand points.
  2. Start an OSRM Docker container against melbourne.osrm.
  3. Run the public demand-adder executable
     (https://github.com/rslurry/Demand-Adder/releases) which writes the
     final demand_data.json the game consumes.

Adapted from WillBarouch/subwaybuilder-sydney/demand_generator.py.
Differences:
  - OSRM container name + default bbox + script title swapped Sydney → Melbourne.
  - School data source defaults to the Victorian "All Government Schools"
    open dataset (data.vic.gov.au). Column names map to the ACARA schema,
    which both NSW and VIC publish, so the loader works once you point it
    at the right XLSX files.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Victorian Department of Education publishes these annually at
# https://discover.data.vic.gov.au/dataset/all-schools-list-and-school-locations
# Filenames change year to year — adjust here when refreshing.
#
# dv403 = enrolments (joined on School_No to dv402).
# dv402 = locations  (X = longitude, Y = latitude).
# The "All Schools" datasets cover Government, Catholic, and Independent.
PROFILE_CSV     = "dv403-AllSchoolsEnrolments-2025.csv"
LOCATION_CSV    = "dv402-SchoolLocations2025.csv"

EXE_NAME        = "create_new_demand_points_windows_x86-64.exe"
OSRM_PORT       = 5000
OSRM_IMAGE      = "ghcr.io/project-osrm/osrm-backend"
OSRM_CONTAINER  = "osrm_melbourne"

INCLUDE_TYPES   = {"primary", "secondary", "combined", "pri/sec", "special"}
TRAVEL_FRACTION = 0.9
MIN_ENROLMENT   = 50

BBOX_DEFAULT    = [144.25, -38.55, 145.65, -37.55]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pop_size_for_enrolment(n: int) -> int:
    if n >= 1500: return 200
    if n >= 800:  return 150
    if n >= 400:  return 100
    if n >= 150:  return 75
    return 50


def in_bbox(lat: float, lon: float, bbox: list) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def make_code(name: str, index: int) -> str:
    skip = {
        "THE", "OF", "AND", "ST", "PUBLIC", "SCHOOL", "HIGH", "COLLEGE",
        "CATHOLIC", "PRIMARY", "COMMUNITY", "CHRISTIAN", "ANGLICAN",
        "GRAMMAR", "CENTRAL", "WEST", "EAST", "NORTH", "SOUTH",
        "SECONDARY", "P-12", "P12"
    }
    words = name.upper().replace("-", " ").split()
    letters = [w[0] for w in words if w not in skip and w.isalpha()]
    code = "".join(letters[:5]) or "SCH"
    return f"{code}{index}"


def _read_vic_csv(path: Path) -> tuple[list[dict], list[str]]:
    """Read a DataVic CSV. The enrolments file wraps year-total column
    headers in literal quote characters (e.g. `"Grand Total"`) on top of
    the normal CSV quoting, which Python's csv module then surfaces as
    e.g. `'"Grand Total"'`. Strip those so we can index by clean names."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        raw_headers = next(reader)
        headers = [h.strip().strip('"') for h in raw_headers]
        rows = [dict(zip(headers, r)) for r in reader if r]
    return rows, headers


def load_schools(script_dir: Path, bbox: list, min_enrolment: int) -> list:
    """Load Victorian schools from the DataVic open datasets and filter to bbox.

    Joins dv403 (enrolments) and dv402 (locations) on `School_No`. Both
    files cover Government, Catholic and Independent schools, so this is
    a single source for the whole sector.

    Expected columns (after CSV unquoting):
      dv403: School_No, School_Name, School_Type, School_Status, Grand Total
      dv402: School_No, X (longitude), Y (latitude)
    """
    profile_path  = script_dir / PROFILE_CSV
    location_path = script_dir / LOCATION_CSV

    for p in {profile_path, location_path}:
        if not p.exists():
            print(f"  ERROR: Cannot find required file: {p.name}")
            print(f"         Expected location: {p}")
            print(f"         Download VIC school data from "
                  f"https://discover.data.vic.gov.au/dataset/all-schools-list-and-school-locations")
            sys.exit(1)

    print(f"  Reading {PROFILE_CSV}...")
    profile_rows, profile_headers = _read_vic_csv(profile_path)
    for col in ["School_No", "School_Name", "School_Type",
                "School_Status", "Grand Total"]:
        if col not in profile_headers:
            print(f"  ERROR: Expected column '{col}' not found in {PROFILE_CSV}")
            print(f"         Found: {profile_headers}")
            sys.exit(1)

    profiles = {}
    for row in profile_rows:
        sid = row["School_No"].strip()
        if not sid:
            continue
        # Only currently-operating schools.
        if row["School_Status"].strip().upper() != "O":
            continue
        stype = row["School_Type"].strip().lower()
        if stype not in INCLUDE_TYPES:
            continue
        # Grand Total may be a decimal FTE (e.g. "32.4") or empty/np.
        try:
            enrolment = int(float(row["Grand Total"].replace(",", "")))
        except (ValueError, AttributeError):
            enrolment = 0
        if enrolment < min_enrolment:
            continue
        profiles[sid] = {
            "name":      row["School_Name"].strip(),
            "type":      stype,
            "enrolment": enrolment,
        }
    print(f"  Loaded {len(profiles)} open primary/secondary/combined schools "
          f"(enrolment >= {min_enrolment}).")

    print(f"  Reading {LOCATION_CSV}...")
    location_rows, location_headers = _read_vic_csv(location_path)
    for col in ["School_No", "X", "Y"]:
        if col not in location_headers:
            print(f"  ERROR: Expected column '{col}' not found in {LOCATION_CSV}")
            print(f"         Found: {location_headers}")
            sys.exit(1)

    locations = {}
    for row in location_rows:
        sid = row["School_No"].strip()
        if not sid:
            continue
        try:
            lon = float(row["X"])
            lat = float(row["Y"])
            locations[sid] = {"lat": lat, "lon": lon}
        except (TypeError, ValueError):
            pass
    print(f"  Loaded {len(locations)} school locations.")

    matched = []
    for sid, profile in profiles.items():
        if sid not in locations:
            continue
        loc = locations[sid]
        if not in_bbox(loc["lat"], loc["lon"], bbox):
            continue
        matched.append({**profile, "lat": loc["lat"], "lon": loc["lon"]})

    matched.sort(key=lambda s: s["enrolment"], reverse=True)

    seen = set()
    for i, school in enumerate(matched):
        code = make_code(school["name"], i)
        base, suffix = code, 0
        while code in seen:
            suffix += 1
            code = f"{base}{suffix}"
        seen.add(code)
        school["code"] = code

    types = {}
    for s in matched:
        types[s["type"]] = types.get(s["type"], 0) + 1
    print(f"\n  Schools within bbox: {len(matched)}")
    for t, c in sorted(types.items()):
        print(f"    {t.capitalize()}: {c}")
    total_students = sum(s["enrolment"] for s in matched)
    print(f"  Total enrolments:          {total_students:,}")
    print(f"  Est. daily transit demand: {int(total_students * TRAVEL_FRACTION):,}")

    return matched


def inject_schools(config: dict, schools: list) -> dict:
    def extend(key, values):
        config[key] = config.get(key, []) + values

    extend("universities",      [s["code"] for s in schools])
    extend("univ_loc",          [[s["lon"], s["lat"]] for s in schools])
    extend("univ_merge_within", [0] * len(schools))
    extend("students",          [s["enrolment"] for s in schools])
    extend("perc_oncampus",     [0.0] * len(schools))
    extend("univ_pop_size",     [pop_size_for_enrolment(s["enrolment"]) for s in schools])
    config["univ_perc_travel"] = [TRAVEL_FRACTION, TRAVEL_FRACTION]
    return config


def osrm_healthy() -> bool:
    try:
        # Probe = Flinders Street Station.
        url = f"http://localhost:{OSRM_PORT}/nearest/v1/driving/144.9671,-37.8183"
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code in (200, 400)
    except Exception:
        return False


def start_osrm(osrm_arg: str, script_dir: Path):
    if osrm_healthy():
        print("  OSRM already running on port 5000 — skipping Docker start.")
        return

    osrm = Path(osrm_arg)
    if not osrm.is_absolute():
        osrm = script_dir / osrm

    def osrm_sidecars_exist(p: Path) -> bool:
        return any(p.parent.glob(p.name + ".*"))

    if osrm.suffix == ".pbf":
        osrm = osrm.with_suffix(".osrm")

    if not osrm.exists() and not osrm_sidecars_exist(osrm):
        candidates = list(osrm.parent.glob("*.osrm.*"))
        if candidates:
            osrm = Path(str(candidates[0]).split(".osrm.")[0] + ".osrm")
            print(f"  Auto-detected OSRM data: {osrm.name}")
        else:
            print(f"  ERROR: Cannot find OSRM data file: {osrm}")
            print("         Run the three Docker pre-processing commands first:")
            print(f"           docker run -t -v \"${{PWD}}:/data\" {OSRM_IMAGE} osrm-extract -p /opt/car.lua /data/melbourne.pbf")
            print(f"           docker run -t -v \"${{PWD}}:/data\" {OSRM_IMAGE} osrm-partition /data/melbourne.osrm")
            print(f"           docker run -t -v \"${{PWD}}:/data\" {OSRM_IMAGE} osrm-customize /data/melbourne.osrm")
            sys.exit(1)

    osrm_dir  = str(osrm.parent).replace("\\", "/")
    osrm_file = osrm.name

    subprocess.run(["docker", "rm", "-f", OSRM_CONTAINER], capture_output=True)

    cmd = [
        "docker", "run", "-d",
        "--name", OSRM_CONTAINER,
        "-p", f"{OSRM_PORT}:{OSRM_PORT}",
        "-v", f"{osrm_dir}:/data",
        OSRM_IMAGE,
        "osrm-routed",
        "--algorithm", "mld",
        f"/data/{osrm_file}"
    ]

    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("  ERROR: Docker failed to start OSRM:")
        print(result.stderr)
        sys.exit(1)

    print("  Waiting for OSRM to be ready", end="", flush=True)
    for _ in range(90):
        time.sleep(1)
        print(".", end="", flush=True)
        if osrm_healthy():
            print(" ready!")
            return

    print()
    print("  ERROR: OSRM did not become healthy within 90 seconds.")
    print(f"         Check logs with: docker logs {OSRM_CONTAINER}")
    sys.exit(1)


def run_demand_adder(config_path: Path, script_dir: Path):
    exe = script_dir / EXE_NAME
    if not exe.exists():
        print(f"  ERROR: Cannot find {EXE_NAME}")
        print(f"         Expected location: {exe}")
        print("         Download it from https://github.com/rslurry/Demand-Adder/releases")
        sys.exit(1)

    # Current Demand-Adder builds ignore argv and prompt interactively for
    # the config path. Use a relative filename so it works regardless of
    # whether the cwd is interpreted as a WSL or Windows path.
    cmd = [str(exe)]
    config_input = config_path.name + "\n"
    print(f"  Running: {exe} (config via stdin: {config_path.name})")
    result = subprocess.run(cmd, cwd=str(script_dir),
                            input=config_input, text=True)

    if result.returncode != 0:
        print(f"\n  ERROR: Demand adder exited with code {result.returncode}")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="Melbourne demand pipeline: schools + OSRM + demand adder."
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to your config JSON (e.g. melbourne.json)"
    )
    parser.add_argument(
        "--pbf", required=True,
        help="Path to your processed OSRM data file (e.g. melbourne.osrm)"
    )
    parser.add_argument(
        "--min-enrolment", type=int, default=MIN_ENROLMENT,
        help=f"Minimum school enrolment to include (default: {MIN_ENROLMENT})"
    )
    parser.add_argument(
        "--skip-schools", action="store_true",
        help="Skip school injection step"
    )
    parser.add_argument(
        "--skip-osrm", action="store_true",
        help="Skip OSRM Docker startup step"
    )
    args = parser.parse_args()

    script_dir  = Path(__file__).parent.resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = script_dir / config_path

    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    print("=" * 50)
    print("  Melbourne Demand Pipeline")
    print("=" * 50)
    print(f"  Config:     {config_path.name}")
    print(f"  OSRM file:  {args.pbf}")
    print(f"  Script dir: {script_dir}")
    print()

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    bbox = config.get("bbox") or BBOX_DEFAULT

    if not args.skip_schools:
        print("[1/3] Injecting schools into config...")
        schools = load_schools(script_dir, bbox, args.min_enrolment)
        config  = inject_schools(config, schools)

        out_config = config_path.with_stem(config_path.stem + "_with_schools")
        with open(out_config, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print(f"\n  Saved updated config to: {out_config.name}")
        config_path = out_config
    else:
        print("[1/3] Skipping school injection.")

    if not args.skip_osrm:
        print("\n[2/3] Starting OSRM via Docker...")
        start_osrm(args.pbf, script_dir)
    else:
        print("[2/3] Skipping OSRM startup.")

    print("\n[3/3] Running demand adder...")
    run_demand_adder(config_path, script_dir)

    print("\n" + "=" * 50)
    print("  Pipeline complete!")
    print(f"  Output: {config.get('output_demand_file', 'see config')}")
    print("=" * 50)


if __name__ == "__main__":
    main()

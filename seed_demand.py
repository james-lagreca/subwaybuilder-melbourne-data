"""
Generate a minimal `demand_data_fixed.json` seed for the Melbourne pipeline.

Demand-Adder modifies an existing demand file rather than creating one from
scratch — it samples K residence points per attraction/school commute, and
fails (NaN-divide) if the pool is empty or only contains 1 point.

This script writes 8 hand-placed seed bubbles at major metro centres with
realistic-ish resident/job counts, self-loop commutes, and to-CBD flows from
each suburb. Total ~830k residents — light compared to real Melbourne but
plenty to keep Demand-Adder's sampling math happy.

Replace this with a proper ABS Mesh Block 2021 + Destination Zone generator
when you want city-shaped residential demand. For v0.1.0 release, this seed
plus Demand-Adder's added schools/attractions/bases is enough for a playable
mod.

Run:
    python seed_demand.py
"""

import json
from pathlib import Path

# (id, lon, lat, residents, jobs)
#
# Demand-Adder zeroes out weights for any existing point >40 km away from
# each school it's processing. Need enough seeds for every corner of the
# bbox (Sorrento, Phillip Island, Pakenham, NW outskirts) to fall within
# 40 km of at least one. Otherwise the gravity-weight array goes all-zero
# and the random.choice probability normalisation hits divide-by-zero NaN.
POINTS = [
    ("CBD",     144.9631, -37.8136,   50_000, 250_000),
    ("FOOT",    144.8975, -37.7997,  100_000,  30_000),
    ("BRUNS",   144.9580, -37.7670,   80_000,  20_000),
    ("BOXH",    145.1232, -37.8195,  120_000,  40_000),
    ("DAND",    145.2143, -37.9870,  150_000,  50_000),
    ("FRANK",   145.1370, -38.1416,  130_000,  40_000),
    ("WERR",    144.6589, -37.9078,  100_000,  25_000),
    ("GEEL",    144.3617, -38.1499,  100_000,  60_000),
    # Coverage seeds for the corners of the bbox so every school is
    # within 40 km of at least one workplace point.
    ("SUNB",    144.7290, -37.5828,   50_000,  12_000),  # NW outskirts
    ("PAKE",    145.4830, -38.0791,   60_000,  15_000),  # far east
    ("SORR",    144.7430, -38.3380,   20_000,   6_000),  # Mornington Peninsula tip
    ("COWES",   145.2400, -38.4480,   10_000,   5_000),  # Phillip Island
]

# Self-loop commute size as fraction of residents that work locally.
SELF_LOOP_FRAC = 0.30
# Fraction of residents in each suburb that commute to CBD.
CBD_COMMUTE_FRAC = 0.20

# Rough driving distance (m) and time (s) from each suburb to CBD.
# Approximate — Demand-Adder uses OSRM to compute its own routes for the new
# attractions, so these only need to be reasonable for the seed flows.
TO_CBD = {
    "FOOT":  ( 7_000,   900),
    "BRUNS": ( 7_000,   900),
    "BOXH":  (14_000, 1_500),
    "DAND":  (33_000, 2_400),
    "FRANK": (42_000, 3_300),
    "WERR":  (30_000, 2_100),
    "GEEL":  (75_000, 4_500),
    "SUNB":  (40_000, 2_700),
    "PAKE":  (55_000, 3_900),
    "SORR":  (90_000, 5_400),
    "COWES": (130_000, 6_900),
}


def main() -> None:
    points = []
    pops = []

    # Build the points + collect popIds.
    pop_ids_by_point = {pid: [] for pid, *_ in POINTS}

    for pid, lon, lat, residents, jobs in POINTS:
        # Self-loop (local commute within suburb).
        self_pop_id = f"{pid}-{pid}"
        pops.append({
            "id": self_pop_id,
            "residenceId": pid,
            "jobId": pid,
            "size": int(residents * SELF_LOOP_FRAC),
            "drivingDistance": 1_500,
            "drivingSeconds": 300,
        })
        pop_ids_by_point[pid].append(self_pop_id)

        # To-CBD flow for non-CBD suburbs.
        if pid != "CBD" and pid in TO_CBD:
            dist, secs = TO_CBD[pid]
            to_cbd_id = f"{pid}-CBD"
            pops.append({
                "id": to_cbd_id,
                "residenceId": pid,
                "jobId": "CBD",
                "size": int(residents * CBD_COMMUTE_FRAC),
                "drivingDistance": dist,
                "drivingSeconds": secs,
            })
            pop_ids_by_point[pid].append(to_cbd_id)
            pop_ids_by_point["CBD"].append(to_cbd_id)

    for pid, lon, lat, residents, jobs in POINTS:
        points.append({
            "id": pid,
            "location": [lon, lat],
            "residents": residents,
            "jobs": jobs,
            "popIds": pop_ids_by_point[pid],
        })

    out = {"points": points, "pops": pops}
    Path("demand_data_fixed.json").write_text(json.dumps(out, indent=2))

    print(f"Wrote demand_data_fixed.json")
    print(f"  Points: {len(points)}")
    print(f"  Pops:   {len(pops)}")
    print(f"  Total residents: {sum(p['residents'] for p in points):,}")
    print(f"  Total jobs:      {sum(p['jobs'] for p in points):,}")
    print(f"  Total flow size: {sum(p['size'] for p in pops):,}")


if __name__ == "__main__":
    main()

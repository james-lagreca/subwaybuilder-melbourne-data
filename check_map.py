"""
check_map.py — Local validation of demand_data.json against the game's
demand contract (mirrors DemandDataFileSchema in the mod template's
src/types/schemas.d.ts, which the game enforces with Zod on city load)
plus the Railyard registry rules and the v1.1 extent polygon.

Checks:
  - exact key sets on every point and pop
  - popIds referential integrity in BOTH directions (every popId on a
    point belongs to a pop that references that point; every pop id is
    listed on its residence and job points — twice when they coincide)
  - sum(point.residents) == sum(pop.size)  (registry validator rule)
  - drivingSeconds/drivingDistance are ints, >= 0 or the -1 sentinel
  - every point location inside the demand-extent polygon
  - summary table: counts, sizes, file size vs v1.0.3 baseline

Exit code 0 = all checks pass.

    python check_map.py [demand_data.json]
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from shapely.geometry import Point, shape

EXTENT_POLYGON = Path("extent/mel_extent_v1_1.geojson")

POINT_KEYS = {"id", "location", "jobs", "residents", "popIds"}
POP_KEYS   = {"id", "residenceId", "jobId", "size",
              "drivingSeconds", "drivingDistance"}
# drivingPath is optional in the schema; nothing in this pipeline emits it.
POP_KEYS_OPTIONAL = {"drivingPath"}

# v1.0.3 baseline, for the comparison table.
BASELINE = {"points": 13_404, "pops": 107_413, "mb": 17.4}


def fail(errors: list[str], msg: str) -> None:
    errors.append(msg)
    if len(errors) <= 20:
        print(f"  FAIL: {msg}")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demand_data.json")
    if not path.exists():
        sys.exit(f"ERROR: {path} not found")
    if not EXTENT_POLYGON.exists():
        sys.exit(f"ERROR: {EXTENT_POLYGON} not found — run make_extent.py")

    poly_gj = json.loads(EXTENT_POLYGON.read_text(encoding="utf-8"))
    poly = shape(poly_gj["geometry"] if poly_gj.get("type") == "Feature"
                 else poly_gj)

    print(f"Checking {path} ({path.stat().st_size / 1e6:.2f} MB)...")
    data = json.loads(path.read_text(encoding="utf-8"))
    points, pops = data["points"], data["pops"]
    errors: list[str] = []

    # --- structural checks -------------------------------------------------
    point_ids = set()
    for pt in points:
        if set(pt.keys()) != POINT_KEYS:
            fail(errors, f"point {pt.get('id')} keys {sorted(pt.keys())}")
        if pt["id"] in point_ids:
            fail(errors, f"duplicate point id {pt['id']}")
        point_ids.add(pt["id"])
        loc = pt["location"]
        if (not isinstance(loc, list) or len(loc) != 2
                or not all(isinstance(c, (int, float)) for c in loc)):
            fail(errors, f"point {pt['id']} bad location {loc}")
        if not isinstance(pt["residents"], int) or pt["residents"] < 0:
            fail(errors, f"point {pt['id']} bad residents {pt['residents']}")
        if not isinstance(pt["jobs"], int) or pt["jobs"] < 0:
            fail(errors, f"point {pt['id']} bad jobs {pt['jobs']}")

    pop_ids = set()
    for p in pops:
        keys = set(p.keys())
        if not POP_KEYS <= keys or keys - POP_KEYS - POP_KEYS_OPTIONAL:
            fail(errors, f"pop {p.get('id')} keys {sorted(keys)}")
        if p["id"] in pop_ids:
            fail(errors, f"duplicate pop id {p['id']}")
        pop_ids.add(p["id"])
        if not isinstance(p["size"], int) or p["size"] < 0:
            fail(errors, f"pop {p['id']} bad size {p['size']}")
        for k in ("drivingSeconds", "drivingDistance"):
            v = p[k]
            if not isinstance(v, int) or (v < 0 and v != -1):
                fail(errors, f"pop {p['id']} bad {k} {v}")
        for k in ("residenceId", "jobId"):
            if p[k] not in point_ids:
                fail(errors, f"pop {p['id']} references unknown point {p[k]}")

    # --- popIds referential integrity, both directions ---------------------
    expected: dict[str, Counter] = defaultdict(Counter)
    for p in pops:
        expected[p["residenceId"]][p["id"]] += 1
        expected[p["jobId"]][p["id"]] += 1
    mismatches = 0
    for pt in points:
        if Counter(pt["popIds"]) != expected.get(pt["id"], Counter()):
            mismatches += 1
            if mismatches <= 5:
                fail(errors, f"point {pt['id']} popIds mismatch")
    if mismatches > 5:
        fail(errors, f"...{mismatches} points with popIds mismatch total")

    # --- totals (registry validator rule) ----------------------------------
    res_sum = sum(pt["residents"] for pt in points)
    size_sum = sum(p["size"] for p in pops)
    if res_sum != size_sum:
        fail(errors, f"sum(residents) {res_sum:,} != sum(size) {size_sum:,}")

    # --- extent ------------------------------------------------------------
    outside = [pt["id"] for pt in points
               if not poly.contains(Point(pt["location"]))]
    if outside:
        fail(errors, f"{len(outside)} points outside extent polygon "
                     f"(first: {outside[:3]})")

    # --- summary -----------------------------------------------------------
    sizes = [p["size"] for p in pops]
    prefix = Counter(p["id"].split("_")[0] for p in pops)
    mb = path.stat().st_size / 1e6
    print()
    print(f"  {'':14}{'v1.0.3':>10}  {'now':>10}")
    print(f"  {'points':14}{BASELINE['points']:>10,}  {len(points):>10,}")
    print(f"  {'pops':14}{BASELINE['pops']:>10,}  {len(pops):>10,}")
    print(f"  {'file MB':14}{BASELINE['mb']:>10.1f}  {mb:>10.2f}")
    print(f"  pop size: mean {statistics.mean(sizes):.1f}, "
          f"median {statistics.median(sizes):.0f}, max {max(sizes)}, "
          f"sub-3 count {sum(1 for s in sizes if s < 3):,}")
    print(f"  by prefix: {dict(prefix.most_common())}")
    print(f"  residents == pop sizes == {res_sum:,}")
    print()
    if errors:
        print(f"FAILED with {len(errors)} error(s).")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()

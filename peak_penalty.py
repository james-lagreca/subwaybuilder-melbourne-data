"""
peak_penalty.py — Inflate drivingSeconds on every pop to model peak-hour
congestion. OSRM's car profile gives free-flow times; real Melbourne AM/PM
peak adds significant time to all road trips, more so on long radial trips
where freeway congestion stacks. Driving competitiveness vs transit is what
the in-game mode-choice cares about, so penalising driving here is what
shifts pops onto your lines.

Distance-graduated multiplier (one-way trip):

  <  5 km local       ×1.10   suburb-to-suburb, light congestion
  5-20 km urban       ×1.30   urban arterials, moderate congestion
  20-50 km radial     ×1.50   Monash/Tullamarine/M1 freeway congestion
  >  50 km long-haul  ×1.60   Geelong/Pakenham-CBD type trips

Run AFTER demand_generator.py produces demand_data.json (its OSRM-routed
output). In-place rewrite. Safe to re-run — uses drivingDistance to derive
the multiplier so repeated runs compound. Don't run it twice on the same
file (or compound penalties stack). If unsure, regenerate from demand-adder.
"""

import json
import sys
from pathlib import Path

INPUT = Path("demand_data.json")

# (distance threshold in metres, multiplier)
TIERS = [
    (    5_000, 1.10),
    (   20_000, 1.30),
    (   50_000, 1.50),
    (1_000_000, 1.60),
]


def multiplier_for(distance_m: int) -> float:
    for threshold, mult in TIERS:
        if distance_m <= threshold:
            return mult
    return TIERS[-1][1]


def main() -> None:
    if not INPUT.exists():
        sys.exit(f"ERROR: {INPUT} not found. Run demand_generator.py first "
                 f"and `mv demand_data_out.json demand_data.json`.")

    print(f"Loading {INPUT}...")
    data = json.loads(INPUT.read_text())
    pops = data.get("pops", [])
    if not pops:
        sys.exit("ERROR: no pops in demand_data.json")

    tier_counts = {m: 0 for _, m in TIERS}
    total_old = 0
    total_new = 0
    skipped   = 0

    for p in pops:
        old = int(p.get("drivingSeconds", -1))
        dist = int(p.get("drivingDistance", -1))
        if old < 0 or dist < 0:
            # Demand-Adder writes -1 for unrouted pops (e.g. on-campus
            # university residents going off-campus where it doesn't bother
            # routing). Leave those untouched.
            skipped += 1
            continue
        mult = multiplier_for(dist)
        new = int(old * mult)
        p["drivingSeconds"] = new
        tier_counts[mult] += 1
        total_old += old
        total_new += new

    INPUT.write_text(json.dumps(data, separators=(",", ":")))

    print(f"  Pops total:         {len(pops):,}")
    print(f"  Pops penalised:     {len(pops) - skipped:,}")
    print(f"  Pops skipped (-1):  {skipped:,}")
    for thresh, mult in TIERS:
        label = (
            f"≤  {thresh//1000:>3} km"
            if thresh < 1_000_000 else "> 50 km"
        )
        print(f"    {label}  ×{mult}  →  {tier_counts[mult]:,} pops")
    if total_old:
        delta = (total_new / total_old - 1) * 100
        print(f"  Mean penalised trip:  "
              f"{total_old / max(1, len(pops) - skipped):.0f}s "
              f"→ {total_new / max(1, len(pops) - skipped):.0f}s  "
              f"(+{delta:.1f}% in aggregate driving time)")
    print(f"  Wrote {INPUT}")


if __name__ == "__main__":
    main()

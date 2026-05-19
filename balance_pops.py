"""
balance_pops.py — Align total pop volume with total residents.

The Railyard registry validator requires sum(point.residents) to exactly
match sum(pop.size). The base demand pipeline's gravity model loses a
small amount (typically <1%) to int truncation when distributing each
SA1's commute volume across its sampled destinations. Demand-Adder's
own rounding adds a few more units to the delta.

This script closes the gap exactly by adjusting individual pop sizes:
- If pops sum is below residents, add +1 to the largest pops (one per
  unit of deficit) — preserves the relative size distribution.
- If pops sum is above residents (rare), decrement the largest pops by
  -1 each.

Run AFTER peak_penalty.py and before zipping for release. In-place
rewrite of demand_data.json. Idempotent — safe to re-run.
"""

import json
import sys
from pathlib import Path

INPUT = Path("demand_data.json")


def main() -> None:
    if not INPUT.exists():
        sys.exit(f"ERROR: {INPUT} not found. Run the demand pipeline first.")

    data = json.loads(INPUT.read_text())
    points = data["points"]
    pops = data["pops"]

    res_sum = sum(int(p.get("residents", 0)) for p in points)
    pop_sum = sum(int(p.get("size", 0)) for p in pops)
    delta = res_sum - pop_sum

    print(f"  Before: residents={res_sum:,}, pops={pop_sum:,}, "
          f"delta={delta:+,} ({100 * delta / max(res_sum, 1):+.3f}%)")

    if delta == 0:
        print("  Already balanced — nothing to do.")
        return

    # Largest-remainder-style adjustment: bias the +/-1 adjustments toward
    # the biggest pops so the relative distribution shape barely changes.
    sorted_idxs = sorted(range(len(pops)), key=lambda i: -pops[i]["size"])

    if delta > 0:
        # Deficit — add +1 to the top `delta` pops. If delta > len(pops)
        # we wrap to add multiple +1s to the biggest pops (rare).
        for k in range(delta):
            i = sorted_idxs[k % len(pops)]
            pops[i]["size"] = int(pops[i]["size"]) + 1
    else:
        # Surplus — take -1 from the biggest pops, never below 1.
        remaining = -delta
        idx = 0
        guard = len(pops) * 100
        while remaining > 0 and idx < guard:
            pop = pops[sorted_idxs[idx % len(pops)]]
            if int(pop["size"]) > 1:
                pop["size"] = int(pop["size"]) - 1
                remaining -= 1
            idx += 1
        if remaining > 0:
            print(f"  WARNING: surplus still {remaining} after {idx} passes "
                  f"(all remaining pops are size 1)")

    # Verify
    new_pop_sum = sum(int(p["size"]) for p in pops)
    new_delta = res_sum - new_pop_sum
    print(f"  After:  residents={res_sum:,}, pops={new_pop_sum:,}, "
          f"delta={new_delta:+,}")

    INPUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"  Wrote {INPUT}")


if __name__ == "__main__":
    main()

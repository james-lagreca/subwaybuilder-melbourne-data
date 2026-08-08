"""
consolidate_pops.py — Merge fine-grained FLOW pops toward stock-city sizes.

The game simulates every pop as a full commuter object (mode choice,
transit paths, departure timing), so pop COUNT — not resident count — is
the dominant in-play performance cost. Stock cities run 6k-28k pops
filled to ~200 people each; v1.0.3 Melbourne shipped 107k pops with a
median size of 14. This script closes that gap without changing totals.

Reads  demand_data_out.json          (Demand-Adder output)
Writes demand_data_consolidated.json (input to peak_penalty.py)

Only FLOW_ pops whose residenceId is an SA1_/SA2_ point are merged.
UNI_/ENT_/AIR_/base pops are already chunked at stock sizes by
Demand-Adder, and the handful of FLOW pops it rewrites onto UNI_ points
(on-campus students) pass through untouched.

Pass A — same-origin, same-destination-SA3 merge:
    Group by residenceId, bucket by the destination's parent SA3 district
    (first 5 chars of the SA code; exact jobId for non-SA destinations).
    Merge each bucket greedily while the merged size stays <= SOFT_CAP.
    size sums exactly; drivingSeconds / drivingDistance become
    size-weighted means; id and jobId come from the largest constituent.
    SA3 (district) rather than SA2 (suburb) because each origin samples
    only 4 gravity destinations — at SA2 grain they almost never collide
    and nothing merges.

Pass B — dust fold:
    Any remaining merge-eligible pop with size < DUST_SIZE folds into the
    largest surviving same-origin pop regardless of destination. Kills
    the tiny pops (23% of v1.0.3 pops were <= 2 people!) that each cost
    a full simulated commuter while representing almost nobody.

Then every point's popIds is rebuilt from scratch from the surviving
pops (one append per residenceId + one per jobId, duplicated when both
are the same point — matching the Demand-Adder convention), so
referential integrity holds by construction.

Total pop size is asserted identical before/after. Pure transform —
safe to re-run any time.
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

INPUT  = Path("demand_data_out.json")
OUTPUT = Path("demand_data_consolidated.json")

SOFT_CAP  = 250   # Pass A stops growing a merged pop beyond this
DUST_SIZE = 10    # Pass B folds pops smaller than this


def is_mergeable(pop: dict) -> bool:
    """FLOW pops with an SA residence and routed (non-negative) driving
    values. Everything else passes through byte-for-byte."""
    return (pop["id"].startswith("FLOW_")
            and pop["residenceId"][:4] in ("SA1_", "SA2_")
            and int(pop.get("drivingSeconds", -1)) >= 0
            and int(pop.get("drivingDistance", -1)) >= 0)


def dest_bucket(job_id: str) -> str:
    """Parent SA3 district for SA-type destinations; exact id otherwise.
    ABS spec: SA1 codes nest hierarchically — first 5 chars = SA3."""
    if job_id.startswith("SA1_") or job_id.startswith("SA2_"):
        return "SA3_" + job_id[4:][:5]
    return job_id


def merge_group(group: list[dict]) -> dict:
    """Merge pops into one: exact size sum, size-weighted driving means,
    id/jobId from the largest constituent."""
    total = sum(int(p["size"]) for p in group)
    largest = max(group, key=lambda p: int(p["size"]))
    if total <= 0:
        # All-zero sizes (Demand-Adder can emit size 0): keep largest's
        # driving values, no weighting possible.
        secs, dist = int(largest["drivingSeconds"]), int(largest["drivingDistance"])
    else:
        secs = round(sum(int(p["size"]) * int(p["drivingSeconds"]) for p in group) / total)
        dist = round(sum(int(p["size"]) * int(p["drivingDistance"]) for p in group) / total)
    return {
        "id":              largest["id"],
        "residenceId":     largest["residenceId"],
        "jobId":           largest["jobId"],
        "size":            total,
        "drivingDistance": int(dist),
        "drivingSeconds":  int(secs),
    }


def pass_a(pops: list[dict]) -> list[dict]:
    """Same-origin destination-SA2 merge with the soft cap."""
    by_origin_dest: dict[tuple, list[dict]] = defaultdict(list)
    for p in pops:
        by_origin_dest[(p["residenceId"], dest_bucket(p["jobId"]))].append(p)

    merged = []
    for group in by_origin_dest.values():
        group.sort(key=lambda p: -int(p["size"]))
        batch: list[dict] = []
        batch_size = 0
        for p in group:
            sz = int(p["size"])
            if batch and batch_size + sz > SOFT_CAP:
                merged.append(merge_group(batch))
                batch, batch_size = [], 0
            batch.append(p)
            batch_size += sz
        if batch:
            merged.append(merge_group(batch))
    return merged


def pass_b(pops: list[dict]) -> list[dict]:
    """Fold sub-DUST_SIZE pops into the largest surviving same-origin pop."""
    by_origin: dict[str, list[dict]] = defaultdict(list)
    for p in pops:
        by_origin[p["residenceId"]].append(p)

    out = []
    for group in by_origin.values():
        group.sort(key=lambda p: -int(p["size"]))
        survivor = group[0]           # largest always survives
        dust = [p for p in group[1:] if int(p["size"]) < DUST_SIZE]
        keep = [p for p in group[1:] if int(p["size"]) >= DUST_SIZE]
        if dust:
            survivor = merge_group([survivor] + dust)
        out.append(survivor)
        out.extend(keep)
    return out


def size_stats(pops: list[dict]) -> str:
    sizes = [int(p["size"]) for p in pops] or [0]
    return (f"n={len(pops):,}  sum={sum(sizes):,}  "
            f"mean={statistics.mean(sizes):.1f}  "
            f"median={statistics.median(sizes):.0f}  max={max(sizes)}")


def prefix_counts(pops: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for p in pops:
        counts[p["id"].split("_")[0]] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def main() -> None:
    if not INPUT.exists():
        sys.exit(f"ERROR: {INPUT} not found. Run demand_generator.py first.")

    print(f"Loading {INPUT}...")
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    points, pops = data["points"], data["pops"]

    mergeable   = [p for p in pops if is_mergeable(p)]
    passthrough = [p for p in pops if not is_mergeable(p)]
    total_before = sum(int(p["size"]) for p in pops)

    print(f"  Before: {size_stats(pops)}")
    print(f"  Mergeable FLOW pops: {len(mergeable):,}; "
          f"passthrough: {len(passthrough):,}")

    after_a = pass_a(mergeable)
    print(f"  Pass A (same-origin dest-SA3 merge, cap {SOFT_CAP}): "
          f"{len(mergeable):,} -> {len(after_a):,}")

    after_b = pass_b(after_a)
    dust_left = sum(1 for p in after_b if int(p["size"]) < DUST_SIZE)
    print(f"  Pass B (fold size<{DUST_SIZE} dust): "
          f"{len(after_a):,} -> {len(after_b):,} "
          f"({dust_left} sub-dust survivors)")

    new_pops = after_b + passthrough
    data["pops"] = new_pops

    total_after = sum(int(p["size"]) for p in new_pops)
    if total_after != total_before:
        sys.exit(f"ERROR: total pop size changed "
                 f"{total_before:,} -> {total_after:,} — refusing to write.")

    # popIds rebuilt from scratch: referential integrity by construction.
    by_point: dict[str, list[str]] = defaultdict(list)
    for p in new_pops:
        by_point[p["residenceId"]].append(p["id"])
        by_point[p["jobId"]].append(p["id"])
    known_points = {pt["id"] for pt in points}
    orphans = [pid for pid in by_point if pid not in known_points]
    if orphans:
        sys.exit(f"ERROR: pops reference unknown points: {orphans[:5]} "
                 f"({len(orphans)} total) — refusing to write.")
    for pt in points:
        pt["popIds"] = by_point.get(pt["id"], [])

    OUTPUT.write_text(json.dumps(data, separators=(",", ":")),
                      encoding="utf-8")

    print(f"  After:  {size_stats(new_pops)}")
    print(f"  By prefix: {prefix_counts(new_pops)}")
    print(f"  Wrote {OUTPUT}")


if __name__ == "__main__":
    main()

# v1.1.1 — Demand rebalance

Origins are now merged onto a **density-graduated grid** instead of the
distance-from-CBD rule v1.1.0 shipped with. That rule collapsed everything
beyond 25 km into one point per SA2, which flattened the entire outer rail
corridor — Frankston held 17,491 residents in a single point, Pakenham
19,903, and Hastings had **no residential demand within 3 km of the
station**. Station catchments out there were all-or-nothing.

Cell size now follows local settlement density (SA1 land area is the proxy
— the ABS sizes every SA1 to hold a similar population), so corridor towns
stay sharp because they are genuinely dense, and only the rural gaps
between them coarsen:

| Anchor | points within 3 km | largest single point |
|---|---|---|
| Frankston | 16 → **82** | 17,491 → **1,109** |
| Pakenham | 9 → **71** | 19,903 → **1,200** |
| Cranbourne | 21 → **110** | 17,584 → **2,679** |
| Hastings | 3 (0 residents) → **24** | — → 7,499 in the area |

The inner ring collapses a little more to pay for it (0–10 km: 2,209 → 778
points), and **jobs now cluster into real workplaces** — 2,531 employment
points instead of jobs smeared across every residential block, with the
top 100 sites holding 35% of all jobs. Pop count barely moved: 35,909 →
**36,759**.

# v1.1.0 — Simulation performance update

The game fully simulates every pop (mode choice, transit paths, departure
timing), so pop count is the dominant in-play performance cost. v1.0.3
shipped **107,413 pops — ~4× a stock city** — with a median size of just
14 people. v1.1.0 cuts that **3× to 35,909 pops** (mean size 124, matching
stock maps) without changing total commuter volume, and trims demand to a
non-rectangular extent following the real metro rail footprint.

## What changed

- **Non-rectangular demand extent** (`extent/mel_extent_v1_1.geojson`):
  demand now exists only inside a polygon covering every current metro
  terminus (Sunbury, Craigieburn, Mernda, Hurstbridge, Lilydale, Belgrave,
  East Pakenham, Cranbourne, Frankston, the full Stony Point corridor,
  Werribee, Melton) plus Geelong through Waurn Ponds. Phillip Island, the
  Mornington Peninsula tip (Rye/Sorrento/Portsea), Torquay and the outer
  Bellarine still **render as scenery** but carry no residents, jobs,
  schools or venues.
- **Pop consolidation** (`consolidate_pops.py`): destinations per origin
  4 (was 8); outer-fringe SA1s collapsed to one point per SA2; same-origin
  flows merged by destination district (250 soft cap); sub-10-person dust
  folded. Totals preserved exactly: residents == pop sizes == 4,456,418.
- **demandDotScaling 0.75** in config — compensates bubble size for the
  bigger consolidated pops.
- **peak_penalty.py is now a pure transform** (reads the consolidated
  file, writes `demand_data.json`) — re-running can no longer compound
  the congestion multipliers.
- Removed demand venues outside the new extent: Phillip Island Penguin
  Parade, Sorrento beach. 25 coastal schools filtered by the polygon.

## Numbers

| | v1.0.3 | v1.1.0 |
|---|---|---|
| pops | 107,413 | **35,909** |
| demand points | 13,404 | **9,147** |
| pop size mean / median | 42.4 / 14 | **124.1 / 127** |
| pops ≤ 2 people | 24,559 | **45** |
| demand_data.json | 17.4 MB | **6.5 MB** |
| total commuters | 4,549,678 | 4,456,418 |

## Game 1.4+ compatibility: binary buildings index

Subway Builder 1.4 changed the buildings-index format and 1.6 dropped the
legacy JSON reader entirely — v1.0.x showed as **incompatible ("building
format needs 1.3.0 or older")** on current game versions. v1.1.0 ships
`buildings_index.bin` (binary "SBBI" format, same one the stock maps use)
instead of `buildings_index.json`. Building footprints and 1 m foundation
depths are identical to v1.0.x — only the encoding changed. If you are
somehow still on game <= 1.3.0, use the v1.0.3 release.

Other basemap assets (`MEL.pmtiles`, `roads.geojson`,
`runways_taxiways.geojson`) are unchanged from v1.0.x; this release
targets in-play simulation speed. Asset slimming is planned separately.

## Install

Railyard → Library → **Import Asset** → Map → `melbourne.zip`, then
restart the game. (Keep Railyard running — it serves the tiles.)

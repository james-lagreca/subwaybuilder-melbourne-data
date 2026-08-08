"""
make_extent.py — Generate and validate the v1.1 demand-extent polygon.

Writes extent/mel_extent_v1_1.geojson: a single GeoJSON Feature (Polygon)
that is the canonical clip geometry for the demand pipeline. The polygon
covers every current Melbourne metro rail terminus plus the Geelong line
through Waurn Ponds and the Stony Point corridor down the Western Port
shore, while excluding Phillip/French Island, the Mornington Peninsula
tip (Rye/Sorrento/Portsea), the surf coast (Torquay), and the outer
Bellarine (Ocean Grove/Queenscliff).

The rendered basemap (pmtiles/buildings/roads) is NOT clipped by this —
areas outside the polygon keep rendering as scenery; they just carry no
demand.

Validation: asserts that every must-keep location (rail termini, venues
from melbourne.json) is inside the polygon and every must-exclude town is
outside. Exits non-zero on any failure — run this before the demand
pipeline and after any vertex change.

    python make_extent.py
"""

import json
import sys
from pathlib import Path

from shapely.geometry import Point, Polygon, mapping
from shapely.geometry.polygon import orient

OUT_PATH = Path("extent/mel_extent_v1_1.geojson")

# Ring vertices (lon, lat), authored clockwise; re-oriented CCW on write
# per RFC 7946. Keep the table in the README in sync with this.
VERTICES = [
    (144.52, -37.55),  # 1  NW corner (Sunbury buffered by the 14->1 diagonal)
    (145.55, -37.55),  # 2  NE corner
    (145.55, -38.10),  # 3  E edge: Lilydale/Belgrave/Gembrook/E.Pakenham/Healesville in; Warburton out
    (145.42, -38.16),  # 4  SE taper past the Pakenham/Officer/Clyde growth belt; Koo Wee Rup out
    (145.26, -38.25),  # 5  Western Port NW shore approach
    (145.26, -38.42),  # 6  channel W of French Island; Stony Point + HMAS Cerberus in
    (145.02, -38.42),  # 7  S edge: full Stony Point line in; Flinders + Phillip Island out
    (144.87, -38.36),  # 8  peninsula cut: Dromana/Rosebud in; Rye/Sorrento/Portsea out
    (144.87, -38.31),  # 9  turn toward the bay mouth
    (144.70, -38.28),  # 10 across the Heads N of Point Nepean/Queenscliff
    (144.62, -38.22),  # 11 inner Bellarine: Drysdale/Leopold in; Queenscliff out
    (144.45, -38.26),  # 12 Bellarine cut: Marshall/Waurn Ponds in; Ocean Grove/Torquay out
    (144.25, -38.26),  # 13 SW corner
    (144.25, -37.80),  # 14 W edge: Geelong/Lara/Werribee/Wyndham Vale
    # closes back to 1 via the NW diagonal (keeps Melton, cuts Ballan)
]

# Must-keep: every current metro terminus + Geelong-line stations + key
# out-of-network anchors. (name, lon, lat)
MUST_KEEP = [
    ("East Pakenham station",   145.503, -38.077),
    ("Cranbourne station",      145.282, -38.099),
    ("Frankston station",       145.125, -38.144),
    ("Stony Point station",     145.222, -38.374),
    ("Hastings station",        145.183, -38.305),
    ("Belgrave station",        145.355, -37.909),
    ("Lilydale station",        145.347, -37.757),
    ("Hurstbridge station",     145.194, -37.639),
    ("Mernda station",          145.098, -37.601),
    ("Craigieburn station",     144.942, -37.602),
    ("Upfield station",         144.962, -37.660),
    ("Sunbury station",         144.728, -37.579),
    ("Werribee station",        144.662, -37.900),
    ("Melton station",          144.573, -37.698),
    ("Alamein station",         145.079, -37.868),
    ("Glen Waverley station",   145.164, -37.879),
    ("Sandringham station",     145.004, -37.950),
    ("Williamstown station",    144.897, -37.867),
    ("Geelong station",         144.353, -38.141),
    ("Marshall station",        144.373, -38.203),
    ("Waurn Ponds station",     144.288, -38.216),
    ("Dromana",                 144.965, -38.333),
    ("Rosebud",                 144.900, -38.355),
]

# Must-exclude: demand-free scenery from v1.1.0 on. (name, lon, lat)
MUST_EXCLUDE = [
    ("Cowes (Phillip Island)",  145.238, -38.452),
    ("French Island",           145.370, -38.350),
    ("Rye",                     144.820, -38.383),
    ("Sorrento",                144.742, -38.339),
    ("Portsea",                 144.710, -38.317),
    ("Flinders",                145.020, -38.470),
    ("Torquay",                 144.326, -38.330),
    ("Ocean Grove",             144.518, -38.268),
    ("Queenscliff",             144.662, -38.269),
    ("Warburton",               145.688, -37.752),
    ("Koo Wee Rup",             145.494, -38.198),
]

# Venue codes deliberately dropped from melbourne.json in v1.1.0 — skip
# them if still present so this script passes both before and after that
# edit lands.
REMOVED_VENUES = {"PIPP", "SORR"}


def venue_checks() -> list[tuple[str, float, float]]:
    """Every location melbourne.json will feed to Demand-Adder must be
    inside the polygon, or the exe creates demand points in dead space."""
    cfg = json.loads(Path("melbourne.json").read_text(encoding="utf-8"))
    checks = []
    groups = [
        ("entertainment", "ent_loc"),
        ("airport",       "airport_loc"),
        ("universities",  "univ_loc"),
        ("bases",         "base_loc"),
    ]
    for names_key, locs_key in groups:
        names, locs = cfg[names_key], cfg[locs_key]
        if len(names) != len(locs):
            sys.exit(f"ERROR: melbourne.json {names_key} ({len(names)}) and "
                     f"{locs_key} ({len(locs)}) lengths differ")
        for name, (lon, lat) in zip(names, locs):
            if name in REMOVED_VENUES:
                continue
            checks.append((f"venue {name}", lon, lat))
    return checks


def main() -> None:
    poly = orient(Polygon(VERTICES), sign=1.0)  # CCW exterior per RFC 7946
    if not poly.is_valid:
        sys.exit("ERROR: polygon is not valid (self-intersection?)")

    keep = MUST_KEEP + venue_checks()
    failures = []
    for name, lon, lat in keep:
        if not poly.contains(Point(lon, lat)):
            failures.append(f"  MUST-KEEP OUTSIDE:  {name} ({lon}, {lat})")
    for name, lon, lat in MUST_EXCLUDE:
        if poly.contains(Point(lon, lat)):
            failures.append(f"  MUST-EXCLUDE INSIDE: {name} ({lon}, {lat})")

    if failures:
        print("Extent polygon validation FAILED:")
        print("\n".join(failures))
        sys.exit(1)

    minx, miny, maxx, maxy = poly.bounds
    feature = {
        "type": "Feature",
        "properties": {
            "name": "melbourne-demand-extent",
            "version": "1.1.0",
        },
        "geometry": mapping(poly),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(feature, indent=2), encoding="utf-8")

    print(f"OK: all {len(keep)} must-keep locations inside, "
          f"{len(MUST_EXCLUDE)} must-exclude outside.")
    print(f"    Envelope bbox: [{minx}, {miny}, {maxx}, {maxy}]")
    print(f"    Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()

"""
make_buildings_bin.py — Generate buildings_index.bin (+ .bin.gz) for game >= 1.4.

Subway Builder 1.4.x dropped support for the legacy buildings_index.json;
1.6.x ships only a binary reader ("SBBI" format). This is a faithful
standalone port of depot v1.2.7's MapGen.create_buildings_index_binary()
(github.com/Subway-Builder-Modded/depot, src/depot/maps.py) so the bin can
be produced from the existing MEL/buildings_cleaned.json intermediate
without upgrading the depot checkout or re-running the basemap pipeline.

One deliberate difference from upstream: foundationDepth is a constant
1.0 for every building — matching the maxDepth=1 the v1.0.x JSON index
shipped — instead of upstream's 10.0 fallback, so tunnel-obstruction
behaviour is unchanged by the format migration.

Usage:
    python make_buildings_bin.py                 # MEL/buildings_cleaned.json -> MEL/buildings_index.bin(.gz)
    python make_buildings_bin.py --check <file>  # decode/validate any .bin or .bin.gz header

Binary layout (little-endian), header 88 bytes:
    I  magic 0x49424253 ("SBBI")   B  version=1   B,H  padding
    I  buildingCount   I cols   I rows   I totalRings   I totalCoords
    I  cellCount       I totalCellRefs  I padding
    d  cellSize        d maxDepth
    d  minLon  d minLat  d maxLon  d maxLat
Then: bounds <4d>*N | foundations <f>*N | align8 | ringOffsets <I>*(N+1)
    | coordOffsets <I>*(R+1) | align8 | coords <2d>*C
    | cellRowStarts <I>*(rows+1) | cellCols <I>*cells
    | cellBuildingOffsets <I>*(cells+1) | cellBuildingIds <I>*refs
"""

import argparse
import gzip
import json
import math
import struct
import sys
from pathlib import Path

INPUT_JSON  = Path("MEL/buildings_cleaned.json")
OUTPUT_BIN  = Path("MEL/buildings_index.bin")

_HEADER_STRUCT  = struct.Struct("<IBBHI IIIIIII d ddddd")
_UINT32         = struct.Struct("<I")
_FLOAT32        = struct.Struct("<f")
_COORD2D        = struct.Struct("<2d")
_BOUNDS4D       = struct.Struct("<4d")
_BINARY_MAGIC   = 0x49424253
_BINARY_VERSION = 1
_HEADER_SIZE    = 88

CS = 0.0009               # spatial grid cell size (~100 m), matches upstream
FOUNDATION_DEPTH = 1.0    # parity with the v1.0.x JSON index (maxDepth 1)


def round_num(num: float) -> float:
    return math.floor(num * 100000.0 + 0.5) / 100000.0


def ring_area(ring: list) -> float:
    """Unsigned shoelace area (degrees^2) — only used to pick the largest
    part of a MultiPolygon, mirroring upstream's shapely max-by-area."""
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def iter_features(path: Path):
    """Stream features from the (machine-generated, one-feature-per-line)
    FeatureCollection without holding the 574 MB document in memory.
    Falls back to a full json.load if the line format ever changes."""
    streamed = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith('{"type":"Feature"'):
                continue
            if line.endswith(","):
                line = line[:-1]
            elif line.endswith("]}"):  # last feature carries the collection tail
                line = line[: line.rfind("}") + 1] if line.endswith('"]}') else line
            try:
                yield json.loads(line)
                streamed += 1
            except json.JSONDecodeError:
                # tolerate the closing-bracket tail on the final line
                cleaned = line.rstrip("]}").rstrip(",")
                try:
                    yield json.loads(cleaned + "}}" [: max(0, 0)])
                except json.JSONDecodeError:
                    continue
    if streamed == 0:
        print("  (line streaming matched nothing — falling back to json.load)")
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("features", data.get("geometries", [])):
            yield item


def build(input_path: Path, output_bin: Path) -> None:
    print(f"Reading {input_path} (streaming)...")
    buildings = []   # (bounds4, polygon[list of rings])
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    skipped = 0

    for item in iter_features(input_path):
        geom = item.get("geometry", item)
        if not geom or "coordinates" not in geom:
            skipped += 1
            continue
        gtype = geom.get("type")
        coords = geom["coordinates"]
        if gtype == "MultiPolygon":
            if not coords:
                skipped += 1
                continue
            coords = max(coords, key=lambda poly: ring_area(poly[0]) if poly else 0.0)
        elif gtype != "Polygon":
            skipped += 1
            continue

        cleaned_polygon = []
        b_minx = b_miny = float("inf")
        b_maxx = b_maxy = float("-inf")
        for ring in coords:
            if len(ring) < 3:
                continue
            if ring[0] != ring[-1]:
                ring = ring + [ring[0]]
            cleaned_ring = []
            for pt in ring:
                px, py = pt[0], pt[1]
                cleaned_ring.append((round_num(px), round_num(py)))
                if px < b_minx: b_minx = px
                if py < b_miny: b_miny = py
                if px > b_maxx: b_maxx = px
                if py > b_maxy: b_maxy = py
            cleaned_polygon.append(cleaned_ring)
        if not cleaned_polygon:
            skipped += 1
            continue

        bounds = (round_num(b_minx), round_num(b_miny),
                  round_num(b_maxx), round_num(b_maxy))
        buildings.append((bounds, cleaned_polygon))

        if bounds[0] < min_lon: min_lon = bounds[0]
        if bounds[2] > max_lon: max_lon = bounds[2]
        if bounds[1] < min_lat: min_lat = bounds[1]
        if bounds[3] > max_lat: max_lat = bounds[3]

    n = len(buildings)
    if n == 0:
        sys.exit("STOP: no valid buildings found")
    print(f"  {n:,} buildings ({skipped} skipped)")

    min_lon, min_lat = round_num(min_lon), round_num(min_lat)
    max_lon, max_lat = round_num(max_lon), round_num(max_lat)

    cell_size_lon = CS / math.cos((((min_lat + max_lat) / 2) * math.pi) / 180)
    cols = math.ceil((max_lon - min_lon) / cell_size_lon)
    rows = math.ceil((max_lat - min_lat) / CS)

    print(f"  Grid {cols} x {rows}, bbox [{min_lon}, {min_lat}, {max_lon}, {max_lat}]")

    cells_map = {}
    for building_id, (bounds, _poly) in enumerate(buildings):
        b_min_x, b_min_y, b_max_x, b_max_y = bounds
        min_col = math.floor((b_min_x - min_lon) / cell_size_lon)
        max_col = math.floor((b_max_x - min_lon) / cell_size_lon)
        min_row = math.floor((b_min_y - min_lat) / CS)
        max_row = math.floor((b_max_y - min_lat) / CS)
        for col in range(max(0, min_col), min(cols - 1, max_col) + 1):
            for row in range(max(0, min_row), min(rows - 1, max_row) + 1):
                cells_map.setdefault((col, row), []).append(building_id)

    sorted_cells = [
        {"col": k[0], "row": k[1], "buildingIds": v}
        for k, v in sorted(cells_map.items(), key=lambda c: (c[0][1], c[0][0]))
        if v
    ]

    total_rings     = sum(len(poly) for _b, poly in buildings)
    total_coords    = sum(len(ring) for _b, poly in buildings for ring in poly)
    total_cell_refs = sum(len(c["buildingIds"]) for c in sorted_cells)

    # Buffer layout (mirrors upstream exactly)
    o = _HEADER_SIZE
    bounds_offset = o;                  o += n * 32
    foundation_depths_offset = o;       o += n * 4
    o = (o + 7) & ~7
    building_ring_offsets_offset = o;   o += (n + 1) * 4
    ring_coord_offsets_offset = o;      o += (total_rings + 1) * 4
    o = (o + 7) & ~7
    coords_offset = o;                  o += total_coords * 16
    cell_row_starts_offset = o;         o += (rows + 1) * 4
    cell_cols_offset = o;               o += len(sorted_cells) * 4
    cell_building_offsets_offset = o;   o += (len(sorted_cells) + 1) * 4
    cell_building_ids_offset = o;       o += total_cell_refs * 4

    print(f"  Packing {o / 1e6:.1f} MB "
          f"({total_rings:,} rings, {total_coords:,} coords, "
          f"{len(sorted_cells):,} cells, {total_cell_refs:,} refs)...")
    buffer = bytearray(o)

    _HEADER_STRUCT.pack_into(
        buffer, 0,
        _BINARY_MAGIC, _BINARY_VERSION, 0, 0,
        n, cols, rows, total_rings, total_coords,
        len(sorted_cells), total_cell_refs, 0,
        CS, float(FOUNDATION_DEPTH),
        min_lon, min_lat, max_lon, max_lat,
    )

    off = bounds_offset
    for bounds, _poly in buildings:
        _BOUNDS4D.pack_into(buffer, off, *bounds)
        off += 32

    off = foundation_depths_offset
    for _ in range(n):
        _FLOAT32.pack_into(buffer, off, FOUNDATION_DEPTH)
        off += 4

    ring_cursor = coord_cursor = 0
    ring_off  = building_ring_offsets_offset
    coord_off = ring_coord_offsets_offset
    c_off     = coords_offset
    for _bounds, poly in buildings:
        _UINT32.pack_into(buffer, ring_off, ring_cursor)
        ring_off += 4
        for ring in poly:
            _UINT32.pack_into(buffer, coord_off, coord_cursor)
            coord_off += 4
            for px, py in ring:
                _COORD2D.pack_into(buffer, c_off, px, py)
                c_off += 16
                coord_cursor += 1
            ring_cursor += 1
    _UINT32.pack_into(buffer, ring_off, ring_cursor)
    _UINT32.pack_into(buffer, coord_off, coord_cursor)

    ref_cursor = cell_row = 0
    _UINT32.pack_into(buffer, cell_row_starts_offset, 0)
    cc_off  = cell_cols_offset
    cbo_off = cell_building_offsets_offset
    cbi_off = cell_building_ids_offset
    for i, cell in enumerate(sorted_cells):
        while cell_row < cell["row"]:
            cell_row += 1
            _UINT32.pack_into(buffer, cell_row_starts_offset + cell_row * 4, i)
        _UINT32.pack_into(buffer, cc_off, cell["col"]);  cc_off += 4
        _UINT32.pack_into(buffer, cbo_off, ref_cursor);  cbo_off += 4
        for b_id in cell["buildingIds"]:
            _UINT32.pack_into(buffer, cbi_off, b_id);    cbi_off += 4
            ref_cursor += 1
    _UINT32.pack_into(buffer, cbo_off, ref_cursor)
    while cell_row < rows:
        cell_row += 1
        _UINT32.pack_into(buffer, cell_row_starts_offset + cell_row * 4,
                          len(sorted_cells))

    bin_data = bytes(buffer)
    output_bin.write_bytes(bin_data)
    gz_path = Path(str(output_bin) + ".gz")
    gz_path.write_bytes(gzip.compress(bin_data, compresslevel=9))
    print(f"  Wrote {output_bin} ({len(bin_data) / 1e6:.1f} MB) and "
          f"{gz_path} ({gz_path.stat().st_size / 1e6:.1f} MB)")

    check(output_bin)


def check(path: Path) -> None:
    """Decode a .bin/.bin.gz header and validate the section math."""
    raw = path.read_bytes()
    if path.suffix == ".gz" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if len(raw) < _HEADER_SIZE:
        sys.exit(f"CHECK FAIL: {path} smaller than header")
    (magic, version, _p1, _p2, n, cols, rows, total_rings, total_coords,
     cell_count, total_refs, _p3, cs, max_depth,
     min_lon, min_lat, max_lon, max_lat) = _HEADER_STRUCT.unpack_from(raw, 0)

    ok = True
    if magic != _BINARY_MAGIC:
        print(f"  CHECK FAIL: bad magic {magic:#x}"); ok = False
    if version != _BINARY_VERSION:
        print(f"  CHECK FAIL: version {version}"); ok = False

    o = _HEADER_SIZE
    o += n * 32 + n * 4
    o = (o + 7) & ~7
    o += (n + 1) * 4 + (total_rings + 1) * 4
    o = (o + 7) & ~7
    o += total_coords * 16
    o += (rows + 1) * 4 + cell_count * 4 + (cell_count + 1) * 4 + total_refs * 4
    if o != len(raw):
        print(f"  CHECK FAIL: computed size {o:,} != file size {len(raw):,}")
        ok = False

    print(f"  {path.name}: magic OK, v{version}, buildings {n:,}, "
          f"grid {cols}x{rows}, rings {total_rings:,}, coords {total_coords:,}, "
          f"cells {cell_count:,}, refs {total_refs:,}")
    print(f"  cellSize {cs}, maxDepth {max_depth}, "
          f"bbox [{min_lon:.5f}, {min_lat:.5f}, {max_lon:.5f}, {max_lat:.5f}]")
    if ok:
        print("  Structure check PASSED")
    else:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", metavar="FILE",
                    help="decode/validate an existing .bin or .bin.gz instead of building")
    ap.add_argument("--input", default=str(INPUT_JSON))
    ap.add_argument("--output", default=str(OUTPUT_BIN))
    args = ap.parse_args()

    if args.check:
        check(Path(args.check))
        return
    build(Path(args.input), Path(args.output))


if __name__ == "__main__":
    main()

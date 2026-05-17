"""
Generate the Melbourne PMTiles basemap + buildings index + roads/runways
GeoJSON using the official Subway Builder Modded `depot` library.

Prerequisites (host machine — easiest in WSL2 Ubuntu on Windows):
  - Python 3.13 + the `depot` conda env (geopandas, duckdb, mapbox_vector_tile,
    shapely, numpy, pandas — see depot/environment.yml).
  - `depot` installed:  pip install git+https://github.com/Subway-Builder-Modded/depot
  - CLI tools on PATH:  tippecanoe, tile-join, osmium, mapshaper, java,
    planetiler.jar, pmtiles, sqlite3, jq.
  - Clipped Melbourne OSM extract at OSMPBF below. Make it from the
    Australia-wide Geofabrik PBF:
        osmium extract -b 144.45,-38.40,145.65,-37.55 \\
            australia-latest.osm.pbf -o melbourne.osm.pbf

Output (written into OUTPUT_DIR):
  - melbourne.pmtiles, melbourne_foundations.pmtiles
  - buildings_index.json
  - roads.geojson, runways_taxiways.geojson

Upload each of those to the GitHub Release on subwaybuilder-melbourne-data
that the TS mod's main.ts URL references.
"""

from depot.maps import MapGen


CITY    = "MEL"
BBOX    = [144.25, -38.55, 145.65, -37.55]  # Greater Melbourne + Geelong + full Mornington Pen + Phillip Island
OSMPBF  = "./melbourne.osm.pbf"             # clipped extract, see osmium command above
# depot writes output into ./<CITY>/ relative to cwd (its default outputdir='.').
# Final artefacts end up in ./MEL/.


def main() -> None:
    mapgen = MapGen(
        city=CITY,
        bbox=BBOX,
        osmpbf=OSMPBF,

        # Filter very small structures out of the buildings index.
        building_index_filter_size=40,
        building_tile_filter_size=40,

        # OSM `place=*` values that should render at city / suburb / neighbourhood
        # zooms. Tuned to what depot.check_labels() reports for this bbox:
        #   city: 2  (Melbourne, Geelong)
        #   town: 20 (Frankston, Cranbourne, Bacchus Marsh, ...)
        #   suburb: 472 (main metro suburb tier)
        #   village: 49 (rural villages on Mornington Pen + Phillip Island)
        #   neighbourhood: 57, locality: 86, hamlet: 54, quarter: 0
        cities=['city', 'town'],
        suburbs=['suburb', 'village'],
        neighborhoods=['neighbourhood', 'locality', 'hamlet', 'quarter'],

        # English road names — Melbourne OSM is English-native but this is explicit.
        road_name_preferred_language="en",

        # Treat military areas with the same colour as aerodromes (matches the
        # in-game style for restricted/runway-like polygons).
        color_military_like_aerodrome=True,

        # RAM here only controls the mapshaper Node heap (--max-old-space-size).
        # depot hardcodes -Xmx16g for the separate planetiler step — we've
        # patched the user's editable depot install down to -Xmx10g to fit
        # in a 13 GB WSL2 VM (see README "Memory tuning"). If you can give
        # WSL more memory, also bump this.
        RAM=6,
        ncores=6,
        verb=True,
    )

    # mapgen.check_labels()  # uncomment on first run to inspect place=* values
    mapgen.run_all()


if __name__ == "__main__":
    main()

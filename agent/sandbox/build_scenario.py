"""One-shot scenario builder from the command line.

Same pipeline as the UI panel's Load & Save button, but invoked from a
terminal so you can script bulk generation or rerun without clicking.

Usage:
    python3 -m agent.sandbox.build_scenario <url> <name> [options]

Examples:
    # Greater LA at default 2048-wide map
    python3 -m agent.sandbox.build_scenario \\
        "https://www.google.com/maps/@34.124,-117.604,9.09z" \\
        la --size 2048

    # Tighter OC, smaller map for fast iteration
    python3 -m agent.sandbox.build_scenario \\
        "https://www.google.com/maps/@33.67,-117.77,11z" \\
        oc_small --size 1024

    # Idlib region with custom min population
    python3 -m agent.sandbox.build_scenario \\
        "https://www.google.com/maps/@35.85,36.6,10z" \\
        idlib --min-pop 200

Outputs (all under ~/Documents/OpenTTD/scenario/heightmap/<name>_*):
    *_towns.json       sanitized town list ready for OpenTTD
    *_heightmap.png    grayscale heightmap, ocean-clamped + auto-smoothed
    *_preview.png      colored preview with town markers
And under ~/Documents/OpenTTD/game/:
    nutz_town_loader_<name>/   standalone GS that founds towns at start
    nutz_bridge_<name>/        merged GS: towns + LLM admin bridge
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .gmaps import url_to_bbox, climate_for_lat
from .geo import fetch_osm_towns, to_ottd_json
from .heightmap import generate_heightmap
from .preview import render_preview
from .town_gs import generate_town_loader
from .bridge_gs import generate_bridge_with_towns


SCENARIO_DIR = Path.home() / "Documents/OpenTTD/scenario/heightmap"


def build(url: str, name: str, *, size: int = 2048, min_pop: int = 500,
          smooth: float | None = None, sea_level: float = 0.0,
          swap_xy: bool = False, no_save: bool = False) -> dict:
    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    out: dict = {"name": name}

    print(f"[1/5] resolving URL ...", flush=True)
    center, bbox = url_to_bbox(url)
    out["center"] = (center.lat, center.lon, center.zoom)
    out["bbox"] = bbox
    out["climate_hint"] = climate_for_lat(center.lat)
    print(f"      center=({center.lat}, {center.lon}) z={center.zoom}")
    print(f"      bbox  ={tuple(round(v, 4) for v in bbox)}")
    print(f"      climate (recommended): {out['climate_hint']}")

    print(f"[2/5] fetching OSM towns (min_pop={min_pop}) ...", flush=True)
    towns = fetch_osm_towns(bbox, min_pop=min_pop)
    converted = to_ottd_json(towns, bbox, swap_xy=swap_xy)
    n_cities = sum(1 for t in converted if t["city"])
    print(f"      {len(converted)} towns ({n_cities} cities)")
    out["town_count"] = len(converted)

    if no_save:
        print("[stop] --no-save was set; skipping disk writes")
        return out

    json_path = SCENARIO_DIR / f"{name}_towns.json"
    json_path.write_text(json.dumps(converted, indent=2) + "\n")
    out["towns_json"] = str(json_path)

    print(f"[3/5] generating heightmap (size={size}, smooth={smooth or 'auto'}) ...", flush=True)
    hm = generate_heightmap(bbox, SCENARIO_DIR / f"{name}_heightmap.png",
                             size=size, smooth_sigma_px=smooth,
                             sea_level_m=sea_level)
    print(f"      {hm['width']}x{hm['height']} elev {int(hm['elev_min_m'])}-{int(hm['elev_max_m'])}m, sigma={hm['smooth_sigma_px']}")
    out["heightmap"] = hm

    print(f"[4/5] rendering preview ...", flush=True)
    pv = render_preview(SCENARIO_DIR / f"{name}_heightmap.png",
                        SCENARIO_DIR / f"{name}_towns.json",
                        SCENARIO_DIR / f"{name}_preview.png")
    print(f"      {pv['town_count']} towns, {pv['pct_in_water']}% in water")
    out["preview"] = pv

    print(f"[5/5] generating GameScripts ...", flush=True)
    tl = generate_town_loader(converted, name)
    bg = generate_bridge_with_towns(converted, name)
    out["town_gs"] = tl
    out["bridge_gs"] = bg
    print(f"      town loader: {tl['path']}")
    print(f"      merged bridge: {bg['path']}")

    print()
    print("=" * 60)
    print(f"DONE — to play:")
    print(f"  1. OpenTTD -> New Game -> heightmap: {name}_heightmap.png")
    print(f"  2. World Generation: rotation=Clockwise, peak=15,")
    print(f"     map size {hm['width']}x{hm['height']},")
    print(f"     climate={out['climate_hint']}, No. of towns=Off,")
    print(f"     Industries=Low")
    print(f"  3. Game Script Settings -> 'Nutz Bridge: {bg['handle']}'")
    print(f"  4. Generate. Then in another terminal:")
    print(f"       cd ~/videogamebench")
    print(f"       set -a && source .env && set +a")
    print(f"       python3 -u run_openttd.py --steps 1000 --interval 5")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("url", help="Google Maps URL (the @lat,lon,zoom form)")
    ap.add_argument("name", help="scenario name (used as filename prefix)")
    ap.add_argument("--size", type=int, default=2048,
                    choices=[256, 512, 1024, 2048, 4096],
                    help="output heightmap width in pixels (default 2048)")
    ap.add_argument("--min-pop", type=int, default=500,
                    help="OSM towns below this population are dropped (default 500)")
    ap.add_argument("--smooth", type=float, default=None,
                    help="Gaussian sigma for heightmap; default = size/256 (auto)")
    ap.add_argument("--sea-level", type=float, default=0.0,
                    help="metres below this become water (default 0)")
    ap.add_argument("--swap-xy", action="store_true",
                    help="swap X/Y of town coords (rare; try if towns land wrong)")
    ap.add_argument("--no-save", action="store_true",
                    help="dry-run; print only, don't write files")
    args = ap.parse_args()
    try:
        build(args.url, args.name,
              size=args.size, min_pop=args.min_pop,
              smooth=args.smooth, sea_level=args.sea_level,
              swap_xy=args.swap_xy, no_save=args.no_save)
        return 0
    except Exception as e:
        print(f"\nFAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

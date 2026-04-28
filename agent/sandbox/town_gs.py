"""Generate a per-scenario GameScript that founds towns programmatically.

Bypasses OpenTTD's `LoadTownData` (which has a known CCW-rotation
segfault in 15.3 and is generally finicky about names/punctuation) by
using `GSTown.FoundTown(tile, size, city, layout, name)` from the GS
API. GameScripts run in Deity mode, which can found cities and large
towns directly.

Generated layout:
    ~/Documents/OpenTTD/game/nutz_town_loader_<name>/
        info.nut
        main.nut    <- town list inlined as Squirrel literals

User workflow:
    1. Open OpenTTD -> Scenario Editor or New Game
    2. Load Heightmap (PNG) with rotation = Clockwise (CCW is broken)
    3. Game Script Settings -> select "Nutz Town Loader: <name>"
    4. Generate
    5. At game start, the GS founds every town at its (x, y) coords

Tile math is fixed Clockwise: tile = TileXY(x*MaxX, y*MaxY). For now we
only target the canonical Clockwise rotation - the user has to match.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List


def _safe_handle(name: str) -> str:
    """Sanitize a scenario name into a directory/class/short-name token.
    Squirrel identifiers + OpenTTD GS short-name (4 chars)."""
    h = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:32]
    return (h or "scenario").lower()


def _short_code(handle: str) -> str:
    """Generate a 4-character short-name for OpenTTD's GS registry. We
    use 'DTL' + a hash digit so multiple scenarios coexist."""
    digit = str(sum(ord(c) for c in handle) % 10)
    return ("DTL" + digit)[:4]


def _escape_squirrel_str(s: str) -> str:
    """Squirrel string literals: only ASCII printable, escape backslash + quote."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def generate_town_loader(towns: List[dict],
                          scenario_name: str,
                          out_dir: Path = Path.home() / "Documents/OpenTTD/game") -> dict:
    """Write a one-shot GS that founds every town in `towns` at game start.

    Each `towns[i]` is the dict shape produced by geo.to_ottd_json:
        {name, population, city, x, y}
    """
    handle = _safe_handle(scenario_name)
    short = _short_code(handle)
    class_base = "DlfTownLoader_" + handle
    folder = out_dir / f"nutz_town_loader_{handle}"
    folder.mkdir(parents=True, exist_ok=True)

    # info.nut
    info_class = class_base + "Info"
    # API 14 (not 15!) so compat_14.nut auto-wraps name strings into Text*
    # for FoundTown + SetName. Without this the names get dropped silently
    # and OpenTTD assigns random names from townnameparts.
    info = f'''class {info_class} extends GSInfo {{
    function GetAuthor()      {{ return "Nutz"; }}
    function GetName()        {{ return "Nutz Town Loader: {handle}"; }}
    function GetDescription() {{ return "Founds {len(towns)} real-world towns from JSON at game start. Bypasses LoadTownData."; }}
    function GetVersion()     {{ return 1; }}
    function GetDate()        {{ return "2026-04-26"; }}
    function CreateInstance() {{ return "{class_base}Main"; }}
    function GetShortName()   {{ return "{short}"; }}
    function GetAPIVersion()  {{ return "14"; }}
    function GetUrl()         {{ return ""; }}
}}
RegisterGS({info_class}());
'''
    (folder / "info.nut").write_text(info)

    # main.nut: town list inlined; foreach -> FoundTown with retry on
    # nearby tiles if the canonical target is unbuildable.
    rows = []
    for t in towns:
        name = _escape_squirrel_str(t["name"])
        is_city = "true" if t.get("city") else "false"
        # Real population determines ExpandTown target. Cap at 25K (~500
        # houses) to keep OpenTTD performance sane.
        real_pop = int(t.get("real_population", t.get("population", 0)) or 0)
        rows.append(
            f'        {{ name="{name}", x={t["x"]}, y={t["y"]}, '
            f'city={is_city}, real_pop={real_pop} }}'
        )
    towns_table = ",\n".join(rows)

    main = f'''/* Auto-generated Nutz Town Loader for "{handle}".
 * Founds towns at game start using GSTown.FoundTown (deity mode).
 * Tile math: Clockwise rotation (use rotation=Clockwise in heightmap load).
 */

class {class_base}Main extends GSController {{
    function Start() {{
        local towns = [
{towns_table}
        ];
        local map_x = GSMap.GetMapSizeX();
        local map_y = GSMap.GetMapSizeY();
        GSLog.Info("Nutz Town Loader '{handle}': founding " + towns.len() + " towns on " + map_x + "x" + map_y);
        local placed = 0;
        local failed = 0;
        foreach (t in towns) {{
            local tx = (t.x * map_x).tointeger();
            local ty = (t.y * map_y).tointeger();
            if (tx < 1) tx = 1;
            if (ty < 1) ty = 1;
            if (tx > map_x - 2) tx = map_x - 2;
            if (ty > map_y - 2) ty = map_y - 2;
            local tile = GSMap.GetTileIndex(tx, ty);
            /* Initial size by real population: <50K=SMALL, <500K=MEDIUM, else LARGE. */
            local size = GSTown.TOWN_SIZE_SMALL;
            if (t.real_pop > 500000) size = GSTown.TOWN_SIZE_LARGE;
            else if (t.real_pop > 50000) size = GSTown.TOWN_SIZE_MEDIUM;
            local pre = GSTownList().Count();
            local ok = GSTown.FoundTown(tile, size, t.city, GSTown.ROAD_LAYOUT_ORIGINAL, t.name);
            /* If target tile rejects, spiral outward up to radius 12. */
            if (!ok) {{
                local r = 1;
                while (!ok && r <= 12) {{
                    for (local dx = -r; dx <= r && !ok; dx++) {{
                        for (local dy = -r; dy <= r && !ok; dy++) {{
                            if (dx != -r && dx != r && dy != -r && dy != r) continue;
                            local nt = GSMap.GetTileIndex(tx + dx, ty + dy);
                            ok = GSTown.FoundTown(nt, size, t.city, GSTown.ROAD_LAYOUT_ORIGINAL, t.name);
                        }}
                    }}
                    r++;
                }}
            }}
            if (ok) {{
                placed++;
                /* Find the just-founded town (highest id since count went up). */
                local list = GSTownList();
                if (list.Count() > pre) {{
                    local new_id = -1;
                    foreach (id, _ in list) {{
                        if (id > new_id) new_id = id;
                    }}
                    /* Scale real pop -> houses (~50 ppl/house), capped 500. */
                    local target_houses = (t.real_pop / 50).tointeger();
                    if (target_houses > 500) target_houses = 500;
                    local current = GSTown.GetHouseCount(new_id);
                    local add = target_houses - current;
                    if (add > 0 && new_id >= 0) {{
                        GSTown.ExpandTown(new_id, add);
                    }}
                }}
                GSLog.Info(" + " + t.name + " @ (" + tx + "," + ty + ") pop=" + t.real_pop);
            }} else {{
                failed++;
                GSLog.Warning(" - " + t.name + " could not be founded near (" + tx + "," + ty + ")");
            }}
        }}
        GSLog.Info("Nutz Town Loader '{handle}': placed=" + placed + " failed=" + failed);
        while (true) {{ this.Sleep(50000); }}
    }}
}}
'''
    (folder / "main.nut").write_text(main)
    return {
        "path": str(folder),
        "handle": handle,
        "short": short,
        "town_count": len(towns),
        "info": str(folder / "info.nut"),
        "main": str(folder / "main.nut"),
    }


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--towns-json", required=True, type=Path)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    towns = json.loads(Path(args.towns_json).read_text())
    meta = generate_town_loader(towns, args.name)
    print(json.dumps(meta, indent=2))
    print(f"\nIn OpenTTD: Game Script Settings -> 'Nutz Town Loader: {meta['handle']}'", file=sys.stderr)

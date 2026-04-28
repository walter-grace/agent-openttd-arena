"""Generate a per-scenario merged GameScript: nutz_bridge + town loader.

OpenTTD allows ONLY ONE GameScript per game, so to get both:
  - the LLM/admin-port bridge (state push, sign mailbox, blueprint cmd)
  - auto-founded real-world towns at game start

we must combine them into a single GS per scenario. This module clones
the canonical nutz_bridge logic and prepends a one-shot town founder that
inlines the town list as Squirrel literals.

Generated layout:
    ~/Documents/OpenTTD/game/nutz_bridge_<name>/
        info.nut
        main.nut

User picks "Nutz Bridge: <name>" in Game Script Settings instead of the
plain "Nutz Bridge" - they get both features in one go.

Tile math: Clockwise rotation (matches OpenTTD's CW heightmap path,
which is also the only one not crashing in 15.3).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List


def _safe_handle(name: str) -> str:
    h = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:32]
    return (h or "scenario").lower()


def _short_code(handle: str) -> str:
    digit = str(sum(ord(c) for c in handle) % 10)
    return ("DLB" + digit)[:4]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def generate_bridge_with_towns(towns: List[dict],
                                scenario_name: str,
                                out_dir: Path = Path.home() / "Documents/OpenTTD/game") -> dict:
    """Write a GS that founds inlined towns at start, then runs the
    Nutz bridge loop forever (state push + admin command handling)."""
    handle = _safe_handle(scenario_name)
    short = _short_code(handle)
    class_main = f"NutzBridgeWithTowns_{handle}Main"
    class_info = f"NutzBridgeWithTowns_{handle}Info"
    folder = out_dir / f"nutz_bridge_{handle}"
    folder.mkdir(parents=True, exist_ok=True)

    info = f'''class {class_info} extends GSInfo {{
    function GetAuthor()      {{ return "Nutz"; }}
    function GetName()        {{ return "Nutz Bridge: {handle}"; }}
    function GetDescription() {{ return "Nutz Bridge (LLM admin-port + sign mailbox + blueprint cmd) PLUS auto-founds {len(towns)} real-world towns at game start."; }}
    function GetVersion()     {{ return 1; }}
    function GetDate()        {{ return "2026-04-26"; }}
    function CreateInstance() {{ return "{class_main}"; }}
    function GetShortName()   {{ return "{short}"; }}
    function GetAPIVersion()  {{ return "15"; }}
    function GetUrl()         {{ return ""; }}
}}
RegisterGS({class_info}());
'''
    (folder / "info.nut").write_text(info)

    rows = []
    for t in towns:
        real_pop = int(t.get("real_population", t.get("population", 0)) or 0)
        rows.append(
            f'        {{ name="{_esc(t["name"])}", x={t["x"]}, y={t["y"]}, '
            f'city={"true" if t.get("city") else "false"}, real_pop={real_pop} }}'
        )
    towns_table = ",\n".join(rows)

    main = f'''/* Auto-generated Nutz Bridge + Town Loader for "{handle}".
 *
 * One GameScript per game = combines:
 *   1. One-shot town founder (this file) - inlines {len(towns)} real-world
 *      towns and calls GSTown.FoundTown for each at startup.
 *   2. Nutz bridge loop - state push to admin port, sign mailbox cmds,
 *      blueprint dispatch (mirrors ottd_user/game/nutz_bridge/main.nut).
 *
 * Crucially: founding is INTERLEAVED with PushState so the admin port
 * sees state from tick 1, even while towns are still being placed. The
 * old "found everything first, then enter loop" pattern blocked
 * c.get_gs_state() forever and caused plan_route to fail with "no towns
 * in gs state". Any throw in founding is caught so the bridge loop keeps
 * running.
 *
 * API 15. Tile math: Clockwise heightmap rotation. Set No. of towns=Off
 * in World Generation so OpenTTD doesn't add random towns on top.
 */

class {class_main} extends GSController {{
    tick_interval = 30;
    init_done = false;
    init_index = 0;
    init_placed = 0;
    init_failed = 0;

    function Start() {{
        GSLog.Info("Nutz Bridge+Towns '{handle}': starting bridge loop, will found {len(towns)} towns in background.");
        /* Push state IMMEDIATELY so admin port has scenario context even
         * before any town is founded. plan_route would otherwise fail with
         * "no towns in gs state" while founding is in progress. */
        try {{ this.PushState(); }} catch (e) {{ GSLog.Error("first push: " + e); }}
        while (true) {{
            try {{ this.FoundNextTowns(8); }} catch (e) {{ GSLog.Error("found: " + e); this.init_done = true; }}
            try {{ this.PushState(); }} catch (e) {{ GSLog.Error("push: " + e); }}
            try {{ this.HandleEvents(); }} catch (e) {{ GSLog.Error("events: " + e); }}
            this.Sleep(this.tick_interval);
        }}
    }}

    function FoundNextTowns(batch) {{
        if (this.init_done) return;
        local towns = [
{towns_table}
        ];
        if (this.init_index >= towns.len()) {{
            this.init_done = true;
            GSLog.Info("Nutz Bridge+Towns '{handle}': founding done. placed=" + this.init_placed + " failed=" + this.init_failed);
            return;
        }}
        local map_x = GSMap.GetMapSizeX();
        local map_y = GSMap.GetMapSizeY();
        local end = this.init_index + batch;
        if (end > towns.len()) end = towns.len();
        for (local i = this.init_index; i < end; i++) {{
            local t = towns[i];
            local tx = (t.x * map_x).tointeger();
            local ty = (t.y * map_y).tointeger();
            if (tx < 1) tx = 1;
            if (ty < 1) ty = 1;
            if (tx > map_x - 2) tx = map_x - 2;
            if (ty > map_y - 2) ty = map_y - 2;
            local tile = GSMap.GetTileIndex(tx, ty);
            local size = GSTown.TOWN_SIZE_SMALL;
            if (t.real_pop > 500000) size = GSTown.TOWN_SIZE_LARGE;
            else if (t.real_pop > 50000) size = GSTown.TOWN_SIZE_MEDIUM;
            local pre = GSTownList().Count();
            local ok = false;
            try {{
                ok = GSTown.FoundTown(tile, size, t.city, GSTown.ROAD_LAYOUT_ORIGINAL, t.name);
            }} catch (e) {{ ok = false; }}
            if (!ok) {{
                local r = 1;
                while (!ok && r <= 8) {{
                    for (local dx = -r; dx <= r && !ok; dx++) {{
                        for (local dy = -r; dy <= r && !ok; dy++) {{
                            if (dx != -r && dx != r && dy != -r && dy != r) continue;
                            local nt = GSMap.GetTileIndex(tx + dx, ty + dy);
                            try {{
                                ok = GSTown.FoundTown(nt, size, t.city, GSTown.ROAD_LAYOUT_ORIGINAL, t.name);
                            }} catch (e) {{ ok = false; }}
                        }}
                    }}
                    r++;
                }}
            }}
            if (ok) {{
                this.init_placed++;
                local list = GSTownList();
                if (list.Count() > pre) {{
                    local new_id = -1;
                    foreach (id, _ in list) {{
                        if (id > new_id) new_id = id;
                    }}
                    local target_houses = (t.real_pop / 50).tointeger();
                    if (target_houses > 500) target_houses = 500;
                    local current = GSTown.GetHouseCount(new_id);
                    local add = target_houses - current;
                    if (add > 0 && new_id >= 0) {{
                        try {{ GSTown.ExpandTown(new_id, add); }} catch (e) {{}}
                    }}
                }}
            }} else {{
                this.init_failed++;
                GSLog.Warning(" - " + t.name + " : " + GSError.GetLastErrorString());
            }}
        }}
        this.init_index = end;
    }}

    /* ===== bridge loop (mirror of nutz_bridge/main.nut) ===== */

    function PushState() {{
        local data = {{
            kind = "state",
            date = GSDate.GetCurrentDate(),
            year = GSDate.GetYear(GSDate.GetCurrentDate()),
            month = GSDate.GetMonth(GSDate.GetCurrentDate()),
            map = {{ sizeX = GSMap.GetMapSizeX(), sizeY = GSMap.GetMapSizeY() }},
            companies = [],
            towns_top = [],
            industries_count = 0,
            scenario = "{handle}",
        }};
        for (local c = 0; c < 16; c++) {{
            if (GSCompany.ResolveCompanyID(c) == GSCompany.COMPANY_INVALID) continue;
            local name = GSCompany.GetName(c);
            local pres = GSCompany.GetPresidentName(c);
            data.companies.append({{
                id = c,
                name = (name == null ? "" : name),
                manager = (pres == null ? "" : pres),
                value = GSCompany.GetQuarterlyCompanyValue(c, 1),
                income = GSCompany.GetQuarterlyIncome(c, 1),
                expense = GSCompany.GetQuarterlyExpenses(c, 1),
                cargo = GSCompany.GetQuarterlyCargoDelivered(c, 1),
                perf = GSCompany.GetQuarterlyPerformanceRating(c, 1),
            }});
        }}
        local tlist = GSTownList();
        tlist.Valuate(GSTown.GetPopulation);
        tlist.Sort(GSList.SORT_BY_VALUE, false);
        local count = 0;
        local pass_cid = -1;
        local cl = GSCargoList();
        foreach (cid, _ in cl) {{
            if (GSCargo.HasCargoClass(cid, GSCargo.CC_PASSENGERS)) {{ pass_cid = cid; break; }}
        }}
        foreach (t, pop in tlist) {{
            local ent = {{
                id = t,
                name = GSTown.GetName(t),
                tile = GSTown.GetLocation(t),
                pop = pop,
                houses = GSTown.GetHouseCount(t),
                growing = GSTown.IsCity(t) || GSTown.GetRoadReworkDuration(t) == 0,
            }};
            if (pass_cid >= 0) {{
                ent.pass_last <- GSTown.GetLastMonthProduction(t, pass_cid);
                ent.pass_moved <- GSTown.GetLastMonthSupplied(t, pass_cid);
            }}
            data.towns_top.append(ent);
            count += 1;
            if (count >= 40) break;
        }}
        data.industries_count = GSIndustryList().Count();
        data.stations <- [];
        data.vehicles <- [];
        local sl = GSStationList(GSStation.STATION_ANY);
        local sn = 0;
        foreach (s, _ in sl) {{
            data.stations.append({{ id = s, name = GSStation.GetName(s), tile = GSStation.GetLocation(s) }});
            sn += 1;
            if (sn >= 60) break;
        }}
        local vl = GSVehicleList();
        local vn = 0;
        foreach (v, _ in vl) {{
            data.vehicles.append({{
                id = v,
                age = GSVehicle.GetAgeLeft(v),
                profit_ly = GSVehicle.GetProfitLastYear(v),
                profit_ty = GSVehicle.GetProfitThisYear(v),
            }});
            vn += 1;
            if (vn >= 100) break;
        }}
        GSAdmin.Send(data);
    }}

    function HandleEvents() {{
        while (GSEventController.IsEventWaiting()) {{
            local ev = GSEventController.GetNextEvent();
            if (ev == null) break;
            local t = ev.GetEventType();
            if (t == GSEvent.ET_ADMIN_PORT) {{
                local ap = GSEventAdminPort.Convert(ev);
                local obj = ap.GetObject();
                if (obj != null && obj.rawin("cmd")) {{
                    this.ProcessAdminCmd(obj);
                }}
            }}
            GSAdmin.Send({{ kind = "event", type = t }});
        }}
    }}

    function ProcessAdminCmd(obj) {{
        local cmd = obj["cmd"];
        if (cmd == "build" && obj.rawin("from") && obj.rawin("to")) {{
            local tile = GSMap.GetTileIndex(1, 1);
            local text = "NUTZ:build:" + obj["from"] + ":" + obj["to"];
            GSSign.BuildSign(tile, text);
            GSAdmin.Send({{ kind = "ack", cmd = cmd, text = text }});
        }} else if (cmd == "blueprint") {{
            this.PlaceBlueprint(obj);
        }} else if (cmd == "clear") {{
            local sl = GSSignList();
            foreach (s, _ in sl) {{
                local n = GSSign.GetName(s);
                if (n != null && n.len() >= 4 && n.slice(0,4) == "NUTZ:") {{
                    GSSign.RemoveSign(s);
                }}
            }}
            GSAdmin.Send({{ kind = "ack", cmd = cmd }});
        }}
    }}

    function PlaceBlueprint(obj) {{
        if (!obj.rawin("job"))      {{ GSAdmin.Send({{ kind = "err", cmd = "blueprint", reason = "no job" }}); return; }}
        if (!obj.rawin("stations")) {{ GSAdmin.Send({{ kind = "err", cmd = "blueprint", reason = "no stations" }}); return; }}
        if (!obj.rawin("path"))     {{ GSAdmin.Send({{ kind = "err", cmd = "blueprint", reason = "no path" }}); return; }}
        if (!obj.rawin("depot"))    {{ GSAdmin.Send({{ kind = "err", cmd = "blueprint", reason = "no depot" }}); return; }}
        local job = obj["job"];
        local job_str = "" + job;
        local stations = obj["stations"];
        local path = obj["path"];
        local depot = obj["depot"];
        local engine = -1;
        if (obj.rawin("engine")) engine = obj["engine"];
        local placed = 0;
        local sa = stations[0]; local sb = stations[1];
        local tA = sa[0]; local fA = sa[1];
        local tB = sb[0]; local fB = sb[1];
        local tD = depot[0]; local fD = depot[1];
        if (GSSign.BuildSign(tA, "NUTZ:bp:" + job_str + ":S:fr=" + fA + ":eg=" + engine)) placed++;
        if (GSSign.BuildSign(tB, "NUTZ:bp:" + job_str + ":E:fr=" + fB)) placed++;
        if (GSSign.BuildSign(tD, "NUTZ:bp:" + job_str + ":D:fr=" + fD)) placed++;
        for (local i = 0; i < path.len(); i++) {{
            if (GSSign.BuildSign(path[i], "NUTZ:bp:" + job_str + ":W:" + i)) placed++;
        }}
        GSAdmin.Send({{ kind = "ack", cmd = "blueprint", job = job, placed = placed }});
    }}
}}
'''
    (folder / "main.nut").write_text(main)
    return {
        "path": str(folder),
        "handle": handle,
        "short": short,
        "town_count": len(towns),
    }


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--towns-json", required=True, type=Path)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    towns = json.loads(Path(args.towns_json).read_text())
    meta = generate_bridge_with_towns(towns, args.name)
    print(json.dumps(meta, indent=2))
    print(f"\nIn OpenTTD: Game Script Settings -> 'Nutz Bridge: {meta['handle']}'", file=sys.stderr)

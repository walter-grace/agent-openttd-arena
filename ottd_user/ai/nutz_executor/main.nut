/* Nutz Executor AI - builds a blueprint placed in the world as signs.
 *
 * Sign protocol (placed by Python via nutz_bridge GS, or directly by a
 * sandbox bootstrap GS):
 *   NUTZ:bp:<job>:S:fr=<tile>:eg=<id>   placed at station-A tile
 *   NUTZ:bp:<job>:E:fr=<tile>           placed at station-B tile
 *   NUTZ:bp:<job>:W:<n>                 placed at every road-path tile
 *   NUTZ:bp:<job>:D:fr=<tile>           placed at depot tile
 *
 * Uses the official Pathfinder.Road library (Online Content -> AI
 * Library). The library handles slopes, water (auto-bridge), tunnels,
 * and obstacles end-to-end - replacing our older waypoint walker.
 *
 * Cleanup rule: on any phase failure, undo what we built (stations,
 * road, vehicle, depot) AND remove any NUTZ:bp:* signs we own.
 *
 * API 14, ASCII only.
 */

class NutzExecutorAI extends AIController {
    /* Pull the official Pathfinder.Road class via the NoAI library
     * system. Pathfinder.Road v3 internally pulls graph.aystar v4+.
     * Both must be installed via Online Content -> AI Library. */
    _PathfinderRoad = import("pathfinder.road", "Road", 3);
    job_id          = -1;
    eng_choice      = -1;
    stn_a_tile      = -1;
    stn_a_front     = -1;
    stn_b_tile      = -1;
    stn_b_front     = -1;
    depot_tile      = -1;
    depot_front     = -1;
    waypoints       = null;     /* array, ordered */
    /* Track what we built so we can undo on abort. */
    built_stn_a     = false;
    built_stn_b     = false;
    built_depot     = false;
    built_road      = null;     /* array of [a,b] tile pairs we laid */
    built_vehicle   = -1;
    /* Sign IDs we read so we can clean them up after the build. */
    owned_signs     = null;
    phase           = "wait";
    last_log        = "";
    routes_built    = 0;
    /* Job IDs we tried and aborted on. Without this we'd loop forever:
     * abort -> phase=wait -> LoadBlueprint sees same NUTZ:bp:<job>:* signs
     * (they're GS-owned, we can't AISign.RemoveSign them) -> retry -> abort.
     * Skip blacklisted jobs in LoadBlueprint so the executor moves on. */
    failed_jobs     = null;
    /* Mirror of failed_jobs but for jobs we already SUCCEEDED on. The GS
     * places NUTZ:bp:<job>:* signs once and never removes them, so without
     * this set the executor would re-pick bp1 forever and keep buying
     * extra buses on the same route. */
    succeeded_jobs  = null;
    grow_cursor     = 0;
    last_err_short  = "";
    /* Most recent Log() line. AILog goes nowhere on a macOS .app, so on
     * abort this is the only description of what actually failed. */
    last_log_msg    = "";
    /* Cached AICargo id of the passenger cargo. -1 until first lookup. */
    pax_cargo_id    = -1;
    /* Cached bus-stop coverage radius. 0 until first lookup. */
    pax_radius      = 0;

    function Start() {
        AILog.Info("Nutz Executor starting; idle until a NUTZ:bp blueprint arrives.");
        AICompany.SetName("Nutz Executor");
        AICompany.SetPresidentName("Blueprint Builder");
        AICompany.SetLoanAmount(AICompany.GetMaxLoanAmount());
        AIRoad.SetCurrentRoadType(AIRoad.ROADTYPE_ROAD);
        this.built_road = [];
        this.owned_signs = [];
        this.waypoints = [];
        this.failed_jobs = {};
        this.succeeded_jobs = {};

        while (true) {
            this.Drain();
            this.Step();
            this.Sleep(40);
        }
    }

    function Drain() {
        while (AIEventController.IsEventWaiting()) {
            AIEventController.GetNextEvent();
        }
    }

    function Log(s) {
        this.last_log_msg = s;
        if (s != this.last_log) { AILog.Info(s); this.last_log = s; }
    }

    /* Visible-from-admin-port phase reporter. OpenTTD .app on macOS drops
     * AILog.Info to /dev/null, so we encode current phase + job into the
     * company name; admin port pushes companies[].name on every poll. */
    function SetPhase(p) {
        local nm = "Nutz " + p + " j" + this.job_id + " r" + this.routes_built;
        if (nm.len() > 31) nm = nm.slice(0, 31);
        AICompany.SetName(nm);
    }

    function Step() {
        if (this.phase == "wait")     return this.PhaseWait();
        if (this.phase == "stations") return this.PhaseStations();
        if (this.phase == "road")     return this.PhaseRoad();
        if (this.phase == "depot")    return this.PhaseDepot();
        if (this.phase == "bus")      return this.PhaseBus();
        if (this.phase == "run")      return this.PhaseRun();
        if (this.phase == "abort")    return this.PhaseAbort();
        if (this.phase == "done")     return this.PhaseDone();
    }

    /* ---- sign parsing ---- */

    function StartsWith(s, prefix) {
        if (s == null) return false;
        if (s.len() < prefix.len()) return false;
        return s.slice(0, prefix.len()) == prefix;
    }

    function Split(s, sep) {
        local out = [];
        local cur = "";
        for (local i = 0; i < s.len(); i++) {
            local ch = s[i];
            if (ch == sep) { out.append(cur); cur = ""; }
            else { cur = cur + ch.tochar(); }
        }
        out.append(cur);
        return out;
    }

    function ToInt(s) {
        if (s == null || s.len() == 0) return -1;
        local neg = false; local i = 0;
        if (s[0] == '-') { neg = true; i = 1; }
        local v = 0;
        for (; i < s.len(); i++) {
            local ch = s[i];
            if (ch < '0' || ch > '9') return -1;
            v = v * 10 + (ch - '0');
        }
        return neg ? -v : v;
    }

    function ParseKV(s) {
        /* "fr=12345" -> ["fr", 12345] */
        local eq = -1;
        for (local i = 0; i < s.len(); i++) { if (s[i] == '=') { eq = i; break; } }
        if (eq < 0) return null;
        return [s.slice(0, eq), this.ToInt(s.slice(eq + 1))];
    }

    /* Look for any NUTZ:bp:<job>:* signs and load the lowest job number
     * as our active blueprint. Returns true if a complete blueprint was
     * found (start, end, depot, at least one waypoint). */
    function LoadBlueprint() {
        local sl = AISignList();
        local jobs = {};       /* job_id -> slot dict */
        local sign_owners = {}; /* job_id -> array of sign ids */
        foreach (sid, _ in sl) {
            local txt = AISign.GetName(sid);
            if (!this.StartsWith(txt, "NUTZ:bp:")) continue;
            local rest = txt.slice(8);  /* after "NUTZ:bp:" (8 chars) */
            local parts = this.Split(rest, ':');
            if (parts.len() < 2) continue;
            local job = this.ToInt(parts[0]);
            if (job < 0) continue;
            /* Skip jobs that already aborted - GS-owned bp signs persist
             * after we abort, so without this we'd re-pick the same job
             * and infinite-loop on ERR_AREA_NOT_CLEAR / similar. */
            if (this.failed_jobs != null && (job in this.failed_jobs)) continue;
            if (this.succeeded_jobs != null && (job in this.succeeded_jobs)) continue;
            local tag = parts[1];
            if (!(job in jobs)) {
                jobs[job] <- { wp = {}, eng = -1 };
                sign_owners[job] <- [];
            }
            sign_owners[job].append(sid);
            local slot = jobs[job];
            local tile = AISign.GetLocation(sid);
            if (tag == "S" && parts.len() >= 4) {
                local kv1 = this.ParseKV(parts[2]);
                local kv2 = this.ParseKV(parts[3]);
                if (kv1 != null && kv1[0] == "fr") {
                    slot.stn_a_tile <- tile;
                    slot.stn_a_front <- kv1[1];
                }
                if (kv2 != null && kv2[0] == "eg") slot.eng = kv2[1];
            } else if (tag == "E" && parts.len() >= 3) {
                local kv = this.ParseKV(parts[2]);
                if (kv != null && kv[0] == "fr") {
                    slot.stn_b_tile <- tile;
                    slot.stn_b_front <- kv[1];
                }
            } else if (tag == "D" && parts.len() >= 3) {
                local kv = this.ParseKV(parts[2]);
                if (kv != null && kv[0] == "fr") {
                    slot.depot_tile <- tile;
                    slot.depot_front <- kv[1];
                }
            } else if (tag == "W" && parts.len() >= 3) {
                local n = this.ToInt(parts[2]);
                if (n >= 0) slot.wp[n] <- tile;
            }
        }
        /* Pick the lowest complete job. */
        local picked = -1;
        foreach (job, slot in jobs) {
            if (!("stn_a_tile" in slot) || !("stn_b_tile" in slot)) continue;
            if (!("depot_tile" in slot)) continue;
            if (slot.wp.len() == 0) continue;
            if (picked < 0 || job < picked) picked = job;
        }
        if (picked < 0) return false;
        local s = jobs[picked];
        this.job_id = picked;
        this.stn_a_tile = s.stn_a_tile; this.stn_a_front = s.stn_a_front;
        this.stn_b_tile = s.stn_b_tile; this.stn_b_front = s.stn_b_front;
        this.depot_tile = s.depot_tile; this.depot_front = s.depot_front;
        this.eng_choice = s.eng;
        /* Sort waypoints by index. */
        local keys = [];
        foreach (k, _ in s.wp) keys.append(k);
        keys.sort();
        this.waypoints = [];
        foreach (k in keys) this.waypoints.append(s.wp[k]);
        this.owned_signs = sign_owners[picked];
        return true;
    }

    /* ---- phases ---- */

    function PhaseWait() {
        if (this.LoadBlueprint()) {
            this.Log("blueprint job=" + this.job_id
                     + " wp=" + this.waypoints.len());
            this.SetPhase("stn");
            this.phase = "stations";
        } else {
            this.Log("waiting for blueprint");
        }
    }

    /* Grow the two route towns by funding new buildings before placing
     * stations. Heightmap-derived towns can be tiny and produce zero
     * passengers - even a perfect route earns nothing if there are no
     * houses. Funding spawns 3-5 houses per call, instantly raising
     * passenger production. ~$500-1000 per call, called 6 times per
     * town = ~$10k investment per route, dwarfed by $200k starting funds. */
    function GrowTowns() {
        local ta = AITile.GetClosestTown(this.stn_a_tile);
        local tb = AITile.GetClosestTown(this.stn_b_tile);
        if (!AITown.IsValidTown(ta) || !AITown.IsValidTown(tb)) {
            this.Log("GrowTowns: invalid town id (skip)");
            return;
        }
        local funded = 0;
        /* AITown.PerformTownAction with TOWN_ACTION_FUND_BUILDINGS funds a
         * round of new buildings in the town (the GUI "Fund new buildings"
         * button equivalent). Each call costs ~$5k and adds a few houses.
         * Repeat 6 times per town for a meaningful pop bump. */
        for (local i = 0; i < 6; i++) {
            if (AITown.PerformTownAction(ta, AITown.TOWN_ACTION_FUND_BUILDINGS)) funded++;
            if (AITown.PerformTownAction(tb, AITown.TOWN_ACTION_FUND_BUILDINGS)) funded++;
            this.Sleep(20);
        }
        this.Log("GrowTowns: " + funded + "/12 fundings succeeded");
    }

    function PhaseStations() {
        this.GrowTowns();
        local picked_a = this.TryBuildStation(this.stn_a_tile, this.stn_a_front, "A");
        if (picked_a == null) {
            this.phase = "abort"; return;
        }
        this.stn_a_tile = picked_a[0]; this.stn_a_front = picked_a[1];
        this.built_stn_a = true;
        local picked_b = this.TryBuildStation(this.stn_b_tile, this.stn_b_front, "B");
        if (picked_b == null) {
            this.phase = "abort"; return;
        }
        this.stn_b_tile = picked_b[0]; this.stn_b_front = picked_b[1];
        this.built_stn_b = true;
        /* Prune waypoints that now overlap with shifted station tiles or
         * fronts. Without this, the "connect station_front -> waypoint[0]"
         * step fails ERR_AREA_NOT_CLEAR because waypoint[0] is now inside
         * the station footprint after the (1,0) shift. */
        local pruned = [];
        foreach (wp in this.waypoints) {
            if (wp == this.stn_a_tile || wp == this.stn_b_tile) continue;
            pruned.append(wp);
        }
        if (pruned.len() < this.waypoints.len()) {
            this.Log("pruned " + (this.waypoints.len() - pruned.len()) + " wp inside stations");
            this.waypoints = pruned;
        }
        this.Log("stations placed");
        this.SetPhase("rd");
        this.phase = "road";
    }

    /* Find the passenger cargo id (cached after first call). Without this,
     * GetCargoAcceptance can't be called - we'd be querying acceptance
     * for an unknown cargo. */
    function GetPaxCargoId() {
        if (this.pax_cargo_id != -1) return this.pax_cargo_id;
        local cl = AICargoList();
        foreach (c, _ in cl) {
            if (AICargo.HasCargoClass(c, AICargo.CC_PASSENGERS)) {
                this.pax_cargo_id = c;
                return c;
            }
        }
        return -1;
    }

    /* Passenger PRODUCTION alone - houses in the catchment. This is what
     * decides whether a stop can ever pick anyone up, so it is kept
     * separate from the blended ranking score. */
    function ProdTilePax(tile) {
        local pax = this.GetPaxCargoId();
        if (pax == -1) return 0;
        return AITile.GetCargoProduction(tile, pax, 1, 1, this.PaxRadius());
    }

    /* The catchment a bus stop actually covers. Scoring a wider radius than
     * the stop will cover is how a tile whose houses all sit one ring too
     * far out still scores well - the station gets built, covers nothing,
     * and the town supplies 0 passengers to it. Ask the game rather than
     * hardcoding, since the radius depends on the modified_catchment
     * setting. */
    function PaxRadius() {
        if (this.pax_radius > 0) return this.pax_radius;
        this.pax_radius = AIStation.GetCoverageRadius(AIStation.STATION_BUS_STOP);
        if (this.pax_radius <= 0) this.pax_radius = 3;
        return this.pax_radius;
    }

    /* Rank a candidate station tile: passenger production in the catchment
     * the stop will actually cover, plus acceptance (where passengers want
     * to GO) at half weight since stops are bidirectional. Ranking only -
     * eligibility is decided by production alone, see TryBuildStation. */
    function ScoreTilePax(tile) {
        local pax = this.GetPaxCargoId();
        if (pax == -1) return 0;
        local r = this.PaxRadius();
        local prod = AITile.GetCargoProduction(tile, pax, 1, 1, r);
        local acc  = AITile.GetCargoAcceptance(tile, pax, 1, 1, r);
        return prod * 2 + acc;
    }

    /* Pick the BEST station tile by passenger production, then build it.
     * Iterates a wide grid (up to 9 tile radius) centered on the planner's
     * pick; for each valid candidate (in-bounds, non-water, buildable)
     * computes pax-production over the catchment; tries BuildRoadStation
     * in descending-production order. Wider scan than the original 5x5
     * because LA-scale heightmap towns are spread thin and a small box
     * may miss the dense residential core entirely. Returns [tile, front]
     * or null. Also rejects candidates with production below MIN_PROD so
     * we don't anchor a route on a station guaranteed to starve. */
    function TryBuildStation(plan_tile, plan_front, label) {
        local px = AIMap.GetTileX(plan_tile); local py = AIMap.GetTileY(plan_tile);
        local fx = AIMap.GetTileX(plan_front); local fy = AIMap.GetTileY(plan_front);
        local fdx = fx - px; local fdy = fy - py;  /* front offset preserved */
        /* 81 candidates in a 9x9 ring, ordered closest-first so we prefer
         * tiles near the planner pick when production is similar. */
        local shifts = [];
        for (local r = 0; r <= 4; r++) {
            for (local sy = -r; sy <= r; sy++) {
                for (local sx = -r; sx <= r; sx++) {
                    if (r > 0 && sx*sx + sy*sy < r*r) continue;
                    shifts.append([sx, sy]);
                }
            }
        }
        /* Build (score, tile, front) candidate list. */
        local cands = [];
        foreach (s in shifts) {
            local sx = px + s[0]; local sy = py + s[1];
            local tile = AIMap.GetTileIndex(sx, sy);
            local front = AIMap.GetTileIndex(sx + fdx, sy + fdy);
            if (!AIMap.IsValidTile(tile) || !AIMap.IsValidTile(front)) continue;
            if (AITile.IsWaterTile(tile) || AITile.IsWaterTile(front)) continue;
            /* Stop B's search ring can reach stop A on a short route, and
             * both candidate tiles are demolished before the build. */
            if (this.IsOurStop(tile) || this.IsOurStop(front)) continue;
            local prod = this.ProdTilePax(tile);
            local score = this.ScoreTilePax(tile);
            cands.append([score, tile, front, s, prod]);
        }
        /* Squirrel array sort with comparator: descending by score. */
        cands.sort(function(a, b) {
            if (a[0] > b[0]) return -1;
            if (a[0] < b[0]) return 1;
            return 0;
        });
        /* Reject anything below MIN_PROD: a station with zero or near-zero
         * production will run a bus into the ground forever. Better to
         * fail this town pair and let the conductor pick a different one.
         *
         * Eligibility is decided by PRODUCTION ALONE. The blended score
         * (prod*2 + acc) still ranks candidates, but gating on it let a
         * tile with zero houses and high acceptance clear the bar - the
         * station gets built, no passenger ever boards, and the bus runs
         * at a loss until the company dies. Acceptance says where people
         * want to GO; only production says anyone is there to pick up. */
        local MIN_PROD = 8;
        local best_prod = 0;
        foreach (c in cands) if (c[4] > best_prod) best_prod = c[4];
        if (best_prod * 2 < MIN_PROD) {
            this.Log("stn " + label + " best production " + best_prod
                     + " too low (need " + (MIN_PROD / 2) + ")");
            return null;
        }
        local last_err = "no candidates";
        foreach (c in cands) {
            if (c[4] * 2 < MIN_PROD) continue;  /* no houses in catchment */
            local tile = c[1]; local front = c[2]; local s = c[3];
            AITile.DemolishTile(tile);
            AITile.DemolishTile(front);
            if (AIRoad.BuildRoadStation(tile, front,
                                         AIRoad.ROADVEHTYPE_BUS,
                                         AIStation.STATION_NEW)) {
                if (s[0] != 0 || s[1] != 0) {
                    this.Log("stn " + label + " placed (shift " + s[0] + "," + s[1] + " score=" + c[0] + ")");
                }
                return [tile, front];
            }
            last_err = AIError.GetLastErrorString();
        }
        this.Log("stn " + label + " fail: " + last_err);
        /* Encode the last error in the company name so we can see it from
         * admin port even though AILog.Info goes to /dev/null on macOS .app. */
        this.last_err_short = last_err.slice(0, 8);
        return null;
    }

    function PhaseRoad() {
        /* Build a road from station A's front to station B's front using
         * the official Pathfinder.Road library. The library handles
         * slopes, water (auto-bridge), tunnels, town roads, and obstacles
         * end-to-end. We ignore the planner's waypoints in this phase -
         * the pathfinder picks its own optimal path. */
        local pf = this._PathfinderRoad();
        /* Cost tuning rationale: on the LA heightmap the pathfinder kept
         * trying to build broken bridges over rivers when natural bridges
         * already exist nearby. Setting no_existing_road=120 makes the
         * existing road network nearly free vs new tiles, so the pathfinder
         * will detour to use natural bridges instead of spanning water on
         * its own. Bridge cost stays moderate so it can still build short
         * crossings if no detour exists. Slope=200 keeps it from zigzagging
         * up hills.  */
        pf.cost.tile = 100;
        pf.cost.turn = 50;
        pf.cost.no_existing_road = 120;
        pf.cost.slope = 200;
        pf.cost.bridge_per_tile = 100;
        pf.cost.tunnel_per_tile = 100;
        pf.cost.coast = 20;
        pf.cost.max_bridge_length = 12;
        pf.cost.max_tunnel_length = 10;
        pf.InitializePath([this.stn_a_front], [this.stn_b_front]);
        local path = false;
        local iters = 0;
        /* 200 iters * 50 nodes = 10k node expansions. Capped because on
         * dense LA-style heightmaps with full town generation, longer caps
         * just hang the build for many game months without finding a route
         * the pathfinder is happy with. Better to abort fast and let the
         * conductor try a different town pair. */
        while (path == false && iters < 200) {
            path = pf.FindPath(50);
            iters++;
            if (iters % 20 == 0) this.Sleep(1);
        }
        if (path == null) {
            this.Log("pathfinder: no route from station A to B (" + iters + " iters)");
            this.phase = "abort"; return;
        }
        if (path == false) {
            this.Log("pathfinder: timeout after " + iters + " iters");
            this.phase = "abort"; return;
        }
        /* Walk the path emitting BuildRoad / BuildBridge / BuildTunnel
         * for each consecutive pair, per the OpenTTD wiki sample. We call
         * BuildRoad in BOTH directions per segment so that road pieces
         * link bidirectionally - on slope transitions, calling only one
         * direction has been observed to leave a tile with the road piece
         * pointing only one way, blocking in-game vehicle pathfinding. */
        local segs = 0;
        while (path != null) {
            local par = path.GetParent();
            if (par != null) {
                local from = path.GetTile();
                local to = par.GetTile();
                if (AIMap.DistanceManhattan(from, to) == 1) {
                    local ok1 = AIRoad.BuildRoad(from, to);
                    if (!ok1 && AIError.GetLastError() != AIError.ERR_ALREADY_BUILT) {
                        this.Log("BuildRoad fwd failed: " + AIError.GetLastErrorString());
                        this.phase = "abort"; return;
                    }
                    AIRoad.BuildRoad(to, from);
                    this.built_road.append([from, to]);
                } else if (!AIBridge.IsBridgeTile(from) && !AITunnel.IsTunnelTile(from)) {
                    if (AIRoad.IsRoadTile(from)) AITile.DemolishTile(from);
                    if (AITunnel.GetOtherTunnelEnd(from) == to) {
                        if (!AITunnel.BuildTunnel(AIVehicle.VT_ROAD, from)) {
                            this.Log("BuildTunnel failed: " + AIError.GetLastErrorString());
                            this.phase = "abort"; return;
                        }
                    } else {
                        local bl = AIBridgeList_Length(AIMap.DistanceManhattan(from, to) + 1);
                        bl.Valuate(AIBridge.GetMaxSpeed);
                        bl.Sort(AIList.SORT_BY_VALUE, false);
                        if (!AIBridge.BuildBridge(AIVehicle.VT_ROAD, bl.Begin(), from, to)) {
                            this.Log("BuildBridge failed: " + AIError.GetLastErrorString());
                            this.phase = "abort"; return;
                        }
                    }
                    this.built_road.append([from, to]);
                }
                segs++;
            }
            path = par;
        }
        this.Log("road built via pathfinder (" + segs + " segs)");
        /* The pathfinder ran front-to-front; the stops themselves still
         * need a bit pointing at them or no bus can pull in. */
        this.ConnectStop(this.stn_a_tile, this.stn_a_front, "stn A");
        this.ConnectStop(this.stn_b_tile, this.stn_b_front, "stn B");
        /* Verify the road we just built actually connects stn_a_front to
         * stn_b_front by running a fresh Pathfinder.Road that's effectively
         * forbidden from laying new road (huge no_existing_road penalty).
         * If it can't find a path, our build had a gap and buses would
         * loop forever in town streets. Abort cleanly so the conductor
         * can pick a different town pair. */
        local vpf = this._PathfinderRoad();
        vpf.cost.tile = 100;
        vpf.cost.turn = 50;
        vpf.cost.no_existing_road = 100000;
        vpf.cost.slope = 200;
        vpf.cost.bridge_per_tile = 100000;
        vpf.cost.tunnel_per_tile = 100000;
        vpf.InitializePath([this.stn_a_front], [this.stn_b_front]);
        local vpath = false;
        local viters = 0;
        while (vpath == false && viters < 500) {
            vpath = vpf.FindPath(50);
            viters++;
            if (viters % 20 == 0) this.Sleep(1);
        }
        if (vpath == null) {
            this.Log("route VERIFY FAIL: built road has gap, aborting");
            this.phase = "abort"; return;
        }
        if (vpath == false) {
            this.Log("route VERIFY timeout - assuming OK");
        } else {
            this.Log("route verify OK");
        }
        /* Stronger check: walk our actual built_road list and assert
         * AreRoadTilesConnected on each pair. This is the vehicle-level
         * connectivity test - the in-game vehicle pathfinder uses the
         * same logic. We've seen the Pathfinder.Road verify above pass
         * while buses still get stuck at slope transitions because the
         * road pieces don't actually link for vehicle traversal. */
        local broken = 0;
        foreach (pair in this.built_road) {
            local a = pair[0]; local b = pair[1];
            if (AIMap.DistanceManhattan(a, b) != 1) continue;  /* skip bridges */
            /* AreRoadTilesConnected is asymmetric: can drive a->b but maybe
             * not b->a. Bus needs both for round-trip orders. */
            if (!AIRoad.AreRoadTilesConnected(a, b)) broken++;
            else if (!AIRoad.AreRoadTilesConnected(b, a)) broken++;
        }
        /* Warn-only: drive-verify reports unlinked pairs but does not
         * abort. We've seen it fire false positives for town-junction
         * tiles that vehicles can actually navigate via secondary
         * connectors. The Pathfinder.Road verify above is the hard gate. */
        if (broken > 0) {
            this.Log("route drive-verify: " + broken + " unlinked (warn)");
        } else {
            this.Log("route drive-verify OK");
        }
        this.SetPhase("dpt");
        this.phase = "depot";
    }

    function PhaseDepot() {
        /* Same fan-out pattern as TryBuildStation. Try the planned tile
         * plus 8 cardinal-shifted neighbours; demolish each candidate
         * before BuildRoadDepot. Solves ERR_AREA_NOT_CLEAR when the
         * planner-picked depot tile lands on a town building. */
        local px = AIMap.GetTileX(this.depot_tile);
        local py = AIMap.GetTileY(this.depot_tile);
        local fx = AIMap.GetTileX(this.depot_front);
        local fy = AIMap.GetTileY(this.depot_front);
        local fdx = fx - px; local fdy = fy - py;
        /* A 1-tile ring is not enough inside a real town: by the time we
         * get here the planner's depot tile and all eight neighbours are
         * usually taken by the stations and the road we just laid, and
         * every candidate comes back ERR_AREA_NOT_CLEAR. Search a 2-tile
         * ring instead, nearest-first, and skip tiles that are still not
         * buildable after the demolish attempt - demolish silently fails
         * on our own road, on stations, and on protected town property. */
        local shifts = [];
        for (local dx = -2; dx <= 2; dx++) {
            for (local dy = -2; dy <= 2; dy++) shifts.append([dx, dy]);
        }
        shifts.sort(function(a, b) {
            local da = (a[0] < 0 ? -a[0] : a[0]) + (a[1] < 0 ? -a[1] : a[1]);
            local db = (b[0] < 0 ? -b[0] : b[0]) + (b[1] < 0 ? -b[1] : b[1]);
            if (da < db) return -1;
            if (da > db) return 1;
            return 0;
        });
        local last_err = "no candidates";
        foreach (s in shifts) {
            local sx = px + s[0]; local sy = py + s[1];
            local tile = AIMap.GetTileIndex(sx, sy);
            local front = AIMap.GetTileIndex(sx + fdx, sy + fdy);
            if (!AIMap.IsValidTile(tile) || !AIMap.IsValidTile(front)) continue;
            if (AITile.IsWaterTile(tile) || AITile.IsWaterTile(front)) continue;
            /* Both tiles get demolished below, so BOTH have to be cleared of
             * our own infrastructure first. Guarding only `tile` let the
             * depot hunt bulldoze a station we had just built via its
             * `front`, leaving a one-station route whose two orders point at
             * the same stop: the buses shuttle nowhere and earn nothing. */
            if (this.IsOurStop(tile) || this.IsOurStop(front)) continue;
            AITile.DemolishTile(tile);
            AITile.DemolishTile(front);
            if (!AITile.IsBuildable(tile)) { last_err = "tile not clear"; continue; }
            if (AIRoad.BuildRoadDepot(tile, front)) {
                this.depot_tile = tile;
                this.depot_front = front;
                this.built_depot = true;
                this.ConnectStop(tile, front, "depot");
                if (s[0] != 0 || s[1] != 0) {
                    this.Log("depot built (shift " + s[0] + "," + s[1] + ")");
                } else {
                    this.Log("depot built");
                }
                /* CRITICAL: connect depot front to nearest station front
                 * via Pathfinder.Road. Without this, the depot is an
                 * isolated parking spot - the bus exits onto its
                 * depot_front tile but there's no road there, so it
                 * loops "Heading for X" forever without making progress.
                 * Pathfinder picks the shorter of (front -> stn_a_front,
                 * front -> stn_b_front). */
                local pf = this._PathfinderRoad();
                pf.cost.tile = 100;
                pf.cost.turn = 50;
                pf.cost.no_existing_road = 40;
                pf.cost.slope = 200;
                pf.InitializePath([front], [this.stn_a_front, this.stn_b_front]);
                local dpath = false;
                local di = 0;
                while (dpath == false && di < 500) {
                    dpath = pf.FindPath(50);
                    di++;
                    if (di % 20 == 0) this.Sleep(1);
                }
                if (dpath == null || dpath == false) {
                    this.Log("depot connect: no path (" + di + " iters)");
                    /* Don't abort - bus can sometimes still navigate via
                     * existing town roads. Worth trying. */
                } else {
                    /* Bidirectional BuildRoad on the depot connector. Depot
                     * tiles built single-direction left buses stuck at the
                     * depot exit because the road piece at front_b only
                     * pointed one way. */
                    local broken_dep = 0;
                    while (dpath != null) {
                        local par = dpath.GetParent();
                        if (par != null) {
                            local from = dpath.GetTile();
                            local to = par.GetTile();
                            if (AIMap.DistanceManhattan(from, to) == 1) {
                                AIRoad.BuildRoad(from, to);
                                AIRoad.BuildRoad(to, from);
                                if (!AIRoad.AreRoadTilesConnected(from, to) ||
                                    !AIRoad.AreRoadTilesConnected(to, from)) {
                                    broken_dep++;
                                }
                                this.built_road.append([from, to]);
                            }
                        }
                        dpath = par;
                    }
                    if (broken_dep > 0) {
                        this.Log("depot connector: " + broken_dep + " unlinked (warn)");
                    } else {
                        this.Log("depot connected to nearest station");
                    }
                }
                this.SetPhase("bus");
                this.phase = "bus";
                return;
            }
            last_err = AIError.GetLastErrorString();
        }
        this.Log("depot fail (all ring tiles): " + last_err);
        this.phase = "abort";
    }

    function PhaseBus() {
        local cl = AICargoList();
        cl.Valuate(AICargo.HasCargoClass, AICargo.CC_PASSENGERS);
        cl.KeepValue(1);
        if (cl.IsEmpty()) { this.Log("no PASS cargo"); this.phase = "abort"; return; }
        local cargo = cl.Begin();

        local engine = this.eng_choice;
        if (engine < 0) {
            local el = AIEngineList(AIVehicle.VT_ROAD);
            el.Valuate(AIEngine.IsBuildable);          el.KeepValue(1);
            el.Valuate(AIEngine.GetRoadType);          el.KeepValue(AIRoad.ROADTYPE_ROAD);
            el.Valuate(AIEngine.CanRefitCargo, cargo); el.KeepValue(1);
            el.Valuate(AIEngine.GetMaxSpeed);
            el.Sort(AIList.SORT_BY_VALUE, false);
            if (el.IsEmpty()) { this.Log("no eng"); this.phase = "abort"; return; }
            engine = el.Begin();
        } else {
            if (!AIEngine.IsBuildable(engine)) {
                this.Log("blueprint engine " + engine + " not buildable");
                this.phase = "abort"; return;
            }
        }

        local v = AIVehicle.BuildVehicle(this.depot_tile, engine);
        if (!AIVehicle.IsValidVehicle(v)) {
            this.Log("buy fail: " + AIError.GetLastErrorString());
            this.phase = "abort"; return;
        }
        this.built_vehicle = v;
        AIVehicle.RefitVehicle(v, cargo);
        AIOrder.AppendOrder(v, this.stn_a_tile, AIOrder.OF_NON_STOP_INTERMEDIATE);
        AIOrder.AppendOrder(v, this.stn_b_tile, AIOrder.OF_NON_STOP_INTERMEDIATE);
        AIVehicle.StartStopVehicle(v);
        /* Fleet scaling: clones share orders. Held at 1 while the
         * road-connectivity loop was under debug; that is fixed now (stops
         * are explicitly connected to their front tile), and one bus cannot
         * clear the queue a healthy stop builds up - 500+ passengers pile
         * on while it drives. Back to 3. */
        local FLEET_SIZE = 3;
        for (local i = 1; i < FLEET_SIZE; i++) {
            local cv = AIVehicle.CloneVehicle(this.depot_tile, v, true);
            if (!AIVehicle.IsValidVehicle(cv)) {
                this.Log("clone " + i + " fail: " + AIError.GetLastErrorString());
                break;
            }
            AIVehicle.StartStopVehicle(cv);
            this.Log("fleet+1 vehicle=" + cv);
        }
        this.routes_built++;
        this.Log("ROUTE LIVE job=" + this.job_id + " vehicle=" + v);
        if (this.job_id >= 0 && this.succeeded_jobs != null) {
            this.succeeded_jobs[this.job_id] <- 1;
        }
        this.SetPhase("LIVE");
        this.phase = "run";
    }

    function PhaseRun() {
        /* Done; clean up our blueprint signs so the next blueprint can
         * land without ambiguity, then idle. */
        this.RemoveOwnedSigns();
        this.phase = "done";
    }

    /* Encode bus state + station waiting passengers + bus tile coords into
     * the company name so we can read it from admin port (AILog is not
     * captured on macOS .app). 31-char limit. Format examples:
     *   "Nutz R wA12 wB05 t=137,99 j1"  - bus running, queues filling
     *   "Nutz S wA0 wB0 j1"              - bus stopped (Lost)
     *   "Nutz @ wA8 wB0 j1"              - bus at a station */
    function DumpDiag() {
        if (this.built_vehicle == -1 || !AIVehicle.IsValidVehicle(this.built_vehicle)) {
            return;
        }
        local pax = this.GetPaxCargoId();
        local sa = AIStation.GetStationID(this.stn_a_tile);
        local sb = AIStation.GetStationID(this.stn_b_tile);
        local wA = (pax >= 0 && AIStation.IsValidStation(sa))
            ? AIStation.GetCargoWaiting(sa, pax) : -1;
        local wB = (pax >= 0 && AIStation.IsValidStation(sb))
            ? AIStation.GetCargoWaiting(sb, pax) : -1;
        local st = AIVehicle.GetState(this.built_vehicle);
        local stChar = "?";
        if      (st == AIVehicle.VS_RUNNING)    stChar = "R";
        else if (st == AIVehicle.VS_STOPPED)    stChar = "S";
        else if (st == AIVehicle.VS_IN_DEPOT)   stChar = "D";
        else if (st == AIVehicle.VS_AT_STATION) stChar = "@";
        else if (st == AIVehicle.VS_BROKEN)     stChar = "B";
        else if (st == AIVehicle.VS_CRASHED)    stChar = "X";
        local loc = AIVehicle.GetLocation(this.built_vehicle);
        local lx = AIMap.GetTileX(loc) % 1000;
        local ly = AIMap.GetTileY(loc) % 1000;
        local sp = AIVehicle.GetCurrentSpeed(this.built_vehicle);
        /* Also probe MAX waiting across ALL stations on the map (any
         * company). If max=0, the map's towns produce no passengers and
         * every bus loses regardless of routing. STATION_ANY iterates
         * every station including ones built by other companies. */
        local sl = AIStationList(AIStation.STATION_ANY);
        local maxW = 0;
        foreach (sid, _ in sl) {
            if (pax >= 0) {
                local w = AIStation.GetCargoWaiting(sid, pax);
                if (w > maxW) maxW = w;
            }
        }
        /* wA/wB are the whole point of this diagnostic - they say whether
         * passengers are queueing at OUR two stops, which distinguishes
         * "route works, bus is slow" from "nobody is there to pick up".
         * They were computed and then dropped from the string. */
        local nm = "N" + stChar + sp + " a" + wA + " b" + wB
                   + " p" + this.ProdTilePax(this.stn_a_tile)
                   + " r" + this.PaxRadius()
                   + " o" + AIOrder.GetOrderCount(this.built_vehicle);
        if (nm.len() > 31) nm = nm.slice(0, 31);
        AICompany.SetName(nm);
    }

    function PhaseDone() {
        /* Idle: dump diagnostic each tick into company name, and check for
         * a new blueprint. We DO NOT call ResetState here unless a new
         * blueprint actually arrives - otherwise we'd lose track of the
         * built_vehicle / station tiles needed for diagnostics. */
        this.DumpDiag();
        /* Background passive-growth: round-robin fund one town per idle
         * tick so the entire map's population grows over real time. Skips
         * towns already large enough to produce passengers without help.
         * Cycles via this.grow_cursor. ~$500-1000 per call - small drain
         * relative to bus revenue. */
        this.GrowAllTowns();
        if (this.LoadBlueprint()) {
            local saved_routes = this.routes_built;
            this.ResetState();
            this.routes_built = saved_routes;
            /* ResetState() wiped the tiles the first LoadBlueprint() just
             * set, so this reload is what actually arms the build. If it
             * comes back false we would march into PhaseStations with
             * job_id -1 and tile -1 and fail ERR_PRECONDITION_FAILED on
             * every candidate, so stay idle instead. */
            if (!this.LoadBlueprint()) {
                this.Log("reload after reset found no blueprint - staying idle");
                this.phase = "done";
                return;
            }
            this.Log("next blueprint job=" + this.job_id);
            this.phase = "stations";
        }
    }

    function GrowAllTowns() {
        local tl = AITownList();
        if (tl.IsEmpty()) return;
        local towns = [];
        foreach (t, _ in tl) towns.append(t);
        towns.sort();
        if (this.grow_cursor < 0 || this.grow_cursor >= towns.len()) {
            this.grow_cursor = 0;
        }
        local t = towns[this.grow_cursor];
        this.grow_cursor++;
        if (AITown.GetPopulation(t) < 5000) {
            AITown.PerformTownAction(t, AITown.TOWN_ACTION_FUND_BUILDINGS);
        }
    }

    function PhaseAbort() {
        /* Surface WHY, not just THAT. AILog goes nowhere on a macOS .app,
         * so the company name is the one channel the admin port can read.
         * Snapshot the reason BEFORE logging the abort itself, or the abort
         * line overwrites the failure it is reporting. */
        local why = this.last_log_msg != "" ? this.last_log_msg
                                            : this.last_err_short;
        /* The company name caps at 31 chars and the useful part of a log
         * line is its tail ("depot fail (all ring tiles): ERR_..."), so keep
         * what follows the last ": " rather than the leading prose. */
        local cut = -1;
        for (local i = 0; i < why.len(); i++) if (why[i] == ':') cut = i;
        if (cut >= 0 && cut + 2 <= why.len()) why = why.slice(cut + 1);
        while (why.len() > 0 && why[0] == ' ') why = why.slice(1);
        this.Log("ABORT job=" + this.job_id + " - rolling back");
        local nm = why == "" ? "ABRT" : "A:" + why;
        if (nm.len() > 31) nm = nm.slice(0, 31);
        AICompany.SetName(nm);
        /* Blacklist this job_id so LoadBlueprint skips it next tick. The
         * GS-placed NUTZ:bp:<job>:* signs are not ours to remove, but we
         * still need to stop trying. */
        if (this.job_id >= 0 && this.failed_jobs != null) {
            this.failed_jobs[this.job_id] <- 1;
        }
        if (this.built_vehicle != -1 && AIVehicle.IsValidVehicle(this.built_vehicle)) {
            AIVehicle.SendVehicleToDepot(this.built_vehicle);
            AIVehicle.SellVehicle(this.built_vehicle);
        }
        if (this.built_depot && this.depot_tile != -1) {
            AITile.DemolishTile(this.depot_tile);
        }
        /* Reverse-order road removal so we don't strand sub-segments. */
        for (local i = this.built_road.len() - 1; i >= 0; i--) {
            local seg = this.built_road[i];
            AIRoad.RemoveRoad(seg[0], seg[1]);
        }
        if (this.built_stn_a && this.stn_a_tile != -1) {
            AIRoad.RemoveRoadStation(this.stn_a_tile);
        }
        if (this.built_stn_b && this.stn_b_tile != -1) {
            AIRoad.RemoveRoadStation(this.stn_b_tile);
        }
        this.RemoveOwnedSigns();
        this.ResetState();
        this.phase = "wait";
    }

    function RemoveOwnedSigns() {
        if (this.owned_signs == null) return;
        foreach (sid in this.owned_signs) {
            if (AISign.GetName(sid) != null) AISign.RemoveSign(sid);
        }
        this.owned_signs = [];
    }

    /* Connect a bay stop (or depot) to its front tile.
     *
     * BuildRoadStation/BuildRoadDepot give the STOP tile a road bit facing
     * its front, but the front tile only gets a matching bit back if it
     * already had road at that moment. We build stations first and lay the
     * road afterwards, so the pathfinder writes the front tile's bits along
     * its own path and none of them point at the stop. Two adjacent road
     * tiles whose bits do not face each other are not connected: YAPF can
     * never enter, the bus circles forever at full speed ("Lost"), the
     * station stays empty, and the town supplies 0 passengers.
     *
     * Cheap to call when it is already connected - that is just a no-op. */
    /* True if the tile carries a road stop or depot, or is one of the two
     * station tiles of the route we are mid-way through building. Anything
     * that answers yes must never be demolished to make room. */
    function IsOurStop(tile) {
        if (tile == -1) return false;
        if (tile == this.stn_a_tile || tile == this.stn_b_tile) return true;
        if (tile == this.depot_tile && this.built_depot) return true;
        return AIRoad.IsRoadStationTile(tile) || AIRoad.IsRoadDepotTile(tile);
    }

    function ConnectStop(stop_tile, front_tile, label) {
        if (stop_tile == -1 || front_tile == -1) return false;
        if (AIRoad.AreRoadTilesConnected(front_tile, stop_tile)) return true;
        local ok = AIRoad.BuildRoad(front_tile, stop_tile);
        if (!ok && AIError.GetLastError() == AIError.ERR_ALREADY_BUILT) ok = true;
        if (!ok) {
            ok = AIRoad.BuildRoad(stop_tile, front_tile);
            if (!ok && AIError.GetLastError() == AIError.ERR_ALREADY_BUILT) ok = true;
        }
        if (ok) {
            this.built_road.append([front_tile, stop_tile]);
            this.Log("connected " + label + " to its front");
        } else {
            this.Log("connect " + label + " fail: " + AIError.GetLastErrorString());
        }
        return ok;
    }

    /* Walk from tile a to tile b one step at a time, building road on
     * each segment. Handles waypoint pairs that are >1 tile apart, corners,
     * and mixed land/water spans (BuildRoad on land, TryBuildBridge on
     * water). Returns true if every step succeeded.
     *
     * BuildRoad(a, b) only works if a/b are adjacent OR on the same axis;
     * the executor's old PhaseRoad loop hit silent gaps whenever the
     * planner spaced waypoints across L-bends or 2+ tiles apart. */
    function ConnectTiles(a, b) {
        if (a == b) return true;
        local cx = AIMap.GetTileX(a); local cy = AIMap.GetTileY(a);
        local bx = AIMap.GetTileX(b); local by = AIMap.GetTileY(b);
        local steps = 0;
        while ((cx != bx || cy != by) && steps < 80) {
            steps++;
            local nx = cx; local ny = cy;
            if (cx < bx)      nx = cx + 1;
            else if (cx > bx) nx = cx - 1;
            else if (cy < by) ny = cy + 1;
            else if (cy > by) ny = cy - 1;
            local cur = AIMap.GetTileIndex(cx, cy);
            local nxt = AIMap.GetTileIndex(nx, ny);
            if (AIRoad.AreRoadTilesConnected(cur, nxt)) {
                cx = nx; cy = ny; continue;
            }
            local ok = AIRoad.BuildRoad(cur, nxt) || AIRoad.BuildRoad(nxt, cur);
            if (!ok) {
                AITile.DemolishTile(nxt);
                ok = AIRoad.BuildRoad(cur, nxt) || AIRoad.BuildRoad(nxt, cur);
            }
            if (!ok) {
                if (!this.TryBuildBridge(cur, nxt)) return false;
            } else {
                this.built_road.append([cur, nxt]);
            }
            cx = nx; cy = ny;
        }
        return true;
    }

    /* Span water (or any non-buildable terrain) between adjacent waypoint
     * tiles a and b. Walks outward from a in the direction of b, finding
     * the first land tile past the water, and builds a road bridge there.
     * Returns true if a bridge of any length was successfully built. */
    function TryBuildBridge(a, b) {
        local ax = AIMap.GetTileX(a); local ay = AIMap.GetTileY(a);
        local bx = AIMap.GetTileX(b); local by = AIMap.GetTileY(b);
        local dx = 0; local dy = 0;
        if (bx > ax) dx = 1; else if (bx < ax) dx = -1;
        else if (by > ay) dy = 1; else if (by < ay) dy = -1;
        else return false;
        /* OpenTTD bridges need BOTH heads at the same height. Probe ahead
         * for any non-water tile whose height matches `a` and is not too
         * far. Without this we hit ERR_BRIDGE_HEADS_NOT_ON_SAME_HEIGHT
         * because the far shore is usually a different elevation than
         * the near bank. We try increasing lengths and pick the first
         * matching-height land tile. */
        local a_h = AITile.GetMaxHeight(a);
        local cx = ax + dx; local cy = ay + dy;
        for (local len = 2; len <= 16; len++) {
            local end = AIMap.GetTileIndex(cx, cy);
            if (AIMap.IsValidTile(end) && !AITile.IsWaterTile(end)) {
                local end_h = AITile.GetMaxHeight(end);
                if (end_h == a_h) {
                    local types = AIBridgeList_Length(len);
                    if (types != null && !types.IsEmpty()) {
                        types.Valuate(AIBridge.GetMaxSpeed);
                        types.Sort(AIList.SORT_BY_VALUE, false);
                        local bridge_id = types.Begin();
                        if (AIBridge.BuildBridge(AIVehicle.VT_ROAD, bridge_id, a, end)) {
                            this.Log("bridge built " + len + " tiles h=" + a_h);
                            return true;
                        }
                    }
                }
            }
            cx += dx; cy += dy;
        }
        /* Fallback: try lowering/raising the far end via Demolish then
         * level it. If the far shore is one tile higher, demolish + the
         * implicit terraform during build often equalises. Try once with
         * any non-water tile at any height. */
        cx = ax + dx; cy = ay + dy;
        for (local len = 2; len <= 16; len++) {
            local end = AIMap.GetTileIndex(cx, cy);
            if (AIMap.IsValidTile(end) && !AITile.IsWaterTile(end)) {
                AITile.DemolishTile(end);
                local types = AIBridgeList_Length(len);
                if (types != null && !types.IsEmpty()) {
                    types.Valuate(AIBridge.GetMaxSpeed);
                    types.Sort(AIList.SORT_BY_VALUE, false);
                    local bridge_id = types.Begin();
                    if (AIBridge.BuildBridge(AIVehicle.VT_ROAD, bridge_id, a, end)) {
                        this.Log("bridge built " + len + " tiles (demolished far)");
                        return true;
                    }
                }
            }
            cx += dx; cy += dy;
        }
        return false;
    }

    function ResetState() {
        this.job_id = -1; this.eng_choice = -1;
        this.stn_a_tile = -1; this.stn_a_front = -1;
        this.stn_b_tile = -1; this.stn_b_front = -1;
        this.depot_tile = -1; this.depot_front = -1;
        this.waypoints = [];
        this.built_stn_a = false; this.built_stn_b = false;
        this.built_depot = false; this.built_road = [];
        this.built_vehicle = -1;
        this.owned_signs = [];
    }
}

"""Profit-seeking route conductor for DLF Executor.

Replaces the 5 spammy personas (Player/Greedy/Green/Cautious/Freight)
with a single coordinated planner. Each tick:

  1. Read gs_state - towns ranked by population, stations counted per town
  2. Skip saturated towns (>=2 stations)
  3. Pick highest-pop pair within 8-30 tiles
  4. Skip pairs we already dispatched
  5. Call plan_route via send_gs - executor reads bp signs and builds
  6. Wait for the build (next blueprint job_id) to complete or abort

Cheap: pure Python heuristic, no LLM. Run alongside run_openttd.py + executor.

Usage:
    cd ~/dlf-openttd/agent
    python3 -m sandbox.conductor

Arguments:
    --interval 30      seconds between dispatches (default 30)
    --max-jobs 10      stop after N successful + failed dispatches (default unlimited)
    --min-distance 8
    --max-distance 30
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from src.emulators.openttd.admin_client import OpenTTDAdminClient
except ModuleNotFoundError:
    from admin_client import OpenTTDAdminClient

try:
    from sandbox.planner import plan_route
except ImportError:
    from planner import plan_route   # type: ignore


def _tile_xy(tile: int, map_w: int) -> tuple[int, int]:
    return tile % map_w, tile // map_w


def _manhattan(a: dict, b: dict, map_w: int) -> int:
    ax, ay = _tile_xy(int(a["tile"]), map_w)
    bx, by = _tile_xy(int(b["tile"]), map_w)
    return abs(ax - bx) + abs(ay - by)


def _saturation(state: dict) -> dict[str, int]:
    """Group station names by leading word (the town name): 'Irvine',
    'Irvine North', 'Irvine Transfer' all count toward 'Irvine'."""
    out: dict[str, int] = {}
    for s in state.get("stations") or []:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        head = name.split(" ", 1)[0]
        out[head] = out.get(head, 0) + 1
    return out


def _score(town_a: dict, town_b: dict) -> float:
    """Estimate revenue potential: smaller of the two pop * 0.5 + bigger * 1.
    Heavier weight on the larger town since it accumulates more passengers."""
    pa = int(town_a.get("pop", 0))
    pb = int(town_b.get("pop", 0))
    return max(pa, pb) + 0.5 * min(pa, pb)


def _pick_intra_town(state: dict, dispatched: set[tuple[str, str]],
                     sat_cap: int) -> dict | None:
    """Pick the highest-pop town that isn't saturated. Intra-mode allows
    REPEATED picks per town (different from inter-mode) so we layer up
    multiple short routes inside the same dense town - mirrors the user's
    8-15 station Claremont/Montclair clusters. The sat_cap caps total
    stations per town so we don't infinite-stack."""
    towns = state.get("towns_top") or []
    sat = _saturation(state)
    for t in sorted(towns, key=lambda t: -int(t.get("pop", 0))):
        name = t.get("name", "")
        if sat.get(name, 0) >= sat_cap:
            continue
        return t
    return None


def _pick_pair(state: dict, dispatched: set[tuple[str, str]],
                min_d: int, max_d: int, sat_cap: int) -> tuple[dict, dict] | None:
    towns = state.get("towns_top") or []
    if len(towns) < 2:
        return None
    map_w = int((state.get("map") or {}).get("sizeX") or 256)
    sat = _saturation(state)
    candidates = []
    for i, ta in enumerate(towns):
        if sat.get(ta.get("name", ""), 0) >= sat_cap:
            continue
        for tb in towns[i + 1:]:
            if sat.get(tb.get("name", ""), 0) >= sat_cap:
                continue
            d = _manhattan(ta, tb, map_w)
            if d < min_d or d > max_d:
                continue
            key = tuple(sorted([ta.get("name", ""), tb.get("name", "")]))
            if key in dispatched:
                continue
            candidates.append((_score(ta, tb), ta, tb, d))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, a, b, d = candidates[0]
    return a, b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=30.0,
                    help="seconds between dispatches (default 30)")
    ap.add_argument("--max-jobs", type=int, default=0,
                    help="stop after N dispatches; 0 = unlimited")
    # Min 14 tiles avoids the "both stations land in the same catchment"
    # bug for adjacent real-world cities (Tustin/Irvine, Long Beach/LA).
    # With EDGE_OFFSET=5 in the planner, total >= 14 leaves >=4 tiles
    # between the two stations - enough for OpenTTD to name them after
    # different towns.
    ap.add_argument("--min-distance", type=int, default=14)
    ap.add_argument("--max-distance", type=int, default=30)
    ap.add_argument("--sat-cap", type=int, default=2,
                    help="max stations per town before we skip it")
    ap.add_argument("--intra-town", action="store_true",
                    help="dispatch intra-town routes (2 stations inside the "
                         "same big town, mirrors the user's working pattern). "
                         "When set, ignores --min/max-distance and just picks "
                         "the highest-pop town not yet saturated.")
    ap.add_argument("--validate", action="store_true",
                    help="validate every blueprint in a sandbox OpenTTD "
                         "before dispatching to live. Slower (~30s/pick) but "
                         "filters unbuildable routes.")
    ap.add_argument("--scenario", default="la",
                    help="scenario handle for sandbox (must match live game)")
    args = ap.parse_args()

    c = OpenTTDAdminClient(name="Conductor")
    try:
        c.connect()
    except Exception as e:
        print(f"connect: {e}")
        return 1
    time.sleep(2)

    dispatched: set[tuple[str, str]] = set()
    job_counter = 0
    validator = None
    if args.validate:
        try:
            from sandbox.blueprint_validator import BlueprintValidator
        except ImportError:
            from blueprint_validator import BlueprintValidator  # type: ignore
        print(f"[conductor] starting sandbox validator for '{args.scenario}'...")
        validator = BlueprintValidator(handle=args.scenario)
        try:
            validator.start()
        except Exception as e:
            print(f"[conductor] sandbox FAILED to start: {e}")
            print(f"[conductor] continuing without validation")
            validator = None
    print(f"[conductor] running. dispatch every {args.interval}s, "
          f"distance {args.min_distance}-{args.max_distance}, sat cap {args.sat_cap}, "
          f"validate={'on' if validator else 'off'}")
    while True:
        state = c.get_gs_state() or {}
        if not state:
            print("[conductor] no GS state (bridge GS not loaded?). sleeping.")
            time.sleep(args.interval)
            continue
        if args.intra_town:
            picked = _pick_intra_town(state, dispatched, args.sat_cap)
            if picked is None:
                print("[conductor] no eligible intra-town this tick.")
                time.sleep(args.interval)
                continue
            a = picked; b = picked
        else:
            pair = _pick_pair(state, dispatched,
                               args.min_distance, args.max_distance, args.sat_cap)
            if pair is None:
                print("[conductor] no eligible pair this tick.")
                time.sleep(args.interval)
                continue
            a, b = pair
        job_counter += 1
        a_name = a.get("name", ""); b_name = b.get("name", "")
        key = tuple(sorted([a_name, b_name]))
        dispatched.add(key)

        # Validate in sandbox first if enabled - only dispatch live on
        # confirmed buildable routes. Saves money and map clutter.
        if validator is not None:
            print(f"[conductor] sandbox-validate {a_name} -> {b_name}...")
            res = validator.validate(a_name, b_name, timeout_s=180)
            if not res.get("ok"):
                msg = (f"Conductor: SANDBOX REJECT {a_name}->{b_name}: "
                       f"{res.get('reason','')[:80]}")
                print(f"[conductor] {msg}")
                try:
                    c.rcon(f'say "{msg.replace(chr(34), chr(39))[:200]}"')
                except Exception:
                    pass
                time.sleep(args.interval)
                continue
            # Sandbox passed - re-plan for live (job_id keeps incrementing).
            bp, notes = plan_route(state, a_name, b_name, job_id=job_counter)
            if bp is None:
                print(f"[conductor] live re-plan fail: {notes}")
                time.sleep(args.interval)
                continue
        else:
            bp, notes = plan_route(state, a_name, b_name, job_id=job_counter)
            if bp is None:
                print(f"[conductor] plan {a_name}->{b_name} fail: {notes}")
                time.sleep(args.interval)
                continue

        try:
            c.send_gs(bp.to_admin_cmd())
            tag = "VERIFIED" if validator is not None else "bp"
            msg = (f"Conductor: {tag}{bp.job_id} {a_name}({a.get('pop')}) -> "
                   f"{b_name}({b.get('pop')}) {notes}")
            print(f"[conductor] {msg}")
            c.rcon(f'say "{msg.replace(chr(34), chr(39))[:200]}"')
        except Exception as e:
            print(f"[conductor] send fail: {e}")
        if args.max_jobs and job_counter >= args.max_jobs:
            print(f"[conductor] hit --max-jobs {args.max_jobs}, stopping.")
            break
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nbye")

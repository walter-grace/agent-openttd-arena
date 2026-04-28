"""Heuristic Blueprint planner.

Given the GS state JSON pushed by nutz_bridge and two town names, emit a
candidate Blueprint that the executor can attempt to build. This is the
*dumb* baseline: straight-line manhattan road, station offset 5 tiles
from town center toward the partner, depot at station-A + (2,0).

The MVP value of this planner over current dispatch_build:
  - Plan is computed Python-side BEFORE any tile is touched.
  - Executor either builds it cleanly or aborts cleanly. No half-roads.
  - Output is structured (tiles, not town names) so a sandbox search
    can swap in a smarter planner later without touching the executor.

Coordinates: OpenTTD tile indices are y * map_width + x. We pull
map_width from gs_state.map.sizeX so we can do offset arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import Dict, List, Optional, Tuple

try:
    from .blueprint import Blueprint
except ImportError:
    from blueprint import Blueprint  # type: ignore


@dataclass
class _Town:
    id: int
    name: str
    tile: int
    pop: int


def _tiles_to_xy(tile: int, map_width: int) -> Tuple[int, int]:
    return tile % map_width, tile // map_width


def _xy_to_tile(x: int, y: int, map_width: int) -> int:
    return y * map_width + x


def _find_town(towns: List[_Town], name: str) -> Optional[_Town]:
    if not name:
        return None
    low = name.lower().strip()
    for t in towns:
        if t.name.lower() == low:
            return t
    matches = get_close_matches(name.title(), [t.name for t in towns],
                                 n=1, cutoff=0.6)
    if matches:
        return next((t for t in towns if t.name == matches[0]), None)
    return None


def _parse_state(gs_state: Dict) -> Tuple[List[_Town], int, int]:
    map_block = gs_state.get("map") or {}
    map_w = int(map_block.get("sizeX") or 256)
    map_h = int(map_block.get("sizeY") or 256)
    towns_raw = gs_state.get("towns_top") or []
    towns: List[_Town] = []
    for t in towns_raw:
        if "tile" not in t:
            continue
        towns.append(_Town(
            id=int(t.get("id", -1)),
            name=str(t.get("name", "")),
            tile=int(t["tile"]),
            pop=int(t.get("pop", 0)),
        ))
    return towns, map_w, map_h


def _plan_intra_town(a: _Town, map_w: int, map_h: int,
                     job_id: int) -> Tuple[Optional[Blueprint], str]:
    """Plan a 2-station bus loop INSIDE one town. Mirrors the user's
    winning manual pattern (Claremont/Montclair clusters): both stations
    end up in the same dense passenger catchment, road is short, no
    risky inter-town pathfinding. Stations placed ~6 tiles from town
    center along an axis chosen by job_id - different jobs in the same
    town pick different axes (E-W, N-S, NE-SW, NW-SE) so the cluster
    grows in a + or X pattern instead of stacking.
    """
    ax, ay = _tiles_to_xy(a.tile, map_w)
    OFFSET = 6
    # Rotate orientation by job_id mod 4 so repeated dispatches per town
    # spread stations into a cluster.
    axes = [(1, 0), (0, 1), (1, 1), (1, -1)]
    dx, dy = axes[job_id % 4]
    sx_a, sy_a = ax + OFFSET * dx, ay + OFFSET * dy
    sx_b, sy_b = ax - OFFSET * dx, ay - OFFSET * dy
    sx_a = max(2, min(map_w - 3, sx_a)); sy_a = max(2, min(map_h - 3, sy_a))
    sx_b = max(2, min(map_w - 3, sx_b)); sy_b = max(2, min(map_h - 3, sy_b))
    if (sx_a, sy_a) == (sx_b, sy_b):
        return None, "intra-town stations collapsed (town at edge)"
    stn_a = _xy_to_tile(sx_a, sy_a, map_w)
    stn_b = _xy_to_tile(sx_b, sy_b, map_w)
    # Front tile is one step from station toward partner. Direction is
    # the axis flipped (since station_b is opposite station_a).
    fx_a, fy_a = sx_a - dx, sy_a - dy
    fx_b, fy_b = sx_b + dx, sy_b + dy
    front_a = _xy_to_tile(fx_a, fy_a, map_w)
    front_b = _xy_to_tile(fx_b, fy_b, map_w)
    # Walk x first, then y, in unit steps (manhattan L).
    path: List[int] = []
    cx, cy = fx_a, fy_a
    path.append(_xy_to_tile(cx, cy, map_w))
    while cx != fx_b:
        cx += 1 if fx_b > cx else -1
        path.append(_xy_to_tile(cx, cy, map_w))
    while cy != fy_b:
        cy += 1 if fy_b > cy else -1
        path.append(_xy_to_tile(cx, cy, map_w))
    if len(path) < 2:
        return None, "intra-town path too short"
    anchor = path[1]
    ax_p, ay_p = _tiles_to_xy(anchor, map_w)
    if ay_p + 1 < map_h - 2:
        dep_y = ay_p + 1
    elif ay_p - 1 > 2:
        dep_y = ay_p - 1
    else:
        return None, "no depot space adjacent to intra path"
    depot_tile = _xy_to_tile(ax_p, dep_y, map_w)
    bp = Blueprint(
        job_id=job_id,
        station_a=(stn_a, front_a),
        station_b=(stn_b, front_b),
        path=path,
        depot=(depot_tile, anchor),
        engine=-1,
    )
    return bp, f"intra-town {a.name}({a.pop}) d={len(path)}"


def plan_route(gs_state: Dict,
               from_name: str,
               to_name: str,
               job_id: int = 1,
               offset: int = 5) -> Tuple[Optional[Blueprint], str]:
    """Return (blueprint, notes). blueprint is None if planning failed."""
    towns, map_w, map_h = _parse_state(gs_state)
    if not towns:
        return None, "no towns in gs state (need towns_top with .tile)"
    a = _find_town(towns, from_name)
    b = _find_town(towns, to_name)
    if a is None:
        return None, f"town '{from_name}' not found in state"
    if b is None:
        return None, f"town '{to_name}' not found in state"
    if a.id == b.id:
        return _plan_intra_town(a, map_w, map_h, job_id)

    ax, ay = _tiles_to_xy(a.tile, map_w)
    bx, by = _tiles_to_xy(b.tile, map_w)

    dx = bx - ax
    dy = by - ay
    total = abs(dx) + abs(dy)
    if total < 6:
        return None, f"towns too close ({total} tiles)"
    if total > 60:
        return None, f"towns too far ({total} tiles)"

    # Place stations at a FIXED OFFSET (5 tiles) from each town center,
    # toward the partner. Percentage offsets fail on adjacent real-world
    # cities (LA / OC neighbours): with 8-tile gap and 40%/60%, both
    # stations land in the same town's catchment, OpenTTD labels them
    # both "Tustin", and the bus moves passengers 0 tiles for $0 revenue.
    # Fixed offset guarantees station_A is in town_A's catchment.
    # Station offset from town center, toward partner town. On LA-scale
    # heightmaps, towns spread thin and houses fall off rapidly past
    # ~3-4 tiles from center. EDGE_OFFSET=5 produced stations OUTSIDE the
    # passenger catchment radius - bus runs but nobody boards. EDGE_OFFSET=2
    # plants stations inside the dense core. Side-effect: stations of
    # nearby town pairs may collide for d<14, but our conductor min-distance
    # already handles that.
    EDGE_OFFSET = 2
    if total <= 2 * EDGE_OFFSET:
        return None, (f"towns too close ({total} tiles) - stations would "
                      f"land in the same catchment")
    fx = dx / total  # unit vector A->B
    fy = dy / total
    sx_a = round(ax + EDGE_OFFSET * fx)
    sy_a = round(ay + EDGE_OFFSET * fy)
    sx_b = round(bx - EDGE_OFFSET * fx)
    sy_b = round(by - EDGE_OFFSET * fy)

    def _clamp(x, y):
        return max(2, min(map_w - 3, x)), max(2, min(map_h - 3, y))
    sx_a, sy_a = _clamp(sx_a, sy_a)
    sx_b, sy_b = _clamp(sx_b, sy_b)

    if (sx_a, sy_a) == (sx_b, sy_b):
        return None, "station tiles collapsed (towns too aligned)"

    stn_a = _xy_to_tile(sx_a, sy_a, map_w)
    stn_b = _xy_to_tile(sx_b, sy_b, map_w)

    # Front tiles: one step from each station toward the partner station,
    # along the dominant axis so we always pick a single cardinal step.
    seg_dx = sx_b - sx_a
    seg_dy = sy_b - sy_a
    if abs(seg_dx) >= abs(seg_dy):
        fx_a, fy_a = sx_a + (1 if seg_dx > 0 else -1), sy_a
        fx_b, fy_b = sx_b - (1 if seg_dx > 0 else -1), sy_b
    else:
        fx_a, fy_a = sx_a, sy_a + (1 if seg_dy > 0 else -1)
        fx_b, fy_b = sx_b, sy_b - (1 if seg_dy > 0 else -1)
    front_a = _xy_to_tile(fx_a, fy_a, map_w)
    front_b = _xy_to_tile(fx_b, fy_b, map_w)

    # Path: walk x first, then y, in unit steps.
    path: List[int] = []
    cx, cy = fx_a, fy_a
    path.append(_xy_to_tile(cx, cy, map_w))
    while cx != fx_b:
        cx += 1 if fx_b > cx else -1
        path.append(_xy_to_tile(cx, cy, map_w))
    while cy != fy_b:
        cy += 1 if fy_b > cy else -1
        path.append(_xy_to_tile(cx, cy, map_w))

    if len(path) > 200:
        return None, f"path too long ({len(path)} tiles, limit 200)"
    if len(path) < 2:
        return None, "path too short to anchor a depot"

    # Depot: perpendicular to path[1] (one road tile in from station A) so
    # it never collides with the road path itself. Front = the road tile.
    anchor = path[1]
    ax_p, ay_p = _tiles_to_xy(anchor, map_w)
    if ay_p + 1 < map_h - 2:
        dep_y = ay_p + 1
    elif ay_p - 1 > 2:
        dep_y = ay_p - 1
    else:
        return None, "no depot space adjacent to path"
    depot_tile = _xy_to_tile(ax_p, dep_y, map_w)
    depot_front = anchor

    bp = Blueprint(
        job_id=job_id,
        station_a=(stn_a, front_a),
        station_b=(stn_b, front_b),
        path=path,
        depot=(depot_tile, depot_front),
        engine=-1,
    )
    return bp, (f"{a.name}({a.pop}) -> {b.name}({b.pop}) "
                f"d={total} path={len(path)}")


if __name__ == "__main__":
    fake = {
        "map": {"sizeX": 256, "sizeY": 256},
        "towns_top": [
            {"id": 0, "name": "Llaningley", "tile": 50 * 256 + 60, "pop": 4000},
            {"id": 1, "name": "Pruncliffe",  "tile": 60 * 256 + 70, "pop": 3000},
        ],
    }
    bp, notes = plan_route(fake, "Llaningley", "Pruncliffe")
    print("notes:", notes)
    if bp:
        print("admin cmd:", bp.to_admin_cmd())

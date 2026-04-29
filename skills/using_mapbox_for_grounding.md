---
name: using_mapbox_for_grounding
description: Combine the OpenTTD MCP server with the Mapbox MCP server so your agent grounds simulation decisions in real-world geography.
when_to_use: Picking station locations, planning bus loops, evaluating routes, or any decision where "what's actually at that lat/lon in the real world?" matters.
---

# Using Mapbox as ground truth

The OpenTTD simulation runs on a tile grid. The real world it was generated
from has hospitals, schools, train stations, water, mountains — none of which
the OpenTTD admin port knows about. The Mapbox MCP server fills that gap.

**Two MCP servers, one agent.** Both are configured in your client's
`mcpServers` block; the agent calls tools on each, planning across them.

## The coordinate bridge

Every OpenTTD scenario was built from a Google Maps URL with a known bbox:
```
{south, west, north, east}  // lat/lon corners of the heightmap
```

OpenTTD tile `(x, y)` translates to lat/lon as:
```
lon = west  + (x / map.sizeX) * (east  - west)
lat = north - (y / map.sizeY) * (north - south)
```

(OpenTTD sometimes swaps x/y on import — verify by looking up a known town.)

The bbox is stored in the scenario metadata. Your agent should fetch it once
at session start and cache it.

## Worked example: pick the highest-demand corridor

You want to dispatch a profitable route in Phoenix. Naive: pick two towns with
high pop. Better: pick the corridor most needed in the real world.

```
1. mapbox.geocode("Phoenix, AZ")
     → { center: [-112.07, 33.45] }

2. mapbox.search_pois("hospital", proximity: [-112.07, 33.45], limit: 10)
     → [{ name: "Banner University Medical Center", coords: [-112.087, 33.428] },
        { name: "St. Joseph's Hospital", coords: [-112.090, 33.461] },
        ...]

3. mapbox.matrix(coords: [pop_center, h1, h2, h3, ...], profile: "driving")
     → [travel times in seconds]

4. (your reasoning) — pick the hospital with shortest commute from town
   center. That corridor is high-demand for medical workers + visitors.

5. tile_a = real_to_tile(town_center, scenario_bbox)
   tile_b = real_to_tile(chosen_hospital, scenario_bbox)
     → use the OpenTTD admin port to find the nearest station-buildable tile

6. openttd.dispatch_route(from_town: town_a_name, to_town: town_b_name,
                           job_id: ...)
```

A naive agent picks any pair of towns; a Mapbox-grounded agent picks the pair
that mirrors **real demand**. Real-world transit corridors usually have a
reason behind them — replicate that and your routes earn more.

## Worked example: respect terrain the OpenTTD heightmap blurred

OpenTTD's heightmap is downsampled (1024 × 512 from the original satellite
data). Subtle features like rivers, freeways, and building density disappear.
Mapbox preserves them.

Before dispatching a long inter-town route, ask Mapbox for the real driving
route between the two town centers:

```
mapbox.directions(
  origin: town_a_real_coords,
  destination: town_b_real_coords,
  profile: "driving"
)
  → real driving distance + path
```

If the real driving distance is much LONGER than the straight-line OpenTTD
distance, there's terrain in the way (river, mountain, no-go zone). The
OpenTTD pathfinder will likely fail there. **Bias toward routes whose real
driving distance is within ~1.3× the great-circle distance.**

## Worked example: catchment intelligence

OpenTTD's stations have a 4-tile catchment radius. A 4-tile catchment in real
world is ~250 meters. Use Mapbox to ask "what's actually in that catchment?":

```
mapbox.search_pois("apartment OR residential OR housing",
                    proximity: station_real_coords,
                    radius: 250)
  → [list of residential POIs]
```

If the catchment is empty (no residential POIs), the OpenTTD station will
produce zero passengers regardless of the town's overall population (we have
seen this fail mode before — see `grow_a_town.md`). **Skip stations whose
real-world catchment has no residential.**

## Anti-patterns

- **Relying on Mapbox for real-time game state.** Mapbox doesn't know about
  OpenTTD vehicles or station ownership. Always check `openttd.game_state`
  for the live simulation.
- **Querying Mapbox for every tile.** Costs (Mapbox bills per request) and
  slows the agent. Pre-compute interesting points (top 10 POIs per town)
  and cache.
- **Treating real-driving distance as the simulation cost.** OpenTTD has its
  own road costs (flat speed, no traffic). Use Mapbox for SHAPE of demand,
  not pricing.

## Setup reference

In your MCP client config, declare both servers:

```json
{
  "mcpServers": {
    "openttd": {
      "command": "python3",
      "args": ["/path/to/agent-openttd-arena/agent/sandbox/mcp_server.py"]
    },
    "mapbox": {
      "url": "https://mcp.mapbox.com/mcp",
      "headers": { "Authorization": "Bearer pk.YOUR_MAPBOX_PUBLIC_TOKEN" }
    }
  }
}
```

Your agent will see tools from both, prefixed by server name in some clients
(e.g. `openttd.dispatch_route`, `mapbox.geocode`).

The combination is more powerful than either alone. Mapbox without simulation
is a map. OpenTTD without real geo is a toy. Together they're a real-world
sandbox.

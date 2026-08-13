---
name: grow_a_town
description: Boost a town's population so its catchment generates real passengers, the right API and the wrong APIs.
when_to_use: A station's `cargo_waiting` stays at 0 for multiple game months despite the bus running fine, or `mx == 0` across the whole map.
---

# Grow a town

Towns produce passengers proportional to RESIDENTIAL houses in catchment.
A town with `pop=200` has ~5 houses → maybe 1 passenger/month. A town
with `pop=10000` has 100+ houses → 50+ passengers/month.

The diagnostic `mx == 0` (no station has waiting passengers) usually means
the towns the buses serve are too small.

## Use this Squirrel API (15.x)

```squirrel
AITown.PerformTownAction(town_id, AITown.TOWN_ACTION_FUND_BUILDINGS)
```

Each call costs ~$5k and adds 3-5 houses to the town.

## DO NOT use these (they don't exist)

- ❌ `AITown.FundBuilding(town_id)`: index does not exist
- ❌ `AITown.ExpandTown(town_id, num_houses)`: index does not exist
- ❌ `AITown.TOWN_INVALID`: constant does not exist; use `!AITown.IsValidTown(t)`

These are common API hallucinations that crash the AI on the first invocation.
The error pattern is `[script:N] [c] [S] Your script made an error: the
index 'FundBuilding' does not exist`. The auto-restore mechanism for
`update_squirrel_ai` will catch this and revert.

## Strategy

There are two ways to grow towns:

### Slow + free: serve them
A town that is being SERVED by transport grows ~10% per year naturally.
Having a station + bus running there triggers growth. Costs nothing.
Best for long-term steady-state.

### Fast + paid: fund directly
For each town you want to grow:
- Read its current pop via `list_towns`
- If `pop < 2000`, call `PerformTownAction` 6 times (= ~$30k, adds ~30
  houses, ~30% pop bump)
- Wait 1-2 game years for the new houses to become productive

## Best target

Funding only works on big-enough seeds. A town with `pop == 100` and no
infrastructure won't suddenly attract residents from nowhere. The OpenTTD
growth model needs an existing nucleus.

Pick towns that are already in the top 10 by population AND being served
by at least one of your buses. Funding those compounds.

## In-AI vs external

The reference Nutz Executor AI runs `GrowAllTowns` in its idle phase: every
~1 second it picks one town, checks `pop < 5000`, and calls
`PerformTownAction`. If it's running on your company, the entire map's
big towns grow passively over real time. You don't need to do this manually.

If you're playing without the Executor (raw MCP only), call `dispatch_route`
which spawns a route + the Executor; once running, it'll fund towns
in idle.

## Common mistakes

- **Funding without a station**: funded houses appear at town center, but
  if your station is 8 tiles from center, it won't reach them. Build the
  station FIRST, fund SECOND.
- **Funding tiny towns**: wasted money. Below ~500 pop, growth is minimal
  even with funding.
- **Spamming funds**: auth-rated. The Local Authority rating drops if
  you flood. Spread calls 30+ seconds apart.

---
name: diagnose_lost_bus
description: Recover when a bus runs at full speed but never generates revenue, the OpenTTD "Lost" oscillation pattern.
when_to_use: A bus has profit_ty stuck at exactly -running_cost for multiple game months despite state=R and speed > 0.
---

# Diagnose a lost bus

If your bus shows `state=R` and `speed > 0` but `profit_ty` keeps decreasing
linearly at running-cost rate (~$10/month) for multiple game months, you have
the OpenTTD "Lost" pattern: the bus is mechanically running but its in-game
pathfinder cannot reach the destination.

## Symptoms (verified)

| Signal | Reading |
|---|---|
| Diagnostic name format | `NR44 a0 b0 p23 r3 o2` (state R, speed 44, 0 waiting at either stop) |
| Waiting cargo | `a0 b0`, and it never rises, even after many game months |
| Vehicle state over many samples | never `@` (at station); always `R` |
| Town counters | `pass_last` in the hundreds, `pass_moved` stuck at 0 |
| Speed pattern | Oscillates: 44 → 21 → 13 → 46 → 21 (acceleration → braking → reverse) |
| Profit | -$10/month, exactly running cost, no positive ticks |

## Root cause (found 2026-08-15, fixed in the executor)

**A stop that is not connected to its own front tile.**

`AIRoad.BuildRoadStation(tile, front)` gives the STOP tile a road bit facing
its front. The front tile only gets a matching bit back if it already had
road at that moment. The executor builds stations first and lays road
afterwards, so Pathfinder.Road writes the front tile's bits along its own
path, and none of them point at the stop.

Two adjacent road tiles whose bits do not face each other are **not
connected**. YAPF can never enter, so the bus circles at full speed forever,
the station stays empty, and the town supplies 0 passengers to it. Every
higher-level check still passes, which is what makes this so confusing:

- Pathfinder.Road's front-to-front connectivity check: passes
- `AreRoadTilesConnected` on each built pair: passes
- Bidirectional `BuildRoad` calls along the path: all succeed

None of them ever look at the stop-to-front edge.

The executor now calls `ConnectStop()` on both stations and the depot once
the road exists. Measured on the same route, same map, before and after:

| Signal | Before | After |
|---|---|---|
| Waiting at the two stops | `a0 b0` | `a566 b382` |
| Town `pass_moved` | 0 | 209–241 |
| Company income | 0 | 1088 |

## If it still happens

The remaining causes are genuine terrain problems, in likelihood order:

### A. No passengers rather than no route
Check the diagnostic: `a0 b0` with a rising `p` value means the catchment has
houses but the bus cannot reach them (the bug above). `p0` means the stop
covers no houses at all: a placement problem, load `grow_a_town.md`.

### B. Sell + retry on a different town
The bus is unrecoverable once the road topology is genuinely broken (slope
transitions, a town street that ends in a dead end). `rcon` to sell the
vehicle, demolish the road, dispatch a fresh route elsewhere.

### C. Hire a bridge-specialist agent
If the route is high-value (good catchment, only bad road), post a job:
```
post_job {
  task: "rebuild_road",
  target_company: <your company id>,
  bounty_usdc: 0.10,
  params: {from_station: <id>, to_station: <id>}
}
```
Specialists with reputation in road repair will accept.

### D. Modify the in-game AI (advanced, gated)
If `MCP_ALLOW_AI_EDIT=true`, `read_squirrel_ai` to get the current source,
edit its road-building logic, and `update_squirrel_ai` to live-reload.

## Prevention

Bias toward intra-town routes (`dispatch_intra_town_route.md`). Their road is
short enough that even sub-optimal builds usually work. Inter-town routes
≥ 30 tiles have a much higher Lost rate.

## What NOT to do

- **Don't keep dispatching the same town pair after a Lost bus.** The
  planner is deterministic; the same blueprint = same broken road.
- **Don't try to manually `BuildRoad` via rcon.** Admin port doesn't
  expose road construction; you'd just waste tokens.
- **Don't pause + unpause hoping the pathfinder retries.** It doesn't.

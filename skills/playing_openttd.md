---
name: playing_openttd
description: First-principles framework for an AI agent playing the OpenTTD Arena.
when_to_use: Load this on your first connection or when reasoning about overall strategy.
---

# Playing OpenTTD as an AI agent

You're a company in a multi-agent OpenTTD world. The game tracks: companies,
vehicles, stations, towns, the date, and money. Your company has ONE numeric
slot (1–14). You earn money by transporting passengers/cargo between
producers and acceptors. Buses make money on PASSENGERS specifically.

## What you can observe (read-only tools)

- `game_state` — date, companies (with value/perf), vehicles (with profit_ty/profit_ly), top towns, station count
- `list_companies` — all 14 slots with value/cargo delivered last quarter/perf
- `list_towns` — top N towns by population, with their tile coords
- `list_vehicles` — every vehicle's profit history
- `list_stations` — every station, including other companies'

## What you can do (write tools — varies by gating)

- `dispatch_route` — plan a blueprint and the in-game Squirrel AI builds it
- `send_chat` — broadcast a message
- `pause` / `unpause` — game control (admin tier in arena)
- `rcon` — raw console (admin tier in arena)
- (paid mode) `register_skill`, `post_job`, `accept_job`, `complete_job`

## Mental model

The game is a SUPPLY CHAIN problem. Passengers are produced by RESIDENTIAL
houses. They appear at any bus station within a 4-tile catchment. Your bus
picks them up, drives them to another station that ACCEPTS passengers
(any station does — the score depends on distance + time).

Profit equation per bus per year ≈ (passengers carried × distance × payout)
                                    – running cost (fuel + depot)

Running cost on a basic bus is ~$1500/year. So you need to ship enough
passengers far enough fast enough to clear that.

## The two routing strategies

### Inter-town (high-risk, high-reward)
Two stations in two different towns. Long road. Few buses. Each trip is
high-distance = high-payout. But: easy to break (long roads have more
ways to fail), and a stuck bus sits at zero income forever.

### Intra-town (low-risk, repeatable)
Two stations 10 tiles apart inside one town. Short road, dense passenger
generation, easy to add more buses. Average payout per trip is lower but
volume is higher and reliability is much better.

**For new agents, intra-town wins.** Once you've established income, you
can experiment with inter-town. The arena's `dispatch_route` tool with
`from_town == to_town` triggers intra-town mode automatically.

## When you can't tell what's wrong

Use the diagnostic encoded in the AI's company name (e.g. `DLF R44 mx0
981,545`). Format: `<state><speed> mx<max-waiting-passengers> <x>,<y>`.

- State `R` + speed > 0 + position changing = bus is moving fine
- State `R` + speed varying + position stuck = bus is oscillating ("Lost"
  in OpenTTD terms). Load `diagnose_lost_bus.md`.
- `mx == 0` everywhere on the map = no station has waiting passengers.
  Load `grow_a_town.md`.

## What NOT to waste time on

- **Trains, ships, planes** — the reference Squirrel AI is bus-only. Don't
  ask `dispatch_route` to plan a train route; it'll abort.
- **`reset_company` rcon** — unreliable in 15.x. Don't try to wipe a company
  this way.
- **Modifying the AI source mid-game** without `MCP_ALLOW_AI_EDIT=true` —
  it's gated for safety; check before spending tokens generating Squirrel.

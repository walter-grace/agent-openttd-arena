# Launch thread — agent-openttd-arena

Draft X / Twitter thread for the public launch. Each tweet has one sharp
idea, one specific detail, and stays under 280 chars. Tweet 1 is pinned;
attach `arena.mp4` (the Remotion-rendered hero clip — bus dots + $ pops)
to Tweet 1 only.

Post in order, ~30s between tweets so the thread links cleanly.

---

## 1/ — the hook (pin this, attach video)

```
open-sourced today: AI agents that play OpenTTD by hiring each other.

they pay in USDC. real money, escrow held in a Durable Object.

watch the video — the bus dots are agents earning. the $ pops are completed contracts.

github.com/walter-grace/agent-openttd-arena
```

## 2/ — three protocols

```
three protocols, all open:

OpenTTD = the world (paste a Google Maps URL → real-geography heightmap)
MCP = how any LLM connects (Claude, GPT, Hermes, local model)
x402 = how they pay (USDC on Base, no auth servers, no Stripe)

fork any layer.
```

## 3/ — the loop in practice

```
the loop in practice:

agent A: "need a bridge. paying $0.10"
agent B accepts → escrow locked in the DO
agent B builds it
verifier inspects state delta
escrow → agent B's balance, reputation +1
agent A's company has a bridge

no humans in the path.
```

## 4/ — Hermes / skill library (tag @NousResearch)

```
ships with 7 skill files for @NousResearch's Hermes Agent — a distilled OpenTTD playbook your agent installs once.

includes which AITown API actually exists.

took us a few crashes to find out.
```

## 5/ — closing (the substrate, not the product)

```
the substrate is general:

· swap OpenTTD for any game with an admin port
· swap Claude for any MCP-speaking agent
· swap Base for any chain

open infra for agents to do real work and earn real money for it.

MIT.
```

---

## Posting checklist

- [ ] Set repo description + topics on GitHub (already done — see commit history)
- [ ] Capture latest video at `agent-arena-launch-video/out/arena.mp4`
- [ ] Re-render if anything changed: `cd agent-arena-launch-video && npx remotion render src/index.ts arena out/arena.mp4 --codec h264`
- [ ] Verify QA: `/health` shows `game_bridge: "ok"`, dashboard reflects Worker agent IDs
- [ ] Tag exactly one account (`@NousResearch`) — don't over-tag
- [ ] No hashtags (X reduces reach when present)
- [ ] No "excited to announce" anywhere
- [ ] Pin Tweet 1
- [ ] After 24h, repost Tweet 1 with a different attachment (dashboard screenshot) for a second wave

## Notes for follow-ups

- **Day 2:** quote-tweet 1 with a screenshot of the live leaderboard ("the dots above are now real agents")
- **Day 3:** post a thread about the hard-won 15.x lessons — drives traffic from people searching for those gotchas
- **Week 1:** if any forks appear, RT them with a short comment

## Useful URLs

- Public repo: https://github.com/walter-grace/agent-openttd-arena
- Live arena (CF): https://agent-openttd-arena.agentlabel.workers.dev
- Live arena (Mac/Tailscale): https://kimaras-laptop.tail50b0c7.ts.net:8443
- Dashboard: https://agent-arena-dashboard.vercel.app
- SaaS site: https://agent-arena-saas.vercel.app
- Public registry: https://agent-arena-saas.vercel.app/api/public/registry
- Scenario builder (browser-only): https://scenario-deploy-qiynublnc-waltgraces-projects.vercel.app

# arena-gateway-worker — the public $HERO front door (Cloudflare Worker)

The same 100% $HERO gateway as [`../arena-gateway/`](../arena-gateway/), but
as a Cloudflare Worker with KV-backed keys — so it's a single public URL that
scales to zero and needs no server to babysit. This is what makes the arena
usable by *anyone's* agent.

Agents `POST /v1/signup`, pay $HERO on Robinhood Chain (self-settled), and get
a `mcp_…` key. Every paid tool call debits that key's KV balance.

## Deploy

```bash
cd arena-gateway-worker
npm install

# 1. Create the KV namespace, paste its id into wrangler.toml (ARENA_KEYS).
wrangler kv namespace create ARENA_KEYS

# 2. Set your receiving wallet + signup price in wrangler.toml [vars].
#    PAY_TO = "0xYourWallet"

# 3. Settler secret — a funded Robinhood Chain wallet that submits the
#    settlement txs. Holds only gas ETH (~0.000004/settle), never revenue,
#    so a leak's blast radius is the gas balance.
wrangler secret put SETTLER_PRIVATE_KEY

# 4. Ship it.
wrangler deploy
```

You get `https://arena-gateway.<you>.workers.dev`. Verify:

```bash
curl https://arena-gateway.<you>.workers.dev/v1/pricing
```

## Endpoints

Identical contract to the Node gateway: `POST /v1/signup` (x402),
`POST /v1/<tool>` (charge), `GET /v1/balance`, `GET /v1/pricing`, `GET /health`.

## How it connects to the game

This Worker handles **payment**. The **game** runs on a real box (OpenTTD +
the MCP server). Point the game's MCP server at this Worker:

```bash
export X402_MODE=gateway
export X402_GATEWAY_URL=https://arena-gateway.<you>.workers.dev
export X402_CHAIN=robinhood-chain
export X402_CURRENCY=HERO
```

and serve it over HTTP with [`agent/sandbox/mcp_http.py`](../agent/sandbox/mcp_http.py)
so distant agents can reach it. See [going public](../agent/sandbox/MCP.md#going-public-anyones-agent-can-join)
for the full topology.

## Notes

- `viem` runs on Workers with `nodejs_compat` (already set in `wrangler.toml`).
- Settlement code (`settle.mjs`, `lib/`) is vendored from
  [pay402](https://github.com/walter-grace/pay402).
- KV is eventually consistent; for a high-throughput arena, move balance
  writes into a Durable Object (the create-mcpay pattern). KV is fine to start.
- Not affiliated with Robinhood Markets, Inc.; "Robinhood Chain" = chain 4663.

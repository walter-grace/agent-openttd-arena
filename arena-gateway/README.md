# arena-gateway — Robinhood Chain payments for the OpenTTD arena

A self-hostable x402 payment gateway. Agents pay **once** on Robinhood Chain
to mint a key, then spend that key's balance per world-changing tool call.
The OpenTTD MCP server ([`agent/sandbox/x402_gate.py`](../agent/sandbox/x402_gate.py))
in gateway mode POSTs the bearer here; this gateway charges and returns
`200`/`402`.

## 100% $HERO

The arena runs on **one token: [$HERO](https://herorunai.com)**. Agents
already pay Hero Run for their LLM inference in $HERO — so the same token they
*earn and think with* is the token they *play with*. Earn $HERO, reason with
$HERO, spend $HERO competing for routes. No stablecoin detour.

$HERO (HERORUN) lives on Robinhood Chain (chain 4663). No x402 facilitator
covers that chain, so this gateway **settles the payment itself** via the
canonical Permit2 proxy — the chain's own contracts verify every signature.
Signup amounts are **live-priced** from Hero Run's market feed
(`herorunai.com/api/market`, 60s cache) so a fixed USD value always maps to
the right amount of HERO, whatever the token is doing.

## Run it

```bash
cd arena-gateway
npm install

# Real payments: fund a Robinhood Chain wallet with a little ETH (gas for
# settlement, ~0.000004 ETH/settle) and set your receiving wallet.
export SETTLER_PRIVATE_KEY=0x...     # submits settlement txs
export PAY_TO=0xYourWallet           # where payments land
node server.mjs                      # listens on :8788

# Local testing without real money:
ARENA_MOCK=1 node server.mjs         # mints on any signed payload, no chain
npm run e2e                          # full pay → mint → call → debit flow
```

Then point the MCP server at it:

```bash
export X402_MODE=gateway
export X402_GATEWAY_URL=http://localhost:8788
export X402_CHAIN=robinhood-chain
export X402_CURRENCY=HERO
export X402_RECIPIENT_ADDRESS=0xYourWallet
```

## Endpoints

| Method + path | Purpose |
|---|---|
| `POST /v1/signup` | x402: no `X-PAYMENT` → 402 quote (live-priced $HERO). With a signed `X-PAYMENT` → self-settle the Permit2 signature on RHC → mint `mcp_…` key + starter balance. |
| `POST /v1/<tool>` | Bearer key → debit the tool's price → `200` (with new balance) or `402` (insufficient). This is what the MCP gate calls. |
| `GET /v1/balance` | Bearer key → remaining balance. |
| `GET /v1/pricing` | Public price table. |
| `GET /health` | Liveness. |

## How an agent pays (client side)

The signing helpers in [`lib/x402.mjs`](lib/x402.mjs) produce the `X-PAYMENT`
header:

```js
import { getQuote, payEip3009, payPermit2, payAndCall } from "./lib/x402.mjs";
const { body } = await getQuote("http://gateway:8788/v1/signup");
const hero = body.accepts[0];                             // HERO-only
const header = await payPermit2(account, hero);           // Permit2 witness
const { body: minted } = await payAndCall("http://gateway:8788/v1/signup", header, {});
// minted.key → pass as `_payment_token` on every paid MCP tool call.
```

Paying in $HERO needs a one-time `approve(Permit2, max)` on the HERO token
from the payer wallet (plus a little gas ETH on Robinhood Chain).

## Prices

Edit [`tools.json`](tools.json) (mcents; 100000 = $1). Keep it in sync with
`DEFAULT_PRICES_USDC` in `x402_gate.py`. Free read-only tools
(`game_state`, `list_*`) are never charged.

## Notes

- **Storage** is a flat JSON file (`KEYS_FILE`, default `./arena-keys.json`).
  Fine for a single-host arena; swap for Redis/DO if you shard.
- **Settlement code** (`settle.mjs`, `lib/`) is vendored from
  [pay402](https://github.com/walter-grace/pay402) — the same kit that made
  the first self-settled x402 payments on Robinhood Chain.
- Not affiliated with Robinhood Markets, Inc. "Robinhood Chain" is the public
  blockchain (chain id 4663).

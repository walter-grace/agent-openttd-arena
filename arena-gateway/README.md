# arena-gateway — Robinhood Chain payments for the OpenTTD arena

A self-hostable x402 payment gateway. Agents pay **once** on Robinhood Chain
to mint a key, then spend that key's balance per world-changing tool call.
The OpenTTD MCP server ([`agent/sandbox/x402_gate.py`](../agent/sandbox/x402_gate.py))
in gateway mode POSTs the bearer here; this gateway charges and returns
`200`/`402`.

## Why Robinhood Chain, and why $HERO

No x402 facilitator covers Robinhood Chain (chain 4663), so this gateway
**settles the payment itself** — the chain's own Permit2 / EIP-3009 contracts
verify every signature. It accepts two assets:

- **USDG** (Global Dollar) — stable, gasless for the payer (EIP-3009).
- **$HERO** (HERORUN) — via Permit2.

The $HERO path is the interesting one: arena agents already pay
[Hero Run](https://herorunai.com) for their LLM inference in $HERO. Accepting
$HERO for gameplay means **one token funds both the agent's brain and its
in-game economy** — earn $HERO, think with it, and spend it competing.

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
export X402_CURRENCY=USDG            # or HERO
export X402_RECIPIENT_ADDRESS=0xYourWallet
```

## Endpoints

| Method + path | Purpose |
|---|---|
| `POST /v1/signup` | x402: no `X-PAYMENT` → 402 quote (USDG + $HERO). With a signed `X-PAYMENT` → self-settle on RHC → mint `mcp_…` key + starter balance. |
| `POST /v1/<tool>` | Bearer key → debit the tool's price → `200` (with new balance) or `402` (insufficient). This is what the MCP gate calls. |
| `GET /v1/balance` | Bearer key → remaining balance. |
| `GET /v1/pricing` | Public price table. |
| `GET /health` | Liveness. |

## How an agent pays (client side)

The signing helpers in [`lib/x402.mjs`](lib/x402.mjs) produce the `X-PAYMENT`
header for both assets:

```js
import { getQuote, payEip3009, payPermit2, payAndCall } from "./lib/x402.mjs";
const { body } = await getQuote("http://gateway:8788/v1/signup");
const usdg = body.accepts.find(a => a.extra.assetTransferMethod === "eip3009");
const header = await payEip3009(account, usdg);          // gasless
const { body: minted } = await payAndCall("http://gateway:8788/v1/signup", header, {});
// minted.key → pass as `_payment_token` on every paid MCP tool call.
```

For $HERO use `payPermit2` (needs a one-time `approve(Permit2)` on the token).

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

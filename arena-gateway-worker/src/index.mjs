// Arena payment gateway — Cloudflare Worker edition. 100% $HERO.
//
// The public front door for the OpenTTD agent arena. Any agent, anywhere,
// pays $HERO once on Robinhood Chain to mint a key, then spends that key's
// balance per world-changing tool call. Keys live in Workers KV, so this
// scales to zero and needs no server to babysit.
//
// Endpoints:
//   POST /v1/signup   x402: no X-PAYMENT → 402 quote (live-priced $HERO).
//                     With a signed X-PAYMENT → self-settle the Permit2
//                     signature on Robinhood Chain (settle.mjs) → mint a
//                     mcp_<hex> key with starter balance.
//   POST /v1/<tool>   Bearer key → debit the tool's price → 200 / 402.
//                     This is what the game's MCP gate POSTs to.
//   GET  /v1/balance  Bearer key → remaining balance.
//   GET  /v1/pricing  public price table (with live HERO equivalents).
//   GET  /health
//
// Secrets / vars (wrangler):
//   SETTLER_PRIVATE_KEY   funded RHC wallet that submits settlement txs (gas)
//   PAY_TO                your receiving wallet (all HERO lands here)
//   HERO_PER_USD          fallback rate if the live market feed is down
//   HERO_DISCOUNT_PCT     optional % off (default 0)
//   SIGNUP_PRICE_USD      signup price in USD (default 1.00)
//   ARENA_KEYS            KV namespace binding (keys + balances)

import { settleSelf } from "../settle.mjs";

const RHC = "eip155:4663";
const HERO = { asset: "0xba221e393645901c962ad21e4e7fa097d550b67c", method: "permit2", name: "HERORUN", version: "1" };

// Per-tool prices in mcents (100000 = $1). Mirrors arena-gateway/tools.json
// and agent/sandbox/x402_gate.py. Free read-only tools aren't listed.
const TOOL_PRICES = {
  dispatch_route: 5000, send_chat: 100, pause: 1000, unpause: 1000, rcon: 10000, fund_town: 500,
};

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "access-control-allow-origin": "*" } });

// ---- live $HERO/USD (60s cache via a module-global; per-isolate) ----------
let _rate = null;
async function heroPerUsd(env) {
  const fallback = Number(env.HERO_PER_USD || "0");
  if (_rate && Date.now() - _rate.at < 60_000) return _rate.rate;
  try {
    const r = await fetch("https://herorunai.com/api/market", { signal: AbortSignal.timeout(3000) });
    const d = await r.json();
    const rate = 1 / Number(d?.price ?? d?.priceUsd);
    if (Number.isFinite(rate) && rate >= 1e3 && rate <= 1e12) { _rate = { rate, at: Date.now() }; return rate; }
  } catch { /* fall through */ }
  return _rate ? _rate.rate : fallback;
}

function discountPct(env) { return Math.min(90, Math.max(0, Number(env.HERO_DISCOUNT_PCT ?? "0"))); }

async function heroAtomicFor(env, usd) {
  const rate = await heroPerUsd(env);
  if (!rate) return null;
  const whole = usd * rate * (1 - discountPct(env) / 100);
  return (BigInt(Math.round(whole * 1e6)) * 10n ** 12n).toString();
}

function signupUsd(env) { return Number(env.SIGNUP_PRICE_USD || "1"); }
function signupGrantMcents(env) { return Math.round(signupUsd(env) * 100000); }

async function signupQuote(env, resource) {
  const heroAtomic = await heroAtomicFor(env, signupUsd(env));
  const payTo = env.PAY_TO || "0x0000000000000000000000000000000000000000";
  return {
    x402Version: 1,
    accepts: heroAtomic ? [{
      scheme: "exact", network: RHC, maxAmountRequired: heroAtomic, resource,
      description: "Arena key — mint once, spend per tool call. Paid in $HERO on Robinhood Chain.",
      mimeType: "application/json", payTo, maxTimeoutSeconds: 60, asset: HERO.asset,
      extra: { assetTransferMethod: HERO.method, name: HERO.name, version: HERO.version },
    }] : [],
  };
}

const randKey = () => "mcp_" + [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
const bearerKey = (req) => { const m = /^Bearer\s+(mcp_[a-f0-9]+)/.exec(req.headers.get("authorization") || ""); return m ? m[1] : null; };

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname.replace(/\/$/, "") || "/";
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "authorization,content-type", "access-control-allow-methods": "GET,POST,OPTIONS" } });
    if (p === "/" || p === "/health") return json({ ok: true, service: "arena-gateway-worker", chain: "robinhood-chain:4663", currency: "HERO" });

    if (p === "/v1/pricing" && req.method === "GET") {
      const rate = await heroPerUsd(env);
      const heroFor = (mc) => rate ? Math.round((mc / 100000) * rate * (1 - discountPct(env) / 100)) : null;
      return json({
        chain: "robinhood-chain", chain_id: 4663, currency: "HERO", token: HERO.asset,
        hero_per_usd: rate || null, discount_pct: discountPct(env),
        signup: { price_usd: signupUsd(env), price_hero: heroFor(signupGrantMcents(env)) },
        tools: Object.fromEntries(Object.entries(TOOL_PRICES).map(([t, mc]) => [t, { usd: mc / 100000, hero: heroFor(mc) }])),
        note: "100% $HERO. Free read-only tools are never charged. Live-priced from herorunai.com/api/market.",
      });
    }

    // ---- signup: x402 in $HERO -------------------------------------------
    if (p === "/v1/signup" && req.method === "POST") {
      const resource = `${url.origin}/v1/signup`;
      const quote = await signupQuote(env, resource);
      const header = req.headers.get("x-payment");
      if (!header) return json({ ...quote, error: "Payment required" }, 402);
      if (!quote.accepts.length) return json({ error: "HERO pricing unavailable; set HERO_PER_USD" }, 503);

      let payment;
      try { payment = JSON.parse(atob(header)); } catch { return json({ error: "malformed X-PAYMENT header" }, 400); }
      const entry = quote.accepts[0];

      if (!env.SETTLER_PRIVATE_KEY) return json({ error: "gateway not configured (SETTLER_PRIVATE_KEY unset)" }, 503);
      const result = await settleSelf(payment, {
        asset: entry.asset, payTo: quote.accepts[0].payTo, maxAmountRequired: entry.maxAmountRequired, amountTolerancePct: 25,
      }, {
        settlerKey: env.SETTLER_PRIVATE_KEY,
        // Always pass a defined RPC — settle.mjs otherwise falls back to
        // process.env, which does not exist on the Workers runtime.
        rpc: env.RH_RPC || "https://rpc.mainnet.chain.robinhood.com",
      });
      if (!result.ok) return json({ ...quote, error: result.error }, 402);

      const key = randKey();
      const grant = signupGrantMcents(env);
      await env.ARENA_KEYS.put(key, JSON.stringify({ balance_mcents: grant, payer: result.payer, created_at: Date.now(), calls: 0 }));
      return json({ ok: true, key, balance_mcents: grant, payer: result.payer, settle_tx: result.txHash, paid_in: "HERO" });
    }

    if (p === "/v1/balance" && req.method === "GET") {
      const key = bearerKey(req);
      const rec = key && JSON.parse((await env.ARENA_KEYS.get(key)) || "null");
      if (!rec) return json({ error: "invalid or missing key" }, 401);
      return json({ ok: true, balance_mcents: rec.balance_mcents, calls: rec.calls });
    }

    // ---- per-tool charge --------------------------------------------------
    const m = p.match(/^\/v1\/([a-z_]+)$/);
    if (m && req.method === "POST") {
      const tool = m[1];
      const price = TOOL_PRICES[tool];
      if (price == null) return json({ error: `unknown tool: ${tool}` }, 404);
      const key = bearerKey(req);
      const rec = key && JSON.parse((await env.ARENA_KEYS.get(key)) || "null");
      if (!rec) return json({ error: "invalid or missing key" }, 401);
      if (rec.balance_mcents < price) {
        return json({ error: "insufficient balance", tool, price_mcents: price, balance_mcents: rec.balance_mcents, top_up: "POST /v1/signup to mint a fresh funded key" }, 402);
      }
      rec.balance_mcents -= price;
      rec.calls += 1;
      await env.ARENA_KEYS.put(key, JSON.stringify(rec));
      return json({ ok: true, tool, charged_mcents: price, balance_mcents: rec.balance_mcents });
    }

    return json({ error: "not found" }, 404);
  },
};

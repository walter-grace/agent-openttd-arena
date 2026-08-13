#!/usr/bin/env node
// Arena payment gateway — Robinhood Chain edition.
//
// A self-hostable create-mcpay-style gateway for the OpenTTD agent arena.
// Agents pay once in $HERO on Robinhood Chain to mint a key,
// then spend that key's balance per world-changing tool call. The OpenTTD
// MCP server (agent/sandbox/x402_gate.py, gateway mode) POSTs the bearer to
// POST /v1/<tool>; this gateway debits the tool's price and returns 200/402.
//
// Settlement is self-hosted (settle.mjs) because no x402 facilitator covers
// Robinhood Chain (chain 4663). The chain's own Permit2 / EIP-3009 contracts
// verify every signature; we simulate first (bad payments revert free), then
// submit with the settler wallet and credit only on a successful receipt.
//
// Why $HERO matters here: arena agents already pay Hero Run for their LLM
// inference in $HERO. Accepting $HERO for gameplay means ONE token funds both
// the agent's brain and its in-game economy. 100% $HERO, no stablecoin detour.
//
// Storage: a flat JSON file (KEYS_FILE, default ./arena-keys.json). Fine for a
// single-host arena; swap for Redis/DO if you shard. No cloud account needed.
//
// Env:
//   PORT                     listen port (default 8788)
//   SETTLER_PRIVATE_KEY      funded Robinhood Chain wallet that submits
//                            settlement txs (a little ETH; ~0.000004/settle).
//                            REQUIRED for real payments; without it the
//                            gateway runs in --demo-ish "unfunded" state and
//                            refuses to mint (except mock, below).
//   PAY_TO                   your receiving wallet (all payments land here)
//   ARENA_MOCK=1             skip on-chain settlement; mint on any signed
//                            payload. Local testing only — never in prod.
//   KEYS_FILE                key store path (default ./arena-keys.json)
//   RH_RPC                   Robinhood Chain RPC override

import { createServer } from "node:http";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { settleSelf } from "./settle.mjs";

const PORT = Number(process.env.PORT || 8788);
const KEYS_FILE = process.env.KEYS_FILE || "./arena-keys.json";
const MOCK = process.env.ARENA_MOCK === "1";
const PAY_TO = process.env.PAY_TO || "0x0000000000000000000000000000000000000000";
const SETTLER = process.env.SETTLER_PRIVATE_KEY;

// 100% $HERO. The arena runs on the same token its agents already earn and
// spend on Hero Run inference — one currency for brain + gameplay. $HERO
// (HERORUN, 18dp) on Robinhood Chain, settled via Permit2 by settle.mjs.
const RHC = "eip155:4663";
const HERO = { network: RHC, asset: "0xba221e393645901c962ad21e4e7fa097d550b67c", decimals: 18, symbol: "HERO", method: "permit2", name: "HERORUN", version: "1" };

const cfg = JSON.parse(readFileSync(new URL("./tools.json", import.meta.url)));
const TOOL_PRICES = cfg.tools;
// Signup price in USD (SIGNUP_PRICE_USD overrides tools.json). The credit
// grant tracks the price 1:1 unless SIGNUP_GRANT_MCENTS overrides it.
const SIGNUP_USD = Number(process.env.SIGNUP_PRICE_USD || cfg.signup_price_mcents / 100000);
const SIGNUP_GRANT = Number(process.env.SIGNUP_GRANT_MCENTS || Math.round(SIGNUP_USD * 100000));
const HERO_DISCOUNT_PCT = Math.min(90, Math.max(0, Number(process.env.HERO_DISCOUNT_PCT ?? "0")));
const HERO_PER_USD_FALLBACK = Number(process.env.HERO_PER_USD || "0"); // static fallback

// Live $HERO/USD from Hero Run's market feed, cached 60s. Returns HERO-per-$1.
// Falls back to HERO_PER_USD if the feed is unreachable. Sanity-bounded so a
// broken feed can't quote nonsense.
let _heroRate = null;
async function heroPerUsd() {
  if (_heroRate && Date.now() - _heroRate.at < 60_000) return _heroRate.rate;
  try {
    const r = await fetch("https://herorunai.com/api/market", { signal: AbortSignal.timeout(3000) });
    const d = await r.json();
    const price = Number(d?.price ?? d?.priceUsd);
    const rate = 1 / price;
    if (Number.isFinite(rate) && rate >= 1e3 && rate <= 1e12) {
      _heroRate = { rate, at: Date.now() };
      return rate;
    }
  } catch { /* fall through */ }
  if (_heroRate) return _heroRate.rate;
  return HERO_PER_USD_FALLBACK;
}

// USD value → $HERO atomic amount (18dp) at the live rate, minus discount.
async function heroAtomicFor(usd) {
  const rate = await heroPerUsd();
  if (!rate) return null;
  const heroWhole = usd * rate * (1 - HERO_DISCOUNT_PCT / 100);
  // Round to 6dp then scale to 18 to avoid float drift in the atomic amount.
  return (BigInt(Math.round(heroWhole * 1e6)) * 10n ** 12n).toString();
}

// ---- key store (flat JSON) ------------------------------------------------
function loadKeys() { try { return JSON.parse(readFileSync(KEYS_FILE, "utf8")); } catch { return {}; } }
function saveKeys(k) { writeFileSync(KEYS_FILE, JSON.stringify(k, null, 2)); }
let keys = existsSync(KEYS_FILE) ? loadKeys() : {};

function mintKey(payer) {
  const key = "mcp_" + randomBytes(16).toString("hex");
  keys[key] = { balance_mcents: SIGNUP_GRANT, payer: payer || null, created_at: Date.now(), calls: 0 };
  saveKeys(keys);
  return key;
}

// ---- x402 quote for signup ($HERO only, live-priced) ----------------------
async function signupQuote(resource) {
  const heroAtomic = await heroAtomicFor(SIGNUP_USD);
  return {
    x402Version: 1,
    accepts: heroAtomic ? [{
      scheme: "exact", network: RHC, maxAmountRequired: heroAtomic, resource,
      description: `Arena key — mint once, spend per tool call. Paid in $HERO on Robinhood Chain.`,
      mimeType: "application/json", payTo: PAY_TO, maxTimeoutSeconds: 60, asset: HERO.asset,
      extra: { assetTransferMethod: HERO.method, name: HERO.name, version: HERO.version },
    }] : [],
    _hero_note: heroAtomic ? undefined : "HERO pricing unavailable (set HERO_PER_USD fallback)",
  };
}

// ---- helpers --------------------------------------------------------------
const json = (res, code, obj) => { res.writeHead(code, { "content-type": "application/json", "access-control-allow-origin": "*" }); res.end(JSON.stringify(obj)); };
const bearer = (req) => { const h = req.headers["authorization"] || ""; const m = /^Bearer\s+(mcp_[a-f0-9]+)/.exec(h); return m ? m[1] : null; };
async function readBody(req) { const chunks = []; for await (const c of req) chunks.push(c); return Buffer.concat(chunks).toString("utf8"); }

// ---- server ---------------------------------------------------------------
const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const p = url.pathname.replace(/\/$/, "") || "/";

  if (req.method === "OPTIONS") return json(res, 204, {});
  if (p === "/" || p === "/health") return json(res, 200, { ok: true, service: "arena-gateway", chain: "robinhood-chain:4663", mock: MOCK });

  if (p === "/v1/pricing" && req.method === "GET") {
    const rate = await heroPerUsd();
    const heroFor = (mc) => rate ? Math.round((mc / 100000) * rate * (1 - HERO_DISCOUNT_PCT / 100)) : null;
    return json(res, 200, {
      chain: "robinhood-chain", chain_id: 4663,
      currency: "HERO", token: HERO.asset,
      hero_per_usd: rate || null, discount_pct: HERO_DISCOUNT_PCT,
      signup: { price_usd: SIGNUP_USD, price_hero: heroFor(cfg.signup_price_mcents), grants_usd: SIGNUP_GRANT / 100000 },
      tools: Object.fromEntries(Object.entries(TOOL_PRICES).map(([t, mc]) => [t, { usd: mc / 100000, hero: heroFor(mc) }])),
      note: "100% $HERO. Free read-only tools are never charged. HERO amounts are live-priced from herorunai.com/api/market.",
    });
  }

  // ---- signup: x402 in $HERO on Robinhood Chain -----------------------------
  if (p === "/v1/signup" && req.method === "POST") {
    const resource = `http://${req.headers.host}/v1/signup`;
    const quote = await signupQuote(resource);
    const header = req.headers["x-payment"];
    if (!header) return json(res, 402, { ...quote, error: "Payment required" });
    if (!quote.accepts.length) return json(res, 503, { error: "HERO pricing unavailable; set HERO_PER_USD fallback" });

    let payment;
    try { payment = JSON.parse(Buffer.from(header, "base64").toString("utf8")); }
    catch { return json(res, 400, { error: "malformed X-PAYMENT header" }); }

    const entry = quote.accepts[0]; // HERO-only

    if (MOCK) {
      const inner = payment?.payload || {};
      const key = mintKey(inner.permit2Authorization?.from || inner.authorization?.from || "mock");
      return json(res, 200, { ok: true, key, balance_mcents: SIGNUP_GRANT, note: "MOCK mode — no on-chain settlement" });
    }
    if (!SETTLER) return json(res, 503, { error: "gateway not configured for payments (SETTLER_PRIVATE_KEY unset)" });

    // Permit2 (HERO) — tolerate live-price drift between quote and signature.
    const result = await settleSelf(payment, {
      asset: entry.asset, payTo: PAY_TO, maxAmountRequired: entry.maxAmountRequired,
      amountTolerancePct: 25,
    });
    if (!result.ok) return json(res, 402, { ...quote, error: result.error });
    const key = mintKey(result.payer);
    return json(res, 200, { ok: true, key, balance_mcents: SIGNUP_GRANT, payer: result.payer, settle_tx: result.txHash, paid_in: "HERO" });
  }

  // ---- balance --------------------------------------------------------------
  if (p === "/v1/balance" && req.method === "GET") {
    const key = bearer(req);
    if (!key || !keys[key]) return json(res, 401, { error: "invalid or missing key" });
    return json(res, 200, { ok: true, balance_mcents: keys[key].balance_mcents, calls: keys[key].calls });
  }

  // ---- per-tool charge: POST /v1/<tool> -------------------------------------
  const toolMatch = p.match(/^\/v1\/([a-z_]+)$/);
  if (toolMatch && req.method === "POST") {
    const tool = toolMatch[1];
    const price = TOOL_PRICES[tool];
    if (price == null) return json(res, 404, { error: `unknown tool: ${tool}` });
    const key = bearer(req);
    if (!key || !keys[key]) return json(res, 401, { error: "invalid or missing key" });
    const rec = keys[key];
    if (rec.balance_mcents < price) {
      return json(res, 402, { error: "insufficient balance", tool, price_mcents: price, balance_mcents: rec.balance_mcents, top_up: "POST /v1/signup to mint a fresh funded key" });
    }
    rec.balance_mcents -= price;
    rec.calls += 1;
    saveKeys(keys);
    return json(res, 200, { ok: true, tool, charged_mcents: price, balance_mcents: rec.balance_mcents });
  }

  return json(res, 404, { error: "not found" });
});

server.listen(PORT, () => {
  process.stderr.write(`arena-gateway on :${PORT} — chain=robinhood-chain:4663 mock=${MOCK} payTo=${PAY_TO}\n`);
  if (!MOCK && !SETTLER) process.stderr.write("  ⚠ SETTLER_PRIVATE_KEY unset — signup will 503 until configured (or set ARENA_MOCK=1 for local tests)\n");
});

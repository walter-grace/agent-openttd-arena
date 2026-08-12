#!/usr/bin/env node
// Arena payment gateway — Robinhood Chain edition.
//
// A self-hostable create-mcpay-style gateway for the OpenTTD agent arena.
// Agents pay once on Robinhood Chain (USDG gasless, or $HERO) to mint a key,
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
// the agent's brain and its in-game economy. USDG is the stable alternative.
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

// Robinhood Chain assets. USDG (Global Dollar, 6dp, gasless EIP-3009) and
// $HERO (HERORUN, 18dp, Permit2). Both self-settled by settle.mjs.
const RHC = "eip155:4663";
const USDG = { network: RHC, asset: "0x5fc5360d0400a0fd4f2af552add042d716f1d168", decimals: 6, symbol: "USDG", method: "eip3009", name: "Global Dollar", version: "1" };
const HERO = { network: RHC, asset: "0xba221e393645901c962ad21e4e7fa097d550b67c", decimals: 18, symbol: "HERO", method: "permit2", name: "HERORUN", version: "1" };

const cfg = JSON.parse(readFileSync(new URL("./tools.json", import.meta.url)));
const TOOL_PRICES = cfg.tools;
const SIGNUP_ATOMIC_USDG = String(cfg.signup_price_mcents / 100000 * 1e6); // $ → USDG atomic (6dp)
const SIGNUP_GRANT = cfg.signup_grants_mcents;

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

// ---- x402 quote for signup ------------------------------------------------
function signupQuote(resource) {
  // HERO amount is priced 1:1 at signup value for simplicity here; a live
  // deployment would fetch the HERO/USD rate (herorunai.com/api/market).
  const heroAtomic = "393356643357000000000000"; // ~$0.10 at ~2.3e-7 (illustrative default)
  const entry = (a, amount) => ({
    scheme: "exact", network: RHC, maxAmountRequired: amount, resource,
    description: `Arena key — mint once, spend per tool call (${a.symbol} on Robinhood Chain)`,
    mimeType: "application/json", payTo: PAY_TO, maxTimeoutSeconds: 60, asset: a.asset,
    extra: { assetTransferMethod: a.method, name: a.name, version: a.version },
  });
  return {
    x402Version: 1,
    accepts: [entry(USDG, SIGNUP_ATOMIC_USDG), entry(HERO, heroAtomic)],
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
    return json(res, 200, {
      chain: "robinhood-chain", chain_id: 4663,
      accepts: ["USDG (gasless)", "HERO (Permit2)"],
      signup: { price_usd: cfg.signup_price_mcents / 100000, grants_usd: SIGNUP_GRANT / 100000 },
      tools_mcents: TOOL_PRICES,
      note: "Prices in mcents (100000 = $1). Free read-only tools are never charged.",
    });
  }

  // ---- signup: x402 on Robinhood Chain --------------------------------------
  if (p === "/v1/signup" && req.method === "POST") {
    const resource = `http://${req.headers.host}/v1/signup`;
    const quote = signupQuote(resource);
    const header = req.headers["x-payment"];
    if (!header) return json(res, 402, { ...quote, error: "Payment required" });

    let payment;
    try { payment = JSON.parse(Buffer.from(header, "base64").toString("utf8")); }
    catch { return json(res, 400, { error: "malformed X-PAYMENT header" }); }

    const inner = payment?.payload || {};
    const asset = inner.permit2Authorization?.permitted?.token || inner.authorization?.to && payment?.asset || null;
    // Match the accepts[] entry the payer used (by token address in the payload).
    const paidToken = (inner.permit2Authorization?.permitted?.token || payment?.asset || "").toLowerCase();
    const entry = quote.accepts.find((a) => a.asset.toLowerCase() === paidToken) || quote.accepts[0];

    if (MOCK) {
      const key = mintKey(inner.permit2Authorization?.from || inner.authorization?.from || "mock");
      return json(res, 200, { ok: true, key, balance_mcents: SIGNUP_GRANT, note: "MOCK mode — no on-chain settlement" });
    }
    if (!SETTLER) return json(res, 503, { error: "gateway not configured for payments (SETTLER_PRIVATE_KEY unset)" });

    const result = await settleSelf(payment, {
      asset: entry.asset, payTo: PAY_TO, maxAmountRequired: entry.maxAmountRequired,
      amountTolerancePct: entry.extra.assetTransferMethod === "permit2" ? 20 : 0,
    });
    if (!result.ok) return json(res, 402, { ...quote, error: result.error });
    const key = mintKey(result.payer);
    return json(res, 200, { ok: true, key, balance_mcents: SIGNUP_GRANT, payer: result.payer, settle_tx: result.txHash });
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

// Arena Directory — the registry + $HERO-gated join for the OpenTTD agent
// arena marketplace. Cloudflare Worker, KV-backed.
//
// Two-sided:
//   Hosts  — anyone (agent or person) spins up an OpenTTD server + the
//            mcp_http bridge, then registers it here with a $HERO entry fee.
//   Joiners — agents browse open servers and pay $HERO to join; the entry
//            fee lands in the HOST's wallet. Hosts earn from their arena.
//
// The directory settles join payments itself on Robinhood Chain (settle.mjs)
// so no facilitator is needed. The platform runs one settler wallet (gas
// only); the money flows host↔joiner.
//
// Endpoints:
//   GET  /v1/servers                 public list of LIVE servers
//   GET  /v1/servers/:id             one server's detail
//   POST /v1/servers                 register a server → {id, host_token}
//   POST /v1/servers/:id/heartbeat   host_token → refresh liveness + stats
//   DELETE /v1/servers/:id           host_token → deregister
//   GET  /v1/servers/:id/join        x402 quote (live-priced $HERO entry)
//   POST /v1/servers/:id/join        X-PAYMENT → settle to host → join grant
//                                    { join_key, mcp_url }
//   GET  /health · GET /llms.txt
//
// Secrets/vars: SETTLER_PRIVATE_KEY (gas), HERO_PER_USD (fallback),
//   PLATFORM_NAME. KV binding: DIRECTORY.

import { settleSelf } from "../settle.mjs";

const HERO = { asset: "0xba221e393645901c962ad21e4e7fa097d550b67c", method: "permit2", name: "HERORUN", version: "1" };
const RHC = "eip155:4663";
const LIVE_WINDOW_MS = 5 * 60_000;   // a server is "live" if it heartbeat within 5 min
const MAX_ENTRY_USD = 100;           // sanity cap on a host's entry fee

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "access-control-allow-origin": "*" } });
const text = (s, ct = "text/plain") =>
  new Response(s, { headers: { "content-type": ct, "access-control-allow-origin": "*" } });
const rand = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, "0")).join("");
const isAddr = (a) => /^0x[a-fA-F0-9]{40}$/.test(a || "");

// ---- live HERO/USD (60s cache per isolate) --------------------------------
let _rate = null;
async function heroPerUsd(env) {
  const fb = Number(env.HERO_PER_USD || "0");
  if (_rate && Date.now() - _rate.at < 60_000) return _rate.rate;
  try {
    const r = await fetch("https://herorunai.com/api/market", { signal: AbortSignal.timeout(3000) });
    const rate = 1 / Number((await r.json())?.price);
    if (Number.isFinite(rate) && rate >= 1e3 && rate <= 1e12) { _rate = { rate, at: Date.now() }; return rate; }
  } catch { /* fall through */ }
  return _rate ? _rate.rate : fb;
}
async function heroAtomicFor(env, usd) {
  const rate = await heroPerUsd(env);
  if (!rate) return null;
  return (BigInt(Math.round(usd * rate * 1e6)) * 10n ** 12n).toString();
}

async function listServers(env, { liveOnly = true } = {}) {
  const out = [];
  let cursor;
  for (;;) {
    const page = await env.DIRECTORY.list({ prefix: "srv:", cursor });
    for (const k of page.keys) {
      const s = JSON.parse((await env.DIRECTORY.get(k.name)) || "null");
      if (!s) continue;
      const live = Date.now() - (s.last_heartbeat || 0) < LIVE_WINDOW_MS;
      if (liveOnly && !live) continue;
      out.push(publicView(s, live));
    }
    if (page.list_complete || !page.cursor) break;
    cursor = page.cursor;
  }
  out.sort((a, b) => (b.used_slots - a.used_slots) || (b.created - a.created));
  return out;
}

function publicView(s, live) {
  return {
    id: s.id, name: s.name, region: s.region, description: s.description,
    entry_usd: s.entry_usd, mcp_url: s.mcp_url, host_wallet: s.host_wallet,
    max_slots: s.max_slots, used_slots: s.used_slots || 0,
    open_slots: Math.max(0, s.max_slots - (s.used_slots || 0)),
    economy: s.economy || null, year: s.year || null,
    live, last_heartbeat: s.last_heartbeat || null, created: s.created,
  };
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname.replace(/\/$/, "") || "/";
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "authorization,content-type", "access-control-allow-methods": "GET,POST,DELETE,OPTIONS" } });
    if (p === "/" || p === "/health") return json({ ok: true, service: "arena-directory", chain: "robinhood-chain:4663", currency: "HERO" });

    if (p === "/llms.txt") {
      return text(
`# ${env.PLATFORM_NAME || "Arena Directory"}\n\n` +
`A marketplace of OpenTTD agent arenas. Hosts register servers with a $HERO entry fee; agents pay $HERO to join and play.\n\n` +
`## For joining agents\n` +
`- GET  ${url.origin}/v1/servers          — list live servers (name, region, open_slots, entry_usd, mcp_url)\n` +
`- GET  ${url.origin}/v1/servers/:id/join — x402 quote (live-priced $HERO entry fee)\n` +
`- POST ${url.origin}/v1/servers/:id/join — pay via x402 (X-PAYMENT header, HERO on Robinhood Chain) → { join_key, mcp_url }\n` +
`  Then connect an MCP client to mcp_url with Authorization: Bearer <join_key>.\n\n` +
`## For hosting agents\n` +
`- POST ${url.origin}/v1/servers          — register your server { name, mcp_url, entry_usd, region, max_slots, host_wallet }\n` +
`- POST ${url.origin}/v1/servers/:id/heartbeat — keep it listed (every <5 min) + report slots/economy\n\n` +
`Payment: x402 on Robinhood Chain (chain 4663), $HERO only, self-settled. Entry fees go to the host's wallet.\n`);
    }

    // ---- list / detail --------------------------------------------------
    if (p === "/v1/servers" && req.method === "GET") {
      return json({ ok: true, servers: await listServers(env, { liveOnly: url.searchParams.get("all") !== "1" }) });
    }
    const detail = p.match(/^\/v1\/servers\/([a-z0-9]+)$/);
    if (detail && req.method === "GET") {
      const s = JSON.parse((await env.DIRECTORY.get(`srv:${detail[1]}`)) || "null");
      if (!s) return json({ error: "server not found" }, 404);
      return json({ ok: true, server: publicView(s, Date.now() - (s.last_heartbeat || 0) < LIVE_WINDOW_MS) });
    }

    // ---- register -------------------------------------------------------
    if (p === "/v1/servers" && req.method === "POST") {
      const b = await req.json().catch(() => ({}));
      const name = String(b.name || "").trim().slice(0, 60);
      const mcp_url = String(b.mcp_url || "").trim();
      const entry_usd = Number(b.entry_usd);
      const host_wallet = String(b.host_wallet || "").trim();
      const max_slots = Math.max(1, Math.min(64, Number(b.max_slots) || 8));
      if (!name) return json({ error: "name required" }, 400);
      if (!/^https?:\/\//.test(mcp_url)) return json({ error: "mcp_url must be an http(s) URL to your mcp_http bridge" }, 400);
      if (!isAddr(host_wallet)) return json({ error: "host_wallet must be a 0x address (receives entry fees)" }, 400);
      if (!(entry_usd >= 0 && entry_usd <= MAX_ENTRY_USD)) return json({ error: `entry_usd must be 0..${MAX_ENTRY_USD}` }, 400);
      const id = rand(8);
      const host_token = "host_" + rand(20);
      const srv = {
        id, name, mcp_url, entry_usd, host_wallet, max_slots,
        region: String(b.region || "").slice(0, 40), description: String(b.description || "").slice(0, 200),
        used_slots: 0, economy: null, year: null,
        host_token, created: Date.now(), last_heartbeat: Date.now(),
      };
      await env.DIRECTORY.put(`srv:${id}`, JSON.stringify(srv));
      return json({ ok: true, id, host_token, listing: `${url.origin}/v1/servers/${id}`, note: "save host_token — heartbeat every <5 min to stay listed" });
    }

    // ---- heartbeat / deregister (host_token) ----------------------------
    const hb = p.match(/^\/v1\/servers\/([a-z0-9]+)\/heartbeat$/);
    if (hb && req.method === "POST") {
      const s = JSON.parse((await env.DIRECTORY.get(`srv:${hb[1]}`)) || "null");
      if (!s) return json({ error: "server not found" }, 404);
      const tok = (req.headers.get("authorization") || "").replace(/^Bearer\s+/, "");
      if (tok !== s.host_token) return json({ error: "bad host_token" }, 401);
      const b = await req.json().catch(() => ({}));
      if (Number.isFinite(b.used_slots)) s.used_slots = Math.max(0, Math.min(s.max_slots, Number(b.used_slots)));
      if (Number.isFinite(b.economy)) s.economy = Number(b.economy);
      if (Number.isFinite(b.year)) s.year = Number(b.year);
      s.last_heartbeat = Date.now();
      await env.DIRECTORY.put(`srv:${s.id}`, JSON.stringify(s));
      return json({ ok: true, live: true });
    }
    if (detail && req.method === "DELETE") {
      const s = JSON.parse((await env.DIRECTORY.get(`srv:${detail[1]}`)) || "null");
      if (!s) return json({ error: "server not found" }, 404);
      const tok = (req.headers.get("authorization") || "").replace(/^Bearer\s+/, "");
      if (tok !== s.host_token) return json({ error: "bad host_token" }, 401);
      await env.DIRECTORY.delete(`srv:${s.id}`);
      return json({ ok: true, deregistered: s.id });
    }

    // ---- join: x402 $HERO entry fee → host wallet -----------------------
    const join = p.match(/^\/v1\/servers\/([a-z0-9]+)\/join$/);
    if (join) {
      const s = JSON.parse((await env.DIRECTORY.get(`srv:${join[1]}`)) || "null");
      if (!s) return json({ error: "server not found" }, 404);
      const resource = `${url.origin}/v1/servers/${s.id}/join`;
      const heroAtomic = await heroAtomicFor(env, s.entry_usd);
      const quote = {
        x402Version: 1,
        accepts: (s.entry_usd > 0 && heroAtomic) ? [{
          scheme: "exact", network: RHC, maxAmountRequired: heroAtomic, resource,
          description: `Join "${s.name}": entry fee in $HERO, paid to the host.`,
          mimeType: "application/json", payTo: s.host_wallet, maxTimeoutSeconds: 60, asset: HERO.asset,
          extra: { assetTransferMethod: HERO.method, name: HERO.name, version: HERO.version },
        }] : [],
        server: publicView(s, true),
      };

      if (req.method === "GET") return json(quote);
      if (req.method !== "POST") return json({ error: "use GET (quote) or POST (pay)" }, 405);

      if (Math.max(0, s.max_slots - (s.used_slots || 0)) <= 0) return json({ error: "server full" }, 409);

      // Free entry (entry_usd = 0): mint a join key immediately.
      const grantJoin = async () => {
        const join_key = "mcp_" + rand(16);
        await env.DIRECTORY.put(`join:${join_key}`, JSON.stringify({ server: s.id, at: Date.now() }), { expirationTtl: 86400 });
        return json({ ok: true, join_key, mcp_url: s.mcp_url, server: s.name, note: "connect your MCP client to mcp_url with Authorization: Bearer <join_key>" });
      };
      if (s.entry_usd === 0) return grantJoin();

      const header = req.headers.get("x-payment");
      if (!header) return json({ ...quote, error: "Payment required" }, 402);
      if (!env.SETTLER_PRIVATE_KEY) return json({ error: "directory not configured (SETTLER_PRIVATE_KEY unset)" }, 503);
      let payment;
      try { payment = JSON.parse(atob(header)); } catch { return json({ error: "malformed X-PAYMENT header" }, 400); }
      const result = await settleSelf(payment, {
        asset: HERO.asset, payTo: s.host_wallet, maxAmountRequired: heroAtomic, amountTolerancePct: 25,
      }, { settlerKey: env.SETTLER_PRIVATE_KEY, rpc: env.RH_RPC || "https://rpc.mainnet.chain.robinhood.com" });
      if (!result.ok) return json({ ...quote, error: result.error }, 402);
      const resp = await grantJoin();
      return json({ ...(await resp.json()), payer: result.payer, entry_tx: result.txHash, paid_in: "HERO" });
    }

    return json({ error: "not found" }, 404);
  },
};

#!/usr/bin/env node
// End-to-end test of the arena gateway in ARENA_MOCK mode: quote → sign →
// mint key → call a paid tool → verify balance debits → hit the
// insufficient-balance path. Real-money flow is identical minus the mock
// skip (settle.mjs settles on Robinhood Chain instead of no-op).
//
// Run: ARENA_MOCK=1 node e2e.mjs   (spawns its own gateway on a temp port)

import { spawn } from "node:child_process";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { getQuote, payEip3009, payPermit2, payAndCall } from "./lib/x402.mjs";

const PORT = 8799;
const BASE = `http://localhost:${PORT}`;
const acct = privateKeyToAccount(generatePrivateKey());
let pass = 0, fail = 0;
const check = (name, cond, detail = "") => { if (cond) { pass++; console.log(`  ✅ ${name} ${detail}`); } else { fail++; console.log(`  ❌ ${name} ${detail}`); } };

const gw = spawn("node", ["server.mjs"], {
  cwd: new URL(".", import.meta.url).pathname,
  env: { ...process.env, ARENA_MOCK: "1", PORT: String(PORT), KEYS_FILE: "/tmp/arena-e2e-keys.json", PAY_TO: "0x20Fb0619b6D23F8463d41C938Fc92115cC89406E" },
  stdio: ["ignore", "ignore", "inherit"],
});
const done = (code) => { try { gw.kill(); } catch {} process.exit(code); };

await new Promise((r) => setTimeout(r, 700));
try {
  // 1. Quote
  const { status, body } = await getQuote(`${BASE}/v1/signup`);
  check("signup returns 402 quote", status === 402, `(${status})`);
  check("quote lists USDG + HERO on RHC", (body.accepts || []).length === 2 && body.accepts.every((a) => a.network === "eip155:4663"));

  // 2. Sign + mint (mock skips settlement, but we sign a real payload)
  const usdg = body.accepts.find((a) => a.extra.assetTransferMethod === "eip3009");
  const header = await payEip3009(acct, usdg);
  const minted = await payAndCall(`${BASE}/v1/signup`, header, {});
  check("signup mints a key", !!minted.body?.key, minted.body?.key ? `key=${minted.body.key.slice(0, 12)}…` : JSON.stringify(minted.body));
  const key = minted.body.key;
  const startBal = minted.body.balance_mcents;
  check("starter balance granted", startBal > 0, `(${startBal} mcents)`);

  const auth = { Authorization: `Bearer ${key}`, "Content-Type": "application/json" };

  // 3. Paid tool call debits
  const r1 = await fetch(`${BASE}/v1/dispatch_route`, { method: "POST", headers: auth, body: "{}" });
  const d1 = await r1.json();
  check("dispatch_route charges + returns 200", r1.status === 200 && d1.charged_mcents === 5000, `(charged ${d1.charged_mcents})`);
  check("balance debited by price", d1.balance_mcents === startBal - 5000, `(${d1.balance_mcents})`);

  // 4. Cheap tool
  const r2 = await fetch(`${BASE}/v1/send_chat`, { method: "POST", headers: auth, body: "{}" });
  const d2 = await r2.json();
  check("send_chat cheap charge", r2.status === 200 && d2.charged_mcents === 100);

  // 5. Unknown tool → 404 (free/observation tools aren't charged here)
  const r3 = await fetch(`${BASE}/v1/game_state`, { method: "POST", headers: auth, body: "{}" });
  check("non-priced tool → 404 (never charged)", r3.status === 404);

  // 6. Drain to insufficient balance
  let bal = d2.balance_mcents;
  while (bal >= 10000) { const r = await fetch(`${BASE}/v1/rcon`, { method: "POST", headers: auth, body: "{}" }); bal = (await r.json()).balance_mcents; }
  const rDrain = await fetch(`${BASE}/v1/rcon`, { method: "POST", headers: auth, body: "{}" });
  check("insufficient balance → 402", rDrain.status === 402, `(${rDrain.status})`);

  // 7. Bad key → 401
  const rBad = await fetch(`${BASE}/v1/dispatch_route`, { method: "POST", headers: { Authorization: "Bearer mcp_deadbeef" }, body: "{}" });
  check("unknown key → 401", rBad.status === 401);

  console.log(`\n${pass} passed, ${fail} failed`);
  done(fail ? 1 : 0);
} catch (e) {
  console.error("e2e threw:", e.message);
  done(1);
}

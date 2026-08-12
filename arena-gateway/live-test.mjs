#!/usr/bin/env node
// Real on-chain $HERO signup against a live (non-mock) gateway. Spends a
// small amount of real HERO on Robinhood Chain, self-settled. Uses the
// funded e2e wallets in ~/.dlf/e2e-wallets.json (payer signs, settler
// submits). Run: node live-test.mjs
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { privateKeyToAccount } from "viem/accounts";
import { getQuote, payPermit2, payAndCall } from "./lib/x402.mjs";

const w = JSON.parse(readFileSync(`${homedir()}/.dlf/e2e-wallets.json`, "utf8"));
const acct = privateKeyToAccount(w.payer.pk);
const PORT = 8791, BASE = `http://localhost:${PORT}`;
const REVENUE = "0x20Fb0619b6D23F8463d41C938Fc92115cC89406E";

const gw = spawn("node", ["server.mjs"], {
  cwd: new URL(".", import.meta.url).pathname,
  env: {
    ...process.env, PORT: String(PORT), KEYS_FILE: "/tmp/arena-live-keys.json",
    PAY_TO: REVENUE, SETTLER_PRIVATE_KEY: w.settler.pk,
    SIGNUP_PRICE_USD: "0.01", // ~43,700 HERO — the test wallet covers it
  },
  stdio: ["ignore", "ignore", "inherit"],
});
const done = (c) => { try { gw.kill(); } catch {} process.exit(c); };
await new Promise((r) => setTimeout(r, 800));

try {
  const { body } = await getQuote(`${BASE}/v1/signup`);
  const hero = body.accepts[0];
  console.log(`quote: ${(Number(hero.maxAmountRequired) / 1e18).toLocaleString()} HERO (~$0.01)`);
  const header = await payPermit2(acct, hero);
  console.log("signing + submitting (settler settles on-chain)…");
  const paid = await payAndCall(`${BASE}/v1/signup`, header, {});
  if (!paid.body?.key) { console.error("FAILED:", JSON.stringify(paid.body)); done(1); }
  console.log(`✓ KEY MINTED: ${paid.body.key}`);
  console.log(`  paid_in: ${paid.body.paid_in}  balance: ${paid.body.balance_mcents} mcents`);
  console.log(`  SETTLE TX: https://robinhoodchain.blockscout.com/tx/${paid.body.settle_tx}`);
  // Spend it on a tool.
  const r = await fetch(`${BASE}/v1/send_chat`, { method: "POST", headers: { Authorization: `Bearer ${paid.body.key}` }, body: "{}" });
  const d = await r.json();
  console.log(`✓ send_chat charged ${d.charged_mcents} mcents → balance ${d.balance_mcents} (dispatch_route would 402 — signup was only $0.01)`);
  done(0);
} catch (e) { console.error("threw:", e.message); done(1); }

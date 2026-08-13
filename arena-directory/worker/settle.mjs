// Self-hosted x402 settlement for Robinhood Chain.
//
// No public x402 facilitator settles Robinhood Chain (chain 4663), so your
// server becomes its own facilitator with this module. Security model: the
// on-chain contracts verify every signature. We simulate first (invalid
// payments revert at zero cost), then submit with your settler wallet and
// credit the buyer only after a successful receipt. The Permit2 proxy's
// witness pattern pins the payment destination, so even a leaked settler
// key cannot redirect funds.
//
// Two transfer methods, routed automatically by payload shape:
//   payload.authorization         → EIP-3009 transferWithAuthorization,
//                                   direct on the token (USDG supports it;
//                                   gasless for the payer)
//   payload.permit2Authorization  → Permit2 witness via x402ExactPermit2Proxy
//                                   (works for ANY ERC-20, e.g. HERO; payer
//                                   approves Permit2 once)
//
// Config (env or options): SETTLER_PRIVATE_KEY (funded with a little RHC
// ETH; each settlement costs ~0.000004 ETH), RH_RPC (optional override).

import {
  createPublicClient, createWalletClient, http, parseAbi,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { robinhoodChain } from "./lib/chains.mjs";

export const RHC_NETWORK_ID = "eip155:4663";
const eqStr = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
export const PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3";
export const X402_PERMIT2_PROXY = "0x402085c248EeA27D92E8b30b2C58ed07f9E20001";

const PROXY_ABI = parseAbi([
  "function settle(((address token, uint256 amount) permitted, uint256 nonce, uint256 deadline) permit, address owner, (address to, uint256 validAfter) witness, bytes signature)",
]);
const EIP3009_VRS = parseAbi([
  "function transferWithAuthorization(address from, address to, uint256 value, uint256 validAfter, uint256 validBefore, bytes32 nonce, uint8 v, bytes32 r, bytes32 s)",
]);
const EIP3009_BYTES = parseAbi([
  "function transferWithAuthorization(address from, address to, uint256 value, uint256 validAfter, uint256 validBefore, bytes32 nonce, bytes signature)",
]);

/**
 * Verify + settle an x402 payment on Robinhood Chain.
 *
 * @param {object} payment  decoded X-PAYMENT header JSON (x402 v1)
 * @param {object} expected what YOUR quote demanded:
 *   { asset, payTo, maxAmountRequired, amountTolerancePct? }
 * @param {object} [opts]   { settlerKey?, rpc? } (defaults to env)
 * @returns {Promise<{ok: boolean, payer?: string, txHash?: string, error?: string}>}
 */
export async function settleOnRobinhood(payment, expected, opts = {}) {
  const pk = opts.settlerKey || process.env.SETTLER_PRIVATE_KEY;
  if (!pk) return { ok: false, error: "SETTLER_PRIVATE_KEY not configured" };
  const rpc = opts.rpc || process.env.RH_RPC || robinhoodChain.rpcUrls.default.http[0];
  const inner = payment?.payload;
  if (!inner) return { ok: false, error: "malformed payment (no payload)" };

  // Reject a payment addressed to another chain or another scheme before touching a wallet.
  //
  // The contracts would catch this anyway — an EIP-712 signature is domain-separated by chainId, so
  // one signed for Base cannot validate on 4663 — but relying on that means paying gas to discover
  // it and returning an opaque revert instead of a clear reason. RHC_NETWORK_ID was already exported
  // for exactly this check and then never used, which is how the gap got here.
  const wantNetwork = opts.network || RHC_NETWORK_ID;
  if (payment.network && !eqStr(payment.network, wantNetwork)) {
    return { ok: false, error: `payment is for ${payment.network}, this settler is ${wantNetwork}` };
  }
  if (payment.scheme && !eqStr(payment.scheme, "exact")) {
    return { ok: false, error: `unsupported x402 scheme "${payment.scheme}" (this settles "exact")` };
  }

  const account = privateKeyToAccount(pk.startsWith("0x") ? pk : `0x${pk}`);
  const pub = createPublicClient({ chain: robinhoodChain, transport: http(rpc) });
  const wallet = createWalletClient({ account, chain: robinhoodChain, transport: http(rpc) });
  const eq = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
  const nowSec = Math.floor(Date.now() / 1000);

  // If your quote uses dynamic pricing, the signed amount can lag the current quote. Accept within
  // tolerance (default: exact).
  //
  // CLAMPED, because the unclamped version gave away goods for free. `100 - tolerancePct` goes
  // NEGATIVE at 100 or above, which makes `signed * 100 >= required * negative` true for EVERY
  // amount including zero: a server configured with amountTolerancePct: 100 would accept a payment
  // of nothing and deliver. A non-numeric value threw a RangeError out of BigInt() instead of being
  // rejected. Both are operator mistakes rather than attacks, but both are silent and both cost
  // money, so they are handled here rather than documented.
  const rawTol = Number(expected.amountTolerancePct ?? 0);
  const tolerancePct = Number.isFinite(rawTol) ? Math.min(99, Math.max(0, Math.trunc(rawTol))) : 0;
  const amountOk = (signed) => {
    let s;
    try { s = BigInt(String(signed)); } catch { return false; } // unparseable amount is not a payment
    if (s <= 0n) return false;                                   // zero never settles a quote
    const req = BigInt(String(expected.maxAmountRequired));
    return s * 100n >= req * BigInt(100 - tolerancePct);
  };

  const run = async (address, abi, args) => {
    await pub.simulateContract({ address, abi, functionName: abi === PROXY_ABI ? "settle" : "transferWithAuthorization", args, account });
    const txHash = await wallet.writeContract({ address, abi, functionName: abi === PROXY_ABI ? "settle" : "transferWithAuthorization", args });
    const receipt = await pub.waitForTransactionReceipt({ hash: txHash, timeout: 45_000 });
    if (receipt.status !== "success") throw Object.assign(new Error("settlement reverted"), { txHash });
    return txHash;
  };

  try {
    if (inner.permit2Authorization) {
      const a = inner.permit2Authorization;
      if (!eq(a.permitted?.token, expected.asset)) return { ok: false, error: "wrong token" };
      if (!eq(a.witness?.to, expected.payTo)) return { ok: false, error: "witness.to != payTo" };
      if (!amountOk(a.permitted?.amount)) return { ok: false, error: "amount below quote" };
      if (Number(a.deadline) <= nowSec) return { ok: false, error: "permit expired" };
      const txHash = await run(X402_PERMIT2_PROXY, PROXY_ABI, [
        { permitted: { token: a.permitted.token, amount: BigInt(a.permitted.amount) }, nonce: BigInt(a.nonce), deadline: BigInt(a.deadline) },
        a.from,
        { to: a.witness.to, validAfter: BigInt(a.witness.validAfter ?? 0) },
        inner.signature,
      ]);
      return { ok: true, payer: a.from, txHash };
    }

    if (inner.authorization) {
      const a = inner.authorization;
      if (!eq(a.to, expected.payTo)) return { ok: false, error: "authorization.to != payTo" };
      if (!amountOk(a.value)) return { ok: false, error: "amount below quote" };
      if (Number(a.validBefore) <= nowSec) return { ok: false, error: "authorization expired" };
      const sig = inner.signature.replace(/^0x/, "");
      const base = [a.from, a.to, BigInt(a.value), BigInt(a.validAfter ?? 0), BigInt(a.validBefore), a.nonce];
      // Implementations vary: try (v, r, s) then the bytes overload.
      try {
        const txHash = await run(expected.asset, EIP3009_VRS, [
          ...base, parseInt(sig.slice(128, 130), 16), `0x${sig.slice(0, 64)}`, `0x${sig.slice(64, 128)}`,
        ]);
        return { ok: true, payer: a.from, txHash };
      } catch {
        const txHash = await run(expected.asset, EIP3009_BYTES, [...base, inner.signature]);
        return { ok: true, payer: a.from, txHash };
      }
    }

    return { ok: false, error: "unrecognized payment payload (need authorization or permit2Authorization)" };
  } catch (e) {
    return { ok: false, error: (e?.shortMessage || e?.message || "settlement failed").slice(0, 300), txHash: e?.txHash };
  }
}

// Neutral alias — the library is chain-agnostic; `settleSelf` reads better
// than the original name. Both are supported.
export const settleSelf = settleOnRobinhood;

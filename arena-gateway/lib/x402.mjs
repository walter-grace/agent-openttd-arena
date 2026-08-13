// x402 client helpers for the E2E harness: fetch quotes, sign payments,
// build X-PAYMENT headers. Two signing paths mirror the gateway's two
// settlement paths: EIP-3009 (USDC) and Permit2-with-witness (HERO).

import { privateKeyToAccount } from "viem/accounts";

export const USDC_DOMAIN_VERSION = "2";
export const PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3";
export const X402_PERMIT2_PROXY = "0x402085c248EeA27D92E8b30b2C58ed07f9E20001";

export const CHAIN_IDS = { base: 8453, polygon: 137, arbitrum: 42161, "eip155:4663": 4663 };

export function account(env = process.env) {
  const pk = env.E2E_PRIVATE_KEY;
  if (!pk) return null;
  return privateKeyToAccount(pk.startsWith("0x") ? pk : `0x${pk}`);
}

// POST with no payment → parse the 402 quote body.
export async function getQuote(url) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const body = await r.json().catch(() => null);
  return { status: r.status, body };
}

export function randomHex32() {
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  return "0x" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

const b64 = (obj) => Buffer.from(JSON.stringify(obj)).toString("base64");

// EIP-3009 transferWithAuthorization signature → X-PAYMENT header.
// Works for any accepts[] entry whose asset natively supports 3009 (USDC).
export async function payEip3009(acct, entry) {
  const chainId = CHAIN_IDS[entry.network];
  if (!chainId) throw new Error(`unknown network ${entry.network}`);
  const now = Math.floor(Date.now() / 1000);
  const authorization = {
    from: acct.address,
    to: entry.payTo,
    value: entry.maxAmountRequired,
    validAfter: String(now - 60),
    validBefore: String(now + (entry.maxTimeoutSeconds || 60) + 60),
    nonce: randomHex32(),
  };
  const signature = await acct.signTypedData({
    domain: {
      name: entry.extra?.name || "USD Coin",
      version: entry.extra?.version || USDC_DOMAIN_VERSION,
      chainId,
      verifyingContract: entry.asset,
    },
    types: {
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" },
      ],
    },
    primaryType: "TransferWithAuthorization",
    message: {
      from: authorization.from,
      to: authorization.to,
      value: BigInt(authorization.value),
      validAfter: BigInt(authorization.validAfter),
      validBefore: BigInt(authorization.validBefore),
      nonce: authorization.nonce,
    },
  });
  return b64({
    x402Version: 1,
    scheme: "exact",
    network: entry.network,
    payload: { signature, authorization },
  });
}

// Permit2 permitWitnessTransferFrom signature (witness pins the recipient)
// → X-PAYMENT header. Matches x402ExactPermit2Proxy's WITNESS_TYPE_STRING:
//   Witness(address to,uint256 validAfter)
// Payer prerequisite: one-time ERC-20 approve(PERMIT2_ADDRESS, max).
export async function payPermit2(acct, entry) {
  const chainId = CHAIN_IDS[entry.network];
  if (!chainId) throw new Error(`unknown network ${entry.network}`);
  const now = Math.floor(Date.now() / 1000);
  const nonce = BigInt(randomHex32());
  const deadline = BigInt(now + (entry.maxTimeoutSeconds || 60) + 120);
  const validAfter = BigInt(now - 60);
  const message = {
    permitted: { token: entry.asset, amount: BigInt(entry.maxAmountRequired) },
    spender: X402_PERMIT2_PROXY,
    nonce,
    deadline,
    witness: { to: entry.payTo, validAfter },
  };
  const signature = await acct.signTypedData({
    domain: { name: "Permit2", chainId, verifyingContract: PERMIT2_ADDRESS },
    types: {
      PermitWitnessTransferFrom: [
        { name: "permitted", type: "TokenPermissions" },
        { name: "spender", type: "address" },
        { name: "nonce", type: "uint256" },
        { name: "deadline", type: "uint256" },
        { name: "witness", type: "Witness" },
      ],
      TokenPermissions: [
        { name: "token", type: "address" },
        { name: "amount", type: "uint256" },
      ],
      Witness: [
        { name: "to", type: "address" },
        { name: "validAfter", type: "uint256" },
      ],
    },
    primaryType: "PermitWitnessTransferFrom",
    message,
  });
  return b64({
    x402Version: 1,
    scheme: "exact",
    network: entry.network,
    payload: {
      signature,
      permit2Authorization: {
        permitted: { token: entry.asset, amount: String(entry.maxAmountRequired) },
        from: acct.address,
        spender: X402_PERMIT2_PROXY,
        nonce: nonce.toString(),
        deadline: deadline.toString(),
        witness: { to: entry.payTo, validAfter: validAfter.toString() },
      },
    },
  });
}

// Retry the paid POST with the signed header attached.
export async function payAndCall(url, header, body = {}) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PAYMENT": header },
    body: JSON.stringify(body),
  });
  return { status: r.status, body: await r.json().catch(() => null) };
}

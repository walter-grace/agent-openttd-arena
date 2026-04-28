"""x402 payment gate for the OpenTTD MCP server.

Adds optional pay-per-tool-call gating in front of the MCP tool dispatcher.
Free tools (read-only observation) always pass. Paid tools require an x402
bearer token in the tool args under the special key ``_payment_token`` —
because stdio MCP has no HTTP headers, so we piggyback on the JSON args.

Designed to pair with `create-mcpay` (https://github.com/walter-grace/create-mcpay)
which provides a Cloudflare Worker gateway speaking x402-compatible bearer
auth + atomic charging via a Durable Object. The gate POSTs the bearer to
the Worker; the Worker debits the balance and returns 200/402/401. We treat
the Worker as the source of truth for payment.

Modes (via ``X402_MODE`` env var):
    ``disabled`` (default) — no payment required, all tools work freely.
                             Keeps the OSS repo runnable without setup.
    ``gateway``  — verify each paid tool call against ``X402_GATEWAY_URL``.
                   Requires ``X402_GATEWAY_URL`` to be set. Real-money mode.
    ``mock``     — accept any token starting with ``mcp_test_`` as paid;
                   reject anything else. For local end-to-end tests without
                   spinning up a Worker.

Legacy alias: ``X402_ENABLED=true`` is treated as ``X402_MODE=gateway`` if
``X402_MODE`` is unset. (Matches the spec the user wrote.)

Env vars:
    X402_MODE                 disabled | gateway | mock   (default: disabled)
    X402_ENABLED              legacy boolean alias for gateway mode
    X402_GATEWAY_URL          base URL of a create-mcpay Worker, e.g.
                              https://your-worker.workers.dev
    X402_RECIPIENT_ADDRESS    Base USDC wallet that ultimately receives funds
                              (informational — set on the Worker side too)
    X402_TIMEOUT_S            HTTP timeout to gateway in seconds (default: 5)

Stdlib-only. No external deps. Safe to import even when disabled.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Pricing (USDC).
# ---------------------------------------------------------------------------
# Free tools are observation/read-only — agents can window-shop before paying.
# Paid tools are anything that mutates the world or impacts other players.
# Numbers below are starting points; adjust via PRICES_OVERRIDE_JSON env var
# if your operator economics differ.

FREE_TOOLS = {
    "game_state",
    "list_companies",
    "list_towns",
    "list_vehicles",
    "list_stations",
}

# Price in USDC. mcent (1/1000¢) is the create-mcpay native unit; we expose
# USDC here for human-friendly config and convert at the boundary.
DEFAULT_PRICES_USDC: dict[str, float] = {
    "dispatch_route": 0.05,    # most valuable action — actually changes the world
    "send_chat":      0.001,   # cheap but non-zero — anti-spam
    "pause":          0.01,    # can grief other players
    "unpause":        0.01,
    "rcon":           0.10,    # escape hatch — most expensive
    "fund_town":      0.005,   # advisory only, but charge to discourage abuse
}


def _load_prices() -> dict[str, float]:
    raw = os.environ.get("X402_PRICES_OVERRIDE_JSON")
    if not raw:
        return dict(DEFAULT_PRICES_USDC)
    try:
        override = json.loads(raw)
        if isinstance(override, dict):
            merged = dict(DEFAULT_PRICES_USDC)
            for k, v in override.items():
                if isinstance(v, (int, float)) and v >= 0:
                    merged[k] = float(v)
            return merged
    except Exception:
        pass
    return dict(DEFAULT_PRICES_USDC)


PRICES: dict[str, float] = _load_prices()


def usdc_to_mcents(usdc: float) -> int:
    """1 USDC = 100 cents = 100_000 mcents (1 mcent = 1/1000¢)."""
    return int(round(usdc * 100_000))


# ---------------------------------------------------------------------------
# Mode resolution.
# ---------------------------------------------------------------------------

def _mode() -> str:
    mode = os.environ.get("X402_MODE", "").strip().lower()
    if mode in ("disabled", "gateway", "mock"):
        return mode
    # legacy boolean alias
    legacy = os.environ.get("X402_ENABLED", "").strip().lower()
    if legacy in ("1", "true", "yes", "on"):
        return "gateway"
    return "disabled"


def is_paid_tool(name: str) -> bool:
    return name in PRICES


def is_free_tool(name: str) -> bool:
    return name in FREE_TOOLS


def price_usdc(name: str) -> float:
    return PRICES.get(name, 0.0)


# ---------------------------------------------------------------------------
# Verification.
# ---------------------------------------------------------------------------
# Returns (ok: bool, error_payload: dict | None).
# When ok=True, error_payload is None and the caller proceeds.
# When ok=False, the caller returns the error_payload as the tool result.
# Error payload shape mirrors x402: {"error": "402 Payment Required", ...}

def check_payment(tool_name: str, args: dict[str, Any]) -> tuple[bool, dict | None]:
    """Gate a tool call. Free tools always pass.

    For paid tools, looks for `_payment_token` in args. In gateway mode,
    POSTs to the create-mcpay Worker for atomic charge. In mock mode,
    accepts any `mcp_test_*` token.

    The `_payment_token` key is removed from args by the caller (handled
    in the MCP dispatcher) so it never leaks into tool implementations.
    """
    # Free tools and unknown tools (no price configured) always pass.
    if tool_name in FREE_TOOLS or not is_paid_tool(tool_name):
        return True, None

    mode = _mode()
    if mode == "disabled":
        return True, None

    price_u = price_usdc(tool_name)
    token = args.get("_payment_token")
    if not isinstance(token, str) or not token:
        return False, _payment_required(tool_name, price_u)

    if mode == "mock":
        if token.startswith("mcp_test_"):
            return True, None
        return False, _payment_invalid(tool_name, price_u, "mock mode rejects non-test tokens")

    # gateway mode
    gateway_url = os.environ.get("X402_GATEWAY_URL", "").rstrip("/")
    if not gateway_url:
        return False, _payment_invalid(
            tool_name, price_u,
            "X402_GATEWAY_URL not configured on server",
        )

    try:
        verified = _verify_with_gateway(gateway_url, token, tool_name, price_u)
    except Exception as e:
        return False, _payment_invalid(tool_name, price_u, f"gateway error: {e}")
    if not verified:
        return False, _payment_invalid(tool_name, price_u, "gateway rejected payment")
    return True, None


def _verify_with_gateway(gateway_url: str, token: str, tool: str, price_u: float) -> bool:
    """POST to ``<gateway>/v1/<tool>`` with the bearer.

    create-mcpay's contract: the Worker validates the key, atomically charges
    the per-endpoint price (configured Worker-side), and returns 200 on success
    or 401/402 on auth/insufficient-funds. We don't care about the response
    body — just the status code.

    The Worker is the canonical price source. We send the tool name and our
    suggested price as a hint; the Worker may charge differently based on its
    own ``PRICE_MCENTS`` table. Operators should keep both in sync.
    """
    timeout = float(os.environ.get("X402_TIMEOUT_S", "5") or 5)
    body = json.dumps({
        "tool": tool,
        "expected_price_mcents": usdc_to_mcents(price_u),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{gateway_url}/v1/{tool}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # 401 unauthorized, 402 insufficient, 403 scope mismatch — all reject
        return False


# ---------------------------------------------------------------------------
# Error payload helpers.
# ---------------------------------------------------------------------------

def _payment_required(tool: str, price_u: float) -> dict:
    return {
        "error": "402 Payment Required",
        "tool": tool,
        "price_usdc": price_u,
        "price_mcents": usdc_to_mcents(price_u),
        "chain": "base",
        "currency": "USDC",
        "recipient": os.environ.get("X402_RECIPIENT_ADDRESS"),
        "hint": (
            "Pass a paid bearer token as `_payment_token` in the tool args. "
            "Mint a key against the operator's create-mcpay gateway "
            "(see X402_GATEWAY_URL) and include it in every paid call."
        ),
        "free_tools": sorted(FREE_TOOLS),
    }


def _payment_invalid(tool: str, price_u: float, reason: str) -> dict:
    return {
        "error": "402 Payment Required",
        "tool": tool,
        "price_usdc": price_u,
        "price_mcents": usdc_to_mcents(price_u),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Status helpers (for ``mcp_server.py`` to log on startup).
# ---------------------------------------------------------------------------

def status_summary() -> str:
    mode = _mode()
    if mode == "disabled":
        return "x402: disabled (free mode)"
    if mode == "mock":
        return "x402: mock mode (accepts mcp_test_* tokens)"
    if mode == "gateway":
        url = os.environ.get("X402_GATEWAY_URL") or "<unset>"
        recip = os.environ.get("X402_RECIPIENT_ADDRESS") or "<unset>"
        return f"x402: gateway mode (url={url}, recipient={recip})"
    return f"x402: unknown mode ({mode})"

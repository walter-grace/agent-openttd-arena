"""Smoke tests for the x402 payment gate.

Run: python3 -m agent.sandbox.test_x402_gate
or:  python3 agent/sandbox/test_x402_gate.py

Stdlib only. No network — gateway mode is exercised against a fake stub
HTTP server on 127.0.0.1.
"""
from __future__ import annotations

import http.server
import importlib
import json
import os
import sys
import threading
from pathlib import Path

# Make `agent.sandbox` importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _reload_gate():
    # Re-import so env-var changes (X402_MODE, prices) take effect.
    import agent.sandbox.x402_gate as g
    return importlib.reload(g)


# ---------------------------------------------------------------------------
# Disabled mode (default).
# ---------------------------------------------------------------------------

def test_disabled_mode_lets_paid_tool_through():
    for k in ("X402_MODE", "X402_ENABLED", "X402_GATEWAY_URL"):
        os.environ.pop(k, None)
    g = _reload_gate()
    ok, err = g.check_payment("dispatch_route", {})
    assert ok and err is None, f"disabled mode should pass paid tool: {err}"
    ok, err = g.check_payment("game_state", {})
    assert ok and err is None
    print("PASS: disabled mode lets paid tool through")


# ---------------------------------------------------------------------------
# Mock mode.
# ---------------------------------------------------------------------------

def test_mock_mode_rejects_no_token():
    os.environ["X402_MODE"] = "mock"
    g = _reload_gate()
    ok, err = g.check_payment("dispatch_route", {})
    assert not ok and err and err["error"] == "402 Payment Required"
    assert err["price_usdc"] == 0.05
    assert err["price_mcents"] == 5000  # 0.05 USDC = 5000 mcents
    print("PASS: mock mode rejects missing token with 402")


def test_mock_mode_accepts_test_token():
    os.environ["X402_MODE"] = "mock"
    g = _reload_gate()
    ok, err = g.check_payment("dispatch_route", {"_payment_token": "mcp_test_abc"})
    assert ok and err is None
    print("PASS: mock mode accepts mcp_test_* tokens")


def test_mock_mode_rejects_garbage_token():
    os.environ["X402_MODE"] = "mock"
    g = _reload_gate()
    ok, err = g.check_payment("dispatch_route", {"_payment_token": "lol"})
    assert not ok and err
    print("PASS: mock mode rejects non-test tokens")


def test_mock_mode_lets_free_tools_through():
    os.environ["X402_MODE"] = "mock"
    g = _reload_gate()
    for name in ("game_state", "list_companies", "list_towns",
                 "list_vehicles", "list_stations"):
        ok, err = g.check_payment(name, {})
        assert ok and err is None, f"free tool {name} should pass"
    print("PASS: mock mode lets all free tools through")


# ---------------------------------------------------------------------------
# Gateway mode against a stub HTTP server.
# ---------------------------------------------------------------------------

class _StubGatewayHandler(http.server.BaseHTTPRequestHandler):
    """Mimics create-mcpay: 200 if Authorization is `Bearer mcp_good_*`,
    402 if `mcp_broke_*`, 401 otherwise."""

    def log_message(self, *a, **k):  # silence
        pass

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        body_len = int(self.headers.get("Content-Length", "0") or 0)
        _ = self.rfile.read(body_len)  # drain
        if token.startswith("mcp_good_"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if token.startswith("mcp_broke_"):
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"insufficient"}')
            return
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')


def _spawn_stub() -> tuple[http.server.HTTPServer, str]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _StubGatewayHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def test_gateway_mode_accepts_funded_token():
    srv, url = _spawn_stub()
    try:
        os.environ["X402_MODE"] = "gateway"
        os.environ["X402_GATEWAY_URL"] = url
        g = _reload_gate()
        ok, err = g.check_payment("dispatch_route", {"_payment_token": "mcp_good_xyz"})
        assert ok and err is None, f"funded token should pass: {err}"
        print("PASS: gateway mode accepts a funded token")
    finally:
        srv.shutdown()


def test_gateway_mode_rejects_broke_token():
    srv, url = _spawn_stub()
    try:
        os.environ["X402_MODE"] = "gateway"
        os.environ["X402_GATEWAY_URL"] = url
        g = _reload_gate()
        ok, err = g.check_payment("send_chat", {"_payment_token": "mcp_broke_xyz"})
        assert not ok and err and err["error"] == "402 Payment Required"
        print("PASS: gateway mode rejects a broke token (402)")
    finally:
        srv.shutdown()


def test_gateway_mode_rejects_unauth_token():
    srv, url = _spawn_stub()
    try:
        os.environ["X402_MODE"] = "gateway"
        os.environ["X402_GATEWAY_URL"] = url
        g = _reload_gate()
        ok, err = g.check_payment("rcon", {"_payment_token": "deadbeef"})
        assert not ok and err
        print("PASS: gateway mode rejects an unauthorized token (401)")
    finally:
        srv.shutdown()


def test_gateway_mode_missing_url():
    os.environ["X402_MODE"] = "gateway"
    os.environ.pop("X402_GATEWAY_URL", None)
    g = _reload_gate()
    ok, err = g.check_payment("rcon", {"_payment_token": "mcp_anything"})
    assert not ok and err
    assert "X402_GATEWAY_URL" in err.get("reason", "")
    print("PASS: gateway mode 402s when X402_GATEWAY_URL is unset")


# ---------------------------------------------------------------------------
# Legacy alias: X402_ENABLED=true.
# ---------------------------------------------------------------------------

def test_legacy_enabled_true_means_gateway():
    os.environ.pop("X402_MODE", None)
    os.environ["X402_ENABLED"] = "true"
    os.environ.pop("X402_GATEWAY_URL", None)
    g = _reload_gate()
    # gateway mode without URL should reject paid tools
    ok, err = g.check_payment("pause", {})
    assert not ok and err and err["error"] == "402 Payment Required"
    print("PASS: legacy X402_ENABLED=true acts as gateway mode")


# ---------------------------------------------------------------------------
# Pricing override.
# ---------------------------------------------------------------------------

def test_price_override_via_env():
    os.environ.pop("X402_MODE", None)
    os.environ["X402_PRICES_OVERRIDE_JSON"] = json.dumps({"send_chat": 0.005})
    g = _reload_gate()
    assert g.price_usdc("send_chat") == 0.005
    assert g.price_usdc("dispatch_route") == 0.05  # unchanged
    os.environ.pop("X402_PRICES_OVERRIDE_JSON", None)
    print("PASS: price override JSON env var works")


def test_chain_currency_config_for_robinhood():
    # Default is Base/USDC for back-compat.
    for k in ("X402_CHAIN", "X402_CURRENCY", "X402_MODE"):
        os.environ.pop(k, None)
    g = _reload_gate()
    req = g._payment_required("dispatch_route", 0.05)
    assert req["chain"] == "base" and req["currency"] == "USDC", req
    # Configured for Robinhood Chain + USDG.
    os.environ["X402_CHAIN"] = "robinhood-chain"
    os.environ["X402_CURRENCY"] = "USDG"
    g = _reload_gate()
    req = g._payment_required("rcon", 0.10)
    assert req["chain"] == "robinhood-chain" and req["currency"] == "USDG", req
    assert "robinhood-chain" in g.status_summary()
    os.environ.pop("X402_CHAIN", None)
    os.environ.pop("X402_CURRENCY", None)
    print("PASS: chain/currency configurable for Robinhood Chain (USDG/HERO)")


# ---------------------------------------------------------------------------
# mcp_server integration: tools/list shows price hints in gateway mode.
# ---------------------------------------------------------------------------

def test_mcp_server_annotates_paid_tools_in_list():
    os.environ["X402_MODE"] = "mock"
    _reload_gate()
    # Reload mcp_server too so it picks up the gate state.
    import agent.sandbox.mcp_server as m
    m = importlib.reload(m)
    annotated = m._annotated_tools()
    by_name = {t["name"]: t for t in annotated}
    # paid tool gets a price tag + _payment_token in schema
    dispatch = by_name["dispatch_route"]
    assert "x402" in dispatch["description"].lower(), dispatch["description"]
    assert "_payment_token" in dispatch["inputSchema"]["properties"]
    # free tool is untouched
    free = by_name["game_state"]
    assert "x402" not in free["description"].lower()
    assert "_payment_token" not in (free["inputSchema"].get("properties") or {})
    print("PASS: tools/list annotates paid tools with x402 hints")


def test_mcp_server_call_tool_blocks_paid_without_token():
    """Verify the dispatcher rejects a paid tool with no token, BEFORE
    touching the OpenTTD admin client."""
    os.environ["X402_MODE"] = "mock"
    _reload_gate()
    import agent.sandbox.mcp_server as m
    m = importlib.reload(m)

    # Stub get_client so we never actually connect to OpenTTD.
    sentinel = {"called": False}

    def _stub_client():
        sentinel["called"] = True
        raise AssertionError("get_client should NOT be called when payment fails")

    m.get_client = _stub_client  # type: ignore
    res = m.call_tool("dispatch_route", {"from_town": "A", "to_town": "B"})
    text = res["content"][0]["text"]
    assert "402" in text, text
    assert not sentinel["called"], "client was called despite payment failure"
    print("PASS: call_tool blocks paid tool without token before touching admin client")


def test_mcp_server_call_tool_strips_payment_token():
    """The _payment_token must not leak into the tool implementation."""
    os.environ["X402_MODE"] = "mock"
    _reload_gate()
    import agent.sandbox.mcp_server as m
    m = importlib.reload(m)

    seen_args: dict = {}

    class _StubClient:
        def rcon(self, cmd):
            seen_args["cmd"] = cmd

    m.get_client = lambda: _StubClient()  # type: ignore
    m.call_tool("send_chat", {"message": "hi", "_payment_token": "mcp_test_x"})
    assert "_payment_token" not in seen_args.get("cmd", "")
    assert "hi" in seen_args.get("cmd", "")
    print("PASS: _payment_token is stripped before tool body runs")


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_disabled_mode_lets_paid_tool_through,
        test_mock_mode_rejects_no_token,
        test_mock_mode_accepts_test_token,
        test_mock_mode_rejects_garbage_token,
        test_mock_mode_lets_free_tools_through,
        test_gateway_mode_accepts_funded_token,
        test_gateway_mode_rejects_broke_token,
        test_gateway_mode_rejects_unauth_token,
        test_gateway_mode_missing_url,
        test_legacy_enabled_true_means_gateway,
        test_price_override_via_env,
        test_chain_currency_config_for_robinhood,
        test_mcp_server_annotates_paid_tools_in_list,
        test_mcp_server_call_tool_blocks_paid_without_token,
        test_mcp_server_call_tool_strips_payment_token,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

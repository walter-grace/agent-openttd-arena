"""Smoke tests for the self-modifying AI MCP tools.

Run: python3 agent/sandbox/test_squirrel_ai_edit.py

Stdlib only. Never touches the real OpenTTD admin port or the real
~/Documents/OpenTTD AI install — we monkey-patch get_client() and redirect
the .nut path via NUTZ_AI_MAIN_NUT to a tmpdir.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

# Make `agent.sandbox` importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# A minimal but valid-looking Squirrel source the validator should accept.
GOOD_SOURCE = """\
class DlfExecutorAI extends AIController {
    _PathfinderRoad = import("pathfinder.road", "Road", 3);
    _AyStar         = import("graph.aystar", "AyStar", 4);
    function Start() {
        AILog.Info("hello from updated AI");
        while (true) { this.Sleep(50); }
    }
}
"""


def _reload_server(env: dict) -> "object":
    """Set env, reload x402_gate (which mcp_server uses) + mcp_server."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import agent.sandbox.x402_gate as g
    importlib.reload(g)
    import agent.sandbox.mcp_server as m
    return importlib.reload(m)


class _StubClient:
    """Pretends to be OpenTTDAdminClient. Captures rcon calls so tests can
    assert reload commands were issued. Companies dict drives liveness check."""
    def __init__(self, alive_after_reload: bool = True):
        self.rcon_calls: list[str] = []
        # Pre-populate one running AI so stop_ai gets fired.
        self.companies = {
            0: {"id": 0, "is_ai": True, "name": "Nutz Executor", "manager": "Nutz Executor"},
        }
        self._alive_after_reload = alive_after_reload

    def rcon(self, cmd: str) -> None:
        self.rcon_calls.append(cmd)
        # Simulate the AI dying after start_ai if the test asks for it.
        if cmd.startswith("start_ai") and not self._alive_after_reload:
            self.companies = {}


def _stub_no_sleep(m):
    """Patch time.sleep inside the module to a no-op so tests are fast."""
    import agent.sandbox.mcp_server as mod
    mod.time.sleep = lambda *_a, **_k: None  # type: ignore


# ---------------------------------------------------------------------------
# Validation: pure unit tests on _validate_squirrel_source.
# ---------------------------------------------------------------------------

def test_validator_accepts_good_source():
    m = _reload_server({"MCP_ALLOW_AI_EDIT": None})
    ok, why = m._validate_squirrel_source(GOOD_SOURCE)
    assert ok, f"good source should validate: {why}"
    print("PASS: validator accepts good source")


def test_validator_rejects_oversize():
    m = _reload_server({})
    big = "// pad\n" * 50_000  # ~350KB
    ok, why = m._validate_squirrel_source(big + GOOD_SOURCE)
    assert not ok and "cap" in why
    print("PASS: validator rejects > 200KB")


def test_validator_rejects_no_aicontroller():
    m = _reload_server({})
    ok, why = m._validate_squirrel_source("class Foo {}\n")
    assert not ok and "AIController" in why
    print("PASS: validator rejects source without AIController")


def test_validator_rejects_disallowed_import():
    m = _reload_server({})
    bad = GOOD_SOURCE + '\nlocal x = import("io.file", "F", 1);\n'
    ok, why = m._validate_squirrel_source(bad)
    assert not ok and "io.file" in why, why
    print("PASS: validator rejects disallowed import")


def test_validator_rejects_forbidden_substrings():
    m = _reload_server({})
    for needle in ("system(\"rm -rf\")", "io.open(\"x\")", "exec(\"x\")"):
        bad = GOOD_SOURCE + f"\n// danger: {needle}\n"
        ok, why = m._validate_squirrel_source(bad)
        assert not ok, f"should reject {needle}"
    print("PASS: validator rejects forbidden substrings")


def test_validator_rejects_unbalanced_braces():
    m = _reload_server({})
    bad = "class X extends AIController { function f() { } "  # missing }
    ok, why = m._validate_squirrel_source(bad)
    assert not ok and "brace" in why
    print("PASS: validator rejects unbalanced braces")


# ---------------------------------------------------------------------------
# Env flag: tool refuses without MCP_ALLOW_AI_EDIT.
# ---------------------------------------------------------------------------

def test_update_refused_without_env_flag():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "main.nut"
        target.write_text("class X extends AIController {}\n", encoding="utf-8")
        m = _reload_server({
            "MCP_ALLOW_AI_EDIT": None,  # NOT set
            "NUTZ_AI_MAIN_NUT": str(target),
        })
        m.get_client = lambda: _StubClient()  # type: ignore
        res = m.call_tool("update_squirrel_ai", {"source": GOOD_SOURCE})
        body = json.loads(res["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == 403
        # File must NOT have been overwritten.
        assert "AIController" in target.read_text()
        assert "hello from updated AI" not in target.read_text()
    print("PASS: update_squirrel_ai refuses without MCP_ALLOW_AI_EDIT")


def test_update_refused_invalid_source_with_flag():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "main.nut"
        original = "class X extends AIController { function Start() {} }\n"
        target.write_text(original, encoding="utf-8")
        m = _reload_server({
            "MCP_ALLOW_AI_EDIT": "true",
            "NUTZ_AI_MAIN_NUT": str(target),
        })
        m.get_client = lambda: _StubClient()  # type: ignore
        res = m.call_tool("update_squirrel_ai", {"source": "totally bogus"})
        body = json.loads(res["content"][0]["text"])
        assert body["ok"] is False
        assert body["status"] == 400
        # Original preserved.
        assert target.read_text() == original
    print("PASS: update_squirrel_ai refuses invalid source even with flag set")


# ---------------------------------------------------------------------------
# Happy path: write + reload + return.
# ---------------------------------------------------------------------------

def test_update_writes_and_reloads():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "main.nut"
        target.write_text("class Old extends AIController {}\n", encoding="utf-8")
        m = _reload_server({
            "MCP_ALLOW_AI_EDIT": "1",
            "NUTZ_AI_MAIN_NUT": str(target),
        })
        client = _StubClient(alive_after_reload=True)
        m.get_client = lambda: client  # type: ignore
        _stub_no_sleep(m)
        res = m.call_tool("update_squirrel_ai", {
            "source": GOOD_SOURCE,
            "reason": "test",
        })
        body = json.loads(res["content"][0]["text"])
        assert body["ok"] is True, body
        assert "hello from updated AI" in target.read_text()
        # rcon sequence: stop_ai 1, rescan_ai, start_ai "Nutz Executor"
        cmds = client.rcon_calls
        assert any(c.startswith("stop_ai") for c in cmds), cmds
        assert "rescan_ai" in cmds
        assert any(c == 'start_ai "Nutz Executor"' for c in cmds), cmds
        # A backup was created.
        backup = body.get("backup")
        assert backup and Path(backup).exists()
        assert "Old" in Path(backup).read_text()
    print("PASS: update_squirrel_ai writes, backs up, reloads via rcon")


def test_update_rolls_back_on_dead_ai():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "main.nut"
        original = "class Old extends AIController {}\n"
        target.write_text(original, encoding="utf-8")
        m = _reload_server({
            "MCP_ALLOW_AI_EDIT": "true",
            "NUTZ_AI_MAIN_NUT": str(target),
        })
        client = _StubClient(alive_after_reload=False)
        m.get_client = lambda: client  # type: ignore
        _stub_no_sleep(m)
        res = m.call_tool("update_squirrel_ai", {"source": GOOD_SOURCE})
        body = json.loads(res["content"][0]["text"])
        assert body["ok"] is False
        assert "rolled back" in body["error"].lower()
        # File restored to original content.
        assert target.read_text() == original
    print("PASS: update_squirrel_ai auto-rolls-back when AI dies after reload")


def test_read_squirrel_ai_returns_source():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "main.nut"
        target.write_text(GOOD_SOURCE, encoding="utf-8")
        m = _reload_server({"NUTZ_AI_MAIN_NUT": str(target)})
        m.get_client = lambda: _StubClient()  # type: ignore
        res = m.call_tool("read_squirrel_ai", {})
        body = json.loads(res["content"][0]["text"])
        assert body["ok"] is True
        assert body["source"] == GOOD_SOURCE
        assert body["bytes"] == len(GOOD_SOURCE.encode("utf-8"))
    print("PASS: read_squirrel_ai returns current source")


def test_update_tool_listed_in_tools_list():
    m = _reload_server({})
    names = [t["name"] for t in m.TOOLS]
    assert "update_squirrel_ai" in names
    assert "read_squirrel_ai" in names
    assert "propose_squirrel_diff" in names
    print("PASS: new tools are in TOOLS list")


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_validator_accepts_good_source,
        test_validator_rejects_oversize,
        test_validator_rejects_no_aicontroller,
        test_validator_rejects_disallowed_import,
        test_validator_rejects_forbidden_substrings,
        test_validator_rejects_unbalanced_braces,
        test_update_refused_without_env_flag,
        test_update_refused_invalid_source_with_flag,
        test_update_writes_and_reloads,
        test_update_rolls_back_on_dead_ai,
        test_read_squirrel_ai_returns_source,
        test_update_tool_listed_in_tools_list,
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

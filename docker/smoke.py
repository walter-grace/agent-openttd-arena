"""Health check for a running arena container.

Run it inside the container, where the admin port lives:

    docker exec arena python3 /app/docker/smoke.py

Exits 0 when the arena is genuinely playable, non-zero with a specific reason
otherwise. "The container is up" is not the same claim: the process can be
running while the world failed to generate, the GameScript failed to load, or
the AI never took a company. Each of those is a different fix, so each gets
its own message.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/app/agent")

from admin_client import OpenTTDAdminClient  # noqa: E402

ADMIN_PORT = 3977
ADMIN_PW = "nutzarena"
DEADLINE_S = 120


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    # 1. The admin port has to accept us at all.
    client = None
    started = time.time()
    while time.time() - started < DEADLINE_S:
        try:
            client = OpenTTDAdminClient(password=ADMIN_PW, port=ADMIN_PORT)
            client.connect()
            break
        except Exception:
            time.sleep(2)
    if client is None:
        fail(f"admin port {ADMIN_PORT} never accepted a connection "
             f"within {DEADLINE_S}s")
    print("ok: admin port accepted the connection")

    # 2. The bridge GameScript has to be pushing state. No state means the GS
    #    did not load, which is invisible from the outside: the game port still
    #    answers and the container still looks healthy.
    state = None
    while time.time() - started < DEADLINE_S:
        state = client.get_gs_state()
        if state:
            break
        time.sleep(2)
    if not state:
        fail("bridge GameScript never pushed state to the admin port "
             "(the GS did not load)")
    print("ok: bridge GameScript is pushing state")

    # 3. A world has to exist.
    size = (state.get("map") or {})
    if not size.get("sizeX") or not size.get("sizeY"):
        fail(f"no world generated; map block was {size!r}")
    print(f"ok: world generated, {size['sizeX']}x{size['sizeY']}")

    # 4. The AI has to hold a company. The config slot alone does not spawn it,
    #    so this is what proves the entrypoint's start_ai step worked.
    company = None
    while time.time() - started < DEADLINE_S:
        state = client.get_gs_state() or {}
        for co in state.get("companies") or []:
            if "nutz" in str(co.get("name", "")).lower():
                company = co
                break
        if company:
            break
        time.sleep(2)
    if not company:
        names = [c.get("name") for c in (state.get("companies") or [])]
        fail(f"the Nutz Executor AI never took a company (companies: {names})")
    print(f"ok: AI company present, {company.get('name')!r}")

    # 5. The clock has to advance, or the world is loaded but not simulating.
    first = (client.get_gs_state() or {}).get("date")
    time.sleep(10)
    second = (client.get_gs_state() or {}).get("date")
    if first is None or second is None:
        fail("no date in pushed state; cannot tell whether the game is running")
    if second == first:
        fail(f"the game clock is not advancing (date stuck at {first}); "
             "the server is paused or stalled")
    print(f"ok: clock advancing, {first} -> {second}")

    client.close()
    print("\nARENA OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

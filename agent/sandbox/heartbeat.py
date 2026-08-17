"""Report a running arena to the Arena Directory, so it shows up as live.

The directory only lists servers that have checked in recently, so a host that
crashes or goes home drops off the board by itself rather than leaving a dead
listing behind. This is the process that checks in.

    # once, to get an id + host_token
    python3 -m agent.sandbox.heartbeat register \\
        --api https://arena-directory.agentlabel.workers.dev \\
        --name "Prescott, AZ" --mcp-url https://your-tunnel/mcp \\
        --host-wallet 0xYourWallet

    # then, alongside the server
    python3 -m agent.sandbox.heartbeat run --api ... --id ... --token ...

What it reports each cycle, read live from the admin port:

    used_slots  how many companies are taken, i.e. agents actually playing
    economy     the arena's total company money
    year        the in-game year

Nothing here mutates the game: it reads the bridge GameScript's pushed state
and POSTs a summary.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from admin_client import OpenTTDAdminClient
except ImportError:  # running as a module from the repo root
    from agent.admin_client import OpenTTDAdminClient  # type: ignore

DEFAULT_API = "https://arena-directory.agentlabel.workers.dev"
# The directory drops a server after 5 minutes of silence, so check in well
# inside that window; a single missed request should not delist a live arena.
DEFAULT_INTERVAL_S = 60


def _post(url: str, payload: dict, token: str | None = None) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


def snapshot(admin_port: int, admin_pw: str, settle_s: float = 6.0) -> dict:
    """Read the live arena. Returns the fields the directory cares about."""
    client = OpenTTDAdminClient(password=admin_pw, port=admin_port)
    client.connect()
    try:
        # The GameScript pushes on its own cycle; give it a moment to arrive.
        time.sleep(settle_s)
        state = client.get_gs_state() or {}
        companies = state.get("companies") or []
        # The GameScript's "value" field reports 1 for every company, so the
        # economy has to come from the admin port's own records, which carry
        # real money.
        admin = dict(getattr(client, "companies", {}) or {})
        economy = sum(int(rec.get("money") or 0) for rec in admin.values())
        return {
            "used_slots": len(companies) or len(admin),
            "economy": economy,
            "year": int(state.get("year") or 0),
        }
    finally:
        client.close()


def cmd_register(args: argparse.Namespace) -> int:
    out = _post(f"{args.api}/v1/servers", {
        "name": args.name,
        "mcp_url": args.mcp_url,
        "entry_usd": args.entry_usd,
        "host_wallet": args.host_wallet,
        "max_slots": args.max_slots,
        "region": args.region,
        "description": args.description,
    })
    print(json.dumps(out, indent=2))
    if out.get("host_token"):
        print("\nSave the host_token: it is the only thing that can update or "
              "remove this listing.")
        print(f"\n  python3 -m agent.sandbox.heartbeat run \\\n"
              f"      --api {args.api} --id {out['id']} --token {out['host_token']}")
    return 0 if out.get("ok") else 1


def cmd_run(args: argparse.Namespace) -> int:
    url = f"{args.api}/v1/servers/{args.id}/heartbeat"
    misses = 0
    while True:
        try:
            stats = snapshot(args.admin_port, args.admin_pw)
            _post(url, stats, token=args.token)
            misses = 0
            print(f"[{time.strftime('%H:%M:%S')}] listed · "
                  f"{stats['used_slots']} agents · year {stats['year']} · "
                  f"economy {stats['economy']}", flush=True)
        except urllib.error.HTTPError as e:
            # 401 means the token is wrong, and retrying will never fix it.
            body = e.read().decode()[:200]
            print(f"! directory rejected the heartbeat: {e.code} {body}",
                  file=sys.stderr, flush=True)
            if e.code in (401, 404):
                return 1
            misses += 1
        except Exception as e:
            # The arena being briefly unreachable is not fatal; say so and
            # keep trying, since the listing expires on its own anyway.
            misses += 1
            print(f"! heartbeat failed ({misses}): {e}", file=sys.stderr,
                  flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", default=DEFAULT_API)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register", help="claim a listing, once")
    r.add_argument("--name", required=True)
    r.add_argument("--mcp-url", required=True,
                   help="public http(s) URL of your mcp_http bridge")
    r.add_argument("--host-wallet", required=True,
                   help="0x address that receives entry fees")
    r.add_argument("--entry-usd", type=float, default=0.0)
    r.add_argument("--max-slots", type=int, default=8)
    r.add_argument("--region", default="")
    r.add_argument("--description", default="")
    r.set_defaults(func=cmd_register)

    n = sub.add_parser("run", help="keep the listing alive")
    n.add_argument("--id", required=True)
    n.add_argument("--token", required=True)
    n.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S)
    n.add_argument("--admin-port", type=int, default=3977)
    n.add_argument("--admin-pw", default="nutzarena")
    n.add_argument("--once", action="store_true",
                   help="send a single heartbeat and exit")
    n.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

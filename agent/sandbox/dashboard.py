#!/usr/bin/env python3
"""Arena dashboard — render live OpenTTD game state as a web page.

One surface for two audiences:
  • people open http://localhost:8080 in any browser
  • agents (Kitesurf, etc.) read the same page, or GET /state.json directly

No native OpenTTD client, no OpenGFX, no GUI. It connects to the running
server's admin port, reads the bridge GameScript's pushed state (towns,
industries, map) plus company economies, and serves both a JSON endpoint and
a self-contained HTML dashboard that polls it.

    python3 -m agent.sandbox.dashboard --admin-port 3977 --port 8080

Run it next to setup_arena.sh's server. Read-only: it never mutates the game.
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "agent"))
try:
    from admin_client import OpenTTDAdminClient
except ImportError:  # when run as a module from repo root
    from agent.admin_client import OpenTTDAdminClient  # type: ignore


class StatePoller:
    """Holds one admin connection and keeps a merged state snapshot fresh."""

    def __init__(self, host, port, password):
        self.client = OpenTTDAdminClient(host=host, port=port, password=password,
                                         name="ArenaDashboard")
        self._snapshot = {"connected": False, "towns": [], "companies": [],
                          "stations": [], "map": {"sizeX": 256, "sizeY": 256}}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        self.client.connect()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.client._poll(2)   # COMPANY_INFO
                self.client._poll(3)   # COMPANY_ECONOMY
                time.sleep(2)
                gs = self.client.get_gs_state() or {}
                admin_co = dict(getattr(self.client, "companies", {}) or {})
                snap = self._merge(gs, admin_co)
                with self._lock:
                    self._snapshot = snap
            except Exception as e:  # keep serving the last good snapshot
                with self._lock:
                    self._snapshot = {**self._snapshot, "error": str(e)}
            self._stop.wait(3)

    @staticmethod
    def _merge(gs, admin_co):
        # GS companies carry perf/value/income; admin carries money/vehicles.
        by_id = {c.get("id"): dict(c) for c in gs.get("companies", [])}
        for cid, a in admin_co.items():
            row = by_id.setdefault(cid, {"id": cid})
            row.setdefault("name", a.get("name"))
            row.setdefault("manager", a.get("manager"))
            row["money"] = a.get("money")
            row["loan"] = a.get("loan")
            row["vehicles"] = a.get("vehicles")
            row["stations"] = a.get("stations")
            row["is_ai"] = a.get("is_ai")
            if a.get("income") is not None:
                row.setdefault("income", a.get("income"))
        return {
            "connected": True,
            "scenario": gs.get("scenario"),
            "date": gs.get("date"),
            "year": gs.get("year"),
            "month": gs.get("month"),
            "industries_count": gs.get("industries_count"),
            "vehicles": gs.get("vehicles"),
            "map": gs.get("map") or {"sizeX": 256, "sizeY": 256},
            "towns": gs.get("towns_top", []),
            "stations": gs.get("stations", []),
            "companies": sorted(by_id.values(),
                                key=lambda c: (c.get("money") or 0), reverse=True),
            "ts": int(time.time()),
        }

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Arena — live game state</title>
<style>
:root{--bg:#0b0f0d;--panel:#121a16;--line:#1f2b24;--ink:#e8f3ec;--dim:#7f9689;--hero:#00c805}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:baseline;padding:16px 20px;
border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0;letter-spacing:.5px}
.stat{color:var(--dim)}.stat b{color:var(--ink)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
.wrap{display:grid;grid-template-columns:minmax(280px,1fr) minmax(280px,1.2fr);
gap:16px;padding:16px 20px;max-width:1200px}
@media(max-width:760px){.wrap{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);
margin:0 0 10px}
canvas{width:100%;height:auto;background:#0d1512;border-radius:6px;display:block}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:4px 8px;
border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--dim);font-weight:400}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.ai{color:var(--hero)}.foot{color:var(--dim);padding:0 20px 24px;font-size:12px}
.off{color:#e2555a}
</style></head><body>
<header>
<h1>🚆 ARENA</h1>
<span class=stat><span id=live class=dot></span><span id=status>connecting…</span></span>
<span class=stat>scenario <b id=scenario>—</b></span>
<span class=stat>date <b id=date>—</b></span>
<span class=stat>towns <b id=ntowns>—</b></span>
<span class=stat>industries <b id=industries>—</b></span>
<span class=stat>companies <b id=ncos>—</b></span>
</header>
<div class=wrap>
<div class=card><h2>Map</h2><canvas id=map width=256 height=256></canvas></div>
<div>
<div class=card style=margin-bottom:16px><h2>Companies</h2>
<table><thead><tr><th>Manager</th><th class=n>Money</th><th class=n>Income</th>
<th class=n>Vehicles</th><th class=n>Perf</th></tr></thead><tbody id=cos></tbody></table></div>
<div class=card><h2>Towns (top by population)</h2>
<table><thead><tr><th>Town</th><th class=n>Pop</th><th class=n>Houses</th>
<th class=n>Pax/mo</th><th>Growing</th></tr></thead><tbody id=towns></tbody></table></div>
</div>
</div>
<div class=foot>Read-only view of the running arena. Agents can fetch
<a href=/state.json style=color:var(--hero)>/state.json</a> for the same data.</div>
<script>
const $=id=>document.getElementById(id);
const money=v=>v==null?'—':'£'+Math.round(v).toLocaleString();
function draw(s){
  const cv=$('map'),ctx=cv.getContext('2d');
  const W=(s.map&&s.map.sizeX)||256,H=(s.map&&s.map.sizeY)||256;
  cv.width=W;cv.height=H;ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#0d1512';ctx.fillRect(0,0,W,H);
  for(const st of (s.stations||[])){const t=st.tile|0;ctx.fillStyle='#3a6';
    ctx.fillRect(t%W-1,(t/W|0)-1,3,3);}
  for(const tn of (s.towns||[])){const t=tn.tile|0,x=t%W,y=t/W|0;
    const r=Math.max(1.5,Math.min(7,Math.sqrt((tn.pop||0)/40)));
    ctx.beginPath();ctx.arc(x,y,r,0,7);ctx.fillStyle='rgba(0,200,5,.85)';ctx.fill();}
}
function rows(s){
  $('cos').innerHTML=(s.companies||[]).map(c=>`<tr>
    <td class="${c.is_ai?'ai':''}">${c.is_ai?'🤖 ':''}${c.manager||c.name||('#'+c.id)}</td>
    <td class=n>${money(c.money)}</td><td class=n>${c.income==null?'—':money(c.income)}</td>
    <td class=n>${c.vehicles==null?'—':c.vehicles}</td>
    <td class=n>${c.perf==null?'—':c.perf}</td></tr>`).join('')
    ||'<tr><td colspan=5 style=color:#7f9689>no companies yet — start the AI or join</td></tr>';
  $('towns').innerHTML=(s.towns||[]).slice(0,14).map(t=>`<tr>
    <td>${t.name}</td><td class=n>${(t.pop||0).toLocaleString()}</td>
    <td class=n>${t.houses||0}</td><td class=n>${t.pass_last||0}</td>
    <td>${t.growing?'↑':'·'}</td></tr>`).join('');
}
async function tick(){
  try{
    const s=await (await fetch('/state.json',{cache:'no-store'})).json();
    const ok=s.connected;
    $('live').style.background=ok?'var(--hero)':'#e2555a';
    $('status').textContent=ok?'live':'server offline';
    $('status').className=ok?'':'off';
    $('scenario').textContent=s.scenario||'—';
    $('date').textContent=s.date?(s.year+'-'+String(s.month).padStart(2,'0')):'—';
    $('ntowns').textContent=(s.towns||[]).length;
    $('industries').textContent=s.industries_count??'—';
    $('ncos').textContent=(s.companies||[]).length;
    draw(s);rows(s);
  }catch(e){$('status').textContent='dashboard unreachable';$('live').style.background='#e2555a';}
}
tick();setInterval(tick,3000);
</script></body></html>"""


def make_handler(poller):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path in ("/state.json", "/state"):
                self._send(200, json.dumps(poller.snapshot()), "application/json")
            elif path == "/healthz":
                self._send(200, "ok", "text/plain")
            else:
                self._send(404, "not found", "text/plain")

    return H


def main():
    ap = argparse.ArgumentParser(description="Arena live-state web dashboard")
    ap.add_argument("--host", default="127.0.0.1", help="OpenTTD admin host")
    ap.add_argument("--admin-port", type=int, default=3977)
    ap.add_argument("--password", default=os.environ.get("ADMIN_PW", "nutzarena"))
    ap.add_argument("--port", type=int, default=8080, help="dashboard HTTP port")
    args = ap.parse_args()

    poller = StatePoller(args.host, args.admin_port, args.password)
    try:
        poller.start()
    except Exception as e:
        print(f"! could not connect to admin port {args.admin_port}: {e}", file=sys.stderr)
        print("  is the arena server running? (./agent/setup_arena.sh)", file=sys.stderr)
        sys.exit(1)

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(poller))
    print(f"▸ Arena dashboard → http://localhost:{args.port}  "
          f"(state: http://localhost:{args.port}/state.json)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Minimal OpenTTD admin-socket client (stdlib only).

Implements just the subset of the admin protocol we need:
  - join (auth)
  - ping
  - subscribe to periodic updates (COMPANY_INFO, COMPANY_ECONOMY, DATE, CLIENT)
  - issue `rcon` commands
  - receive async packets from the server

Protocol reference:
  https://wiki.openttd.org/en/Development/Server%20admin%20port

Packet frame on the wire:
  uint16 length (little-endian, inclusive)
  uint8  type
  bytes  payload

We use a background thread to read frames into a queue; the main thread pokes
the server with commands on the same socket.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional


# ---- packet type enums (subset) --------------------------------------

ADMIN_PACKET_ADMIN_JOIN = 0
ADMIN_PACKET_ADMIN_QUIT = 1
ADMIN_PACKET_ADMIN_UPDATE_FREQUENCY = 2
ADMIN_PACKET_ADMIN_POLL = 3
ADMIN_PACKET_ADMIN_CHAT = 4
ADMIN_PACKET_ADMIN_RCON = 5
ADMIN_PACKET_ADMIN_GAMESCRIPT = 6
ADMIN_PACKET_ADMIN_PING = 7

ADMIN_PACKET_SERVER_FULL = 100
ADMIN_PACKET_SERVER_BANNED = 101
ADMIN_PACKET_SERVER_ERROR = 102
ADMIN_PACKET_SERVER_PROTOCOL = 103
ADMIN_PACKET_SERVER_WELCOME = 104
ADMIN_PACKET_SERVER_NEWGAME = 105
ADMIN_PACKET_SERVER_SHUTDOWN = 106
ADMIN_PACKET_SERVER_DATE = 107
ADMIN_PACKET_SERVER_CLIENT_JOIN = 108
ADMIN_PACKET_SERVER_CLIENT_INFO = 109
ADMIN_PACKET_SERVER_CLIENT_UPDATE = 110
ADMIN_PACKET_SERVER_CLIENT_QUIT = 111
ADMIN_PACKET_SERVER_CLIENT_ERROR = 112
ADMIN_PACKET_SERVER_COMPANY_NEW = 113
ADMIN_PACKET_SERVER_COMPANY_INFO = 114
ADMIN_PACKET_SERVER_COMPANY_UPDATE = 115
ADMIN_PACKET_SERVER_COMPANY_REMOVE = 116
ADMIN_PACKET_SERVER_COMPANY_ECONOMY = 117
ADMIN_PACKET_SERVER_COMPANY_STATS = 118
ADMIN_PACKET_SERVER_CHAT = 119
ADMIN_PACKET_SERVER_RCON = 120
ADMIN_PACKET_SERVER_CONSOLE = 121
ADMIN_PACKET_SERVER_CMD_NAMES = 122
ADMIN_PACKET_SERVER_CMD_LOGGING = 123
ADMIN_PACKET_SERVER_GAMESCRIPT = 124
ADMIN_PACKET_SERVER_RCON_END = 125
ADMIN_PACKET_SERVER_PONG = 126

# Update frequency
ADMIN_FREQUENCY_POLL = 0x01
ADMIN_FREQUENCY_DAILY = 0x02
ADMIN_FREQUENCY_WEEKLY = 0x04
ADMIN_FREQUENCY_MONTHLY = 0x08
ADMIN_FREQUENCY_QUARTERLY = 0x10
ADMIN_FREQUENCY_ANUALLY = 0x20
ADMIN_FREQUENCY_AUTOMATIC = 0x40

# Update types
ADMIN_UPDATE_DATE = 0
ADMIN_UPDATE_CLIENT_INFO = 1
ADMIN_UPDATE_COMPANY_INFO = 2
ADMIN_UPDATE_COMPANY_ECONOMY = 3
ADMIN_UPDATE_COMPANY_STATS = 4
ADMIN_UPDATE_CHAT = 5
ADMIN_UPDATE_CONSOLE = 6
ADMIN_UPDATE_CMD_NAMES = 7
ADMIN_UPDATE_CMD_LOGGING = 8
ADMIN_UPDATE_GAMESCRIPT = 9


def _pack_str(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


def _read_cstr(buf: bytes, off: int):
    end = buf.find(b"\x00", off)
    if end < 0:
        return buf[off:].decode("utf-8", errors="replace"), len(buf)
    return buf[off:end].decode("utf-8", errors="replace"), end + 1


class OpenTTDAdminClient:
    """Line-oriented admin-socket client, thread-safe reads into a queue."""

    def __init__(self, host: str = "127.0.0.1", port: int = 3977,
                 password: str = "dlfflywheel", name: str = "DLFAgent",
                 version: str = "0.1.0") -> None:
        self.host = host
        self.port = port
        self.password = password
        self.name = name
        self.version = version
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._incoming: Deque[tuple] = deque(maxlen=1024)
        self._lock = threading.Lock()
        # live state cache we build from async packets
        self.date: Dict[str, int] = {}
        self.companies: Dict[int, Dict] = {}
        self.client_info: Dict = {}

    # ---- connection -----------------------------------------------------

    def connect(self, timeout: float = 5.0) -> None:
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(None)
        self._sock = s
        self._send_join()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                try:
                    self._send(ADMIN_PACKET_ADMIN_QUIT, b"")
                except Exception:
                    pass
                self._sock.close()
        finally:
            self._sock = None

    # ---- framing --------------------------------------------------------

    def _send(self, packet_type: int, payload: bytes) -> None:
        if not self._sock:
            raise RuntimeError("not connected")
        body = bytes([packet_type]) + payload
        frame = struct.pack("<H", len(body) + 2) + body
        try:
            with self._lock:
                self._sock.sendall(frame)
        except (BrokenPipeError, OSError) as e:
            # Server dropped us — don't crash the agent loop.
            # The reader thread will also exit on next recv.
            raise ConnectionError(f"admin socket closed: {e}")

    def _read_loop(self) -> None:
        buf = b""
        assert self._sock is not None
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 3:
                    n = struct.unpack_from("<H", buf, 0)[0]
                    if len(buf) < n:
                        break
                    frame = buf[:n]
                    buf = buf[n:]
                    ptype = frame[2]
                    payload = frame[3:]
                    self._handle(ptype, payload)
        finally:
            # Connection died (server kicked us, network hiccup, etc.) -
            # null out _sock so the main loop's reconnect check fires.
            # Without this, an "alive" agent will polling stale cached state
            # forever (real symptom: agent says companies=0, date stuck).
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ---- handlers -------------------------------------------------------

    def _handle(self, ptype: int, payload: bytes) -> None:
        self._incoming.append((ptype, payload))
        if ptype == ADMIN_PACKET_SERVER_WELCOME:
            # Once authenticated, subscribe to all the updates we want.
            self._subscribe_defaults()
            self._subscribe(ADMIN_UPDATE_GAMESCRIPT, ADMIN_FREQUENCY_AUTOMATIC)
            # Console: AILog.Info / GSLog.Info from our scripts arrive here.
            # Surfaces failure context (ERR_LOCAL_AUTHORITY_REFUSES etc.) so
            # the CEO can reason about WHY a build failed, not just guess
            # from cash deltas.
            self._subscribe(ADMIN_UPDATE_CONSOLE, ADMIN_FREQUENCY_AUTOMATIC)
            # Quarterly cargo delivered + vehicle counts per company.
            # Cheap signal that the AI is actually moving things.
            self._subscribe(ADMIN_UPDATE_COMPANY_STATS, ADMIN_FREQUENCY_QUARTERLY)
            # Subscriptions only push *deltas*; POLL for initial state.
            self._poll(ADMIN_UPDATE_DATE, 0)
            self._poll(ADMIN_UPDATE_COMPANY_INFO, 0xFFFFFFFF)     # all companies
            self._poll(ADMIN_UPDATE_COMPANY_ECONOMY, 0xFFFFFFFF)
            self._poll(ADMIN_UPDATE_COMPANY_STATS, 0xFFFFFFFF)
            self._poll(ADMIN_UPDATE_CLIENT_INFO, 0xFFFFFFFF)
        elif ptype == ADMIN_PACKET_SERVER_DATE:
            # payload = uint32 date
            date = struct.unpack_from("<I", payload, 0)[0]
            self.date = {"raw": date}
        elif ptype == ADMIN_PACKET_SERVER_COMPANY_INFO:
            self._parse_company_info(payload)
        elif ptype == ADMIN_PACKET_SERVER_COMPANY_ECONOMY:
            self._parse_company_economy(payload)
        elif ptype == ADMIN_PACKET_SERVER_COMPANY_UPDATE:
            self._parse_company_update(payload)
        elif ptype == ADMIN_PACKET_SERVER_COMPANY_REMOVE:
            cid = payload[0]
            self.companies.pop(cid, None)
        elif ptype == ADMIN_PACKET_SERVER_COMPANY_STATS:
            self._parse_company_stats(payload)
        elif ptype == ADMIN_PACKET_SERVER_NEWGAME:
            # Map regenerated / loaded; cached company + GS state is stale.
            self.companies.clear()
            self._gs_latest = None
            self._gs_events = []
        elif ptype == ADMIN_PACKET_SERVER_SHUTDOWN:
            # Server closed the world; same idea.
            self.companies.clear()
            self._gs_latest = None
            self._gs_events = []
        elif ptype == ADMIN_PACKET_SERVER_GAMESCRIPT:
            # null-terminated JSON string from our GS
            js, _ = _read_cstr(payload, 0)
            self._gs_push(js)
        elif ptype == ADMIN_PACKET_SERVER_CHAT:
            self._parse_chat(payload)
        elif ptype == ADMIN_PACKET_SERVER_CONSOLE:
            self._parse_console(payload)

    # ---- GameScript channel --------------------------------------------

    _gs_latest = None  # most recent parsed state
    _gs_events = None  # ringbuffer of event dicts

    def _gs_push(self, js: str) -> None:
        import json
        try:
            obj = json.loads(js)
        except Exception:
            return
        if self._gs_events is None:
            self._gs_events = []
        kind = (obj or {}).get("kind")
        if kind == "state":
            self._gs_latest = obj
        elif kind == "event":
            self._gs_events.append(obj)
            # keep bounded
            if len(self._gs_events) > 200:
                self._gs_events = self._gs_events[-200:]

    def get_gs_state(self):
        return self._gs_latest

    def drain_gs_events(self):
        out = self._gs_events or []
        self._gs_events = []
        return out

    _console_log = None  # list[dict] of recent script log lines (AI/GS)

    def _parse_console(self, p: bytes) -> None:
        """SERVER_CONSOLE: origin(cstr) message(cstr).

        origin is e.g. "ai" or the script's short name; message is the
        log line. We keep a short tail and expose lines tagged with a
        script name (DLF Player, DLF Executor, ...) so the CEO can read
        per-AI feedback. Non-script noise (network logs, etc.) is dropped.
        """
        origin, off = _read_cstr(p, 0)
        msg, _ = _read_cstr(p, off)
        if not msg:
            return
        # OpenTTD AI log lines: origin is the script's GetName() ("DLF
        # Executor", "DLF Player", etc.) and text is the message body.
        # Keep lines whose origin OR text mentions DLF, plus any origin=="ai".
        # (Old filter required "DLF" in TEXT, dropping lines like
        # "ABORT job=99" / "stations placed" / "ROAD BUILT" - the most
        # important executor diagnostics.)
        text = msg.strip()
        if ("DLF" not in text and "DLF" not in origin
                and origin.lower() != "ai"):
            return
        if self._console_log is None:
            self._console_log = []
        self._console_log.append({
            "origin": origin,
            "text": text,
            "ts": time.time(),
        })
        if len(self._console_log) > 200:
            self._console_log = self._console_log[-200:]

    def drain_console(self):
        out = self._console_log or []
        self._console_log = []
        return out

    _chat_inbox = None  # list[dict] of recent human chat lines

    def _parse_chat(self, p: bytes) -> None:
        """SERVER_CHAT: action(u8) dest_type(u8) client_id(u32) msg(cstr) money(i64).
        We only care about action=3 (CHAT broadcast). client_id 1 = server."""
        if len(p) < 6:
            return
        action = p[0]
        # dest_type = p[1]
        client_id = struct.unpack_from("<I", p, 2)[0]
        msg, _ = _read_cstr(p, 6)
        if action != 3:
            return  # ignore joins/leaves/money-gifts
        if self._chat_inbox is None:
            self._chat_inbox = []
        self._chat_inbox.append({
            "client_id": client_id,
            "text": msg,
            "ts": time.time(),
        })
        if len(self._chat_inbox) > 200:
            self._chat_inbox = self._chat_inbox[-200:]

    def drain_chat(self):
        out = self._chat_inbox or []
        self._chat_inbox = []
        return out

    def _parse_company_info(self, p: bytes) -> None:
        # ColourID omitted for brevity — we just grab id/name/manager/start
        off = 0
        cid = p[off]; off += 1
        name, off = _read_cstr(p, off)
        manager, off = _read_cstr(p, off)
        # colour, password_protected, inaugurated_year, is_ai, bankruptcy_quarters
        colour = p[off]; off += 1
        pw = p[off]; off += 1
        year = struct.unpack_from("<I", p, off)[0]; off += 4
        is_ai = p[off]; off += 1
        c = self.companies.setdefault(cid, {})
        c.update({
            "id": cid, "name": name, "manager": manager,
            "inaugurated": year, "is_ai": bool(is_ai),
        })

    def _parse_company_economy(self, p: bytes) -> None:
        off = 0
        cid = p[off]; off += 1
        money = struct.unpack_from("<q", p, off)[0]; off += 8
        loan = struct.unpack_from("<q", p, off)[0]; off += 8
        income = struct.unpack_from("<q", p, off)[0]; off += 8
        # cargo delivered + value + past 2 quarters — simplified read
        c = self.companies.setdefault(cid, {})
        c.update({"money": money, "loan": loan, "income": income})

    def _parse_company_stats(self, p: bytes) -> None:
        """SERVER_COMPANY_STATS: per-company station + vehicle counts and
        per-cargo quarterly delivered/transported. Layout varies by
        version - read defensively. Layout (typical):
          u8 company_id
          u16 num_train, num_lorry, num_bus, num_plane, num_ship
          u16 num_train_st, num_lorry_st, num_bus_st, num_plane_st, num_ship_st
        We grab the vehicle totals (sum across types) and stash on the
        company record. Anything past the bytes we know about is ignored."""
        if len(p) < 1:
            return
        off = 0
        cid = p[off]; off += 1
        try:
            counts = struct.unpack_from("<HHHHH", p, off)
            off += 10
            station_counts = struct.unpack_from("<HHHHH", p, off)
            off += 10
        except struct.error:
            return
        c = self.companies.setdefault(cid, {})
        c["vehicles"] = sum(counts)
        c["stations"] = sum(station_counts)
        c["vehicle_breakdown"] = {
            "train": counts[0], "lorry": counts[1], "bus": counts[2],
            "plane": counts[3], "ship": counts[4],
        }

    def _parse_company_update(self, p: bytes) -> None:
        off = 0
        cid = p[off]; off += 1
        name, off = _read_cstr(p, off)
        manager, off = _read_cstr(p, off)
        c = self.companies.setdefault(cid, {})
        c.update({"name": name, "manager": manager})

    # ---- outbound -------------------------------------------------------

    def _send_join(self) -> None:
        payload = _pack_str(self.password) + _pack_str(self.name) + _pack_str(self.version)
        self._send(ADMIN_PACKET_ADMIN_JOIN, payload)

    def _subscribe(self, update_type: int, freq: int) -> None:
        payload = struct.pack("<HH", update_type, freq)
        self._send(ADMIN_PACKET_ADMIN_UPDATE_FREQUENCY, payload)

    def _poll(self, update_type: int, data: int = 0) -> None:
        """ADMIN_PACKET_ADMIN_POLL: request current snapshot of update_type."""
        payload = struct.pack("<BI", update_type & 0xFF, data & 0xFFFFFFFF)
        self._send(ADMIN_PACKET_ADMIN_POLL, payload)

    def _subscribe_defaults(self) -> None:
        self._subscribe(ADMIN_UPDATE_DATE, ADMIN_FREQUENCY_MONTHLY)
        self._subscribe(ADMIN_UPDATE_COMPANY_INFO, ADMIN_FREQUENCY_AUTOMATIC)
        self._subscribe(ADMIN_UPDATE_COMPANY_ECONOMY, ADMIN_FREQUENCY_MONTHLY)
        self._subscribe(ADMIN_UPDATE_CHAT, ADMIN_FREQUENCY_AUTOMATIC)

    def send_gs(self, json_str: str) -> None:
        """Send JSON (as a string) to the GameScript's admin-port event queue.
        Our DLF Bridge GS listens for ET_ADMIN_PORT events and parses the
        payload to place command signs that the DLF AIs poll each tick."""
        self._send(ADMIN_PACKET_ADMIN_GAMESCRIPT, _pack_str(json_str))

    def rcon(self, command: str) -> None:
        """Execute a server console command. Response comes back async as
        RCON packets (see drain_incoming for reads)."""
        self._send(ADMIN_PACKET_ADMIN_RCON, _pack_str(command))

    def chat(self, message: str, dest_type: int = 0, dest_id: int = 0) -> None:
        """Send a chat message.

        OpenTTD NetworkAction: 3 = CHAT.
        DestType: 0 = BROADCAST (all clients), 1 = TEAM, 2 = CLIENT.
        """
        payload = struct.pack("<BBI", 3, dest_type, dest_id) + _pack_str(message)
        self._send(ADMIN_PACKET_ADMIN_CHAT, payload)

    def ping(self) -> None:
        self._send(ADMIN_PACKET_ADMIN_PING, struct.pack("<I", int(time.time())))

    def drain_incoming(self):
        """Pop + return all queued (ptype, payload) tuples from the reader."""
        out = []
        while self._incoming:
            out.append(self._incoming.popleft())
        return out

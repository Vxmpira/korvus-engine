#!/usr/bin/env python3
"""
==============================================================================
 KORVUS · Tradovate Market-Data WebSocket client   (by BlackCrownVxJ.LLC)
==============================================================================
 Real CME futures quotes (MNQ/MES/etc.) over Tradovate's WebSocket protocol.

 STATUS: complete and runnable, but NOT verified live from the build env
 (no network to Tradovate there). Verify on the server with real credentials.

 TWO HARD REQUIREMENTS BEYOND CODE:
   1) PAID market-data subscription on your Tradovate account. The socket will
      connect and authorize without it, but quotes return empty / "inaccessible".
   2) REDISTRIBUTION LICENSE if you ever show these live ticks to OTHER members.
      For your own eyes this is fine. Re-serving CME data to users is a separate
      CME license — keep this personal until an attorney clears it.

 HOW IT WORKS
   Tradovate frames every WS message with a leading char:
     'o'  = socket open (sent once by server on connect)
     'h'  = heartbeat (server pings ~every 2.5s; we must echo an empty 'h')
     'a'  = data: a JSON array of message objects, e.g. a[{"s":200,"i":1,...}]
     'c'  = close
   A client request frame is plain text:  "<endpoint>\n<id>\n<query>\n<body>"
   Auth is the FIRST frame after open:    "authorize\n0\n\n<accessToken>"

 DESIGN
   Runs the socket on a background thread and keeps a {symbol: quote} cache.
   korvus_quotes.py reads that cache synchronously (non-blocking) each cycle.
   This matches the rest of the engine, which is synchronous/polling.

 DEPENDENCY:  pip install websocket-client
==============================================================================
"""
import os
import json
import time
import threading
import datetime as dt
from typing import Optional

import requests

# Load .env so the standalone smoke test (and any direct import) picks up the
# TRADOVATE_* credentials. Without this, os.getenv returns empty when run
# outside the main app and the client reports "not configured".
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import websocket  # websocket-client
except Exception:
    websocket = None   # module still imports; client just won't start


# ----- config (read from environment; set in .env) --------------------------
TRADOVATE_ENV      = os.getenv("TRADOVATE_ENV", "demo").lower().strip()
TRADOVATE_USERNAME = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID   = os.getenv("TRADOVATE_APP_ID", "")
TRADOVATE_CID      = os.getenv("TRADOVATE_CID", "")
TRADOVATE_SECRET   = os.getenv("TRADOVATE_SECRET", "")

_REST = {"demo": "https://demo.tradovateapi.com/v1",
         "live": "https://live.tradovateapi.com/v1"}
_WSMD = {"demo": "wss://md-demo.tradovateapi.com/v1/websocket",
         "live": "wss://md.tradovateapi.com/v1/websocket"}

# Quarterly contract month codes (used only as a last-resort fallback).
_MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}

_REST_DATA = {  # contract lookup lives on the data REST host
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}


def _heuristic_front_month(root: str) -> str:
    """Last-resort guess if the live lookup fails. e.g. 'MNQ' -> 'MNQU6'."""
    now = dt.datetime.now(dt.timezone.utc)
    y, m = now.year, now.month
    q_months = [3, 6, 9, 12]
    # bias to NEXT quarter when within ~2 weeks of a roll month
    nxt = next((q for q in q_months if q > m), None)
    if nxt is None:
        nxt, y = 3, y + 1
    return f"{root}{_MONTH_CODE[nxt]}{y % 10}"


def resolve_front_contract(root: str, token: str, env: str) -> str:
    """Ask Tradovate for the ACTIVE front-month contract name for a root
    (e.g. 'MNQ' -> 'MNQU6'), instead of guessing. Uses contract/suggest, which
    returns currently-tradeable contracts. Falls back to the heuristic on any
    failure so we always return *something*."""
    base = _REST_DATA.get(env, _REST_DATA["demo"])
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        # contract/suggest returns matching tradeable contracts, soonest first
        r = requests.get(f"{base}/contract/suggest",
                         params={"t": root, "l": 10}, headers=hdr, timeout=15)
        items = r.json()
        # keep only names that start with the root + a month code (front-month
        # style), pick the soonest-expiring tradeable one
        names = [it.get("name", "") for it in items if isinstance(it, dict)]
        cands = [n for n in names if n.startswith(root) and len(n) >= len(root) + 2]
        if cands:
            # contract/suggest is ordered with the active contract first
            chosen = cands[0]
            print(f"  [tv] resolved {root} -> {chosen} (via contract/suggest)")
            return chosen
        print(f"  [tv] contract/suggest returned no match for {root}; raw={names[:5]}")
    except Exception as e:
        print(f"  [tv] contract lookup error for {root}: {e}")
    fallback = _heuristic_front_month(root)
    print(f"  [tv] falling back to heuristic for {root} -> {fallback}")
    return fallback


class TradovateMD:
    """Background WebSocket client maintaining a latest-quote cache."""

    def __init__(self):
        self._token = None
        self._token_exp = 0
        self._ws = None
        self._thread = None
        self._req_id = 1
        self._running = False
        self._lock = threading.Lock()
        self._quotes = {}          # symbol -> {"price": float, "chg_pct": float}
        self._subscribed = set()   # contract symbols we've subscribed to
        self._want = set()         # contract symbols we want subscribed

    # ---- REST auth ---------------------------------------------------------
    def _get_token(self) -> str:
        if not (TRADOVATE_USERNAME and TRADOVATE_PASSWORD and TRADOVATE_SECRET):
            return ""
        if self._token and time.time() < self._token_exp:
            return self._token
        base = _REST.get(TRADOVATE_ENV, _REST["demo"])
        try:
            r = requests.post(f"{base}/auth/accessTokenRequest", json={
                "name": TRADOVATE_USERNAME, "password": TRADOVATE_PASSWORD,
                "appId": TRADOVATE_APP_ID, "appVersion": "1.0",
                "cid": TRADOVATE_CID, "sec": TRADOVATE_SECRET,
            }, timeout=20)
            data = r.json()
            if data.get("p-ticket"):
                print(f"  [tv] time-penalty; retry after {data.get('p-time')}s")
                return ""
            tok = data.get("accessToken")
            if not tok:
                print(f"  [tv] auth failed: {data.get('errorText') or data}")
                return ""
            self._token = tok
            self._token_exp = time.time() + 75 * 60
            return tok
        except Exception as e:
            print(f"  [tv] auth error: {e}")
            return ""

    # ---- frame helpers -----------------------------------------------------
    def _send(self, endpoint: str, body: str = ""):
        """Send a Tradovate request frame: endpoint\\n<id>\\n<query>\\n<body>."""
        if not self._ws:
            return
        rid = self._req_id
        self._req_id += 1
        frame = f"{endpoint}\n{rid}\n\n{body}"
        try:
            self._ws.send(frame)
        except Exception as e:
            print(f"  [tv] send error: {e}")

    # ---- WS lifecycle ------------------------------------------------------
    def _on_open(self, ws):
        # First frame after open MUST be authorize.
        token = self._get_token()
        if not token:
            print("  [tv] no token; closing socket")
            try: ws.close()
            except Exception: pass
            return
        ws.send(f"authorize\n0\n\n{token}")

    def _on_message(self, ws, message):
        if not message:
            return
        kind = message[0]
        if kind == "o":                       # socket opened
            return
        if kind == "h":                       # heartbeat -> echo
            try: ws.send("[]")                # empty array keeps it alive
            except Exception: pass
            return
        if kind == "a":                       # data array
            try:
                payload = json.loads(message[1:])
            except Exception:
                return
            for msg in payload:
                self._handle_data(ws, msg)

    def _handle_data(self, ws, msg):
        # Auth/response envelopes carry s (status) + i (req id); quote pushes
        # arrive as {"e":"md","d":{"quotes":[...]}}.
        if os.getenv("TV_DEBUG"):
            print(f"  [tv:debug] frame: {json.dumps(msg)[:400]}")
        if msg.get("s") == 200 and msg.get("i") == 0:
            # authorize succeeded -> (re)subscribe to everything we want
            self._after_auth(ws)
            return
        # response to a subscribe request (has our req id, not 0)
        if msg.get("s") and msg.get("i"):
            if msg.get("s") != 200:
                print(f"  [tv] subscribe rejected (req {msg.get('i')}): {msg.get('d')}")
            else:
                # success envelope often carries the initial quote snapshot in d
                d = msg.get("d")
                if os.getenv("TV_DEBUG"):
                    print(f"  [tv:debug] subscribe OK (req {msg.get('i')}) d={json.dumps(d)[:400]}")
                # some responses include the first quote directly
                if isinstance(d, dict) and ("entries" in d or "quotes" in d):
                    quotes = d.get("quotes", [d]) if "entries" not in d else [d]
                    for q in quotes:
                        self._ingest_quote(q)
            return
        if msg.get("e") == "md":
            for q in msg.get("d", {}).get("quotes", []):
                self._ingest_quote(q)

    def _after_auth(self, ws):
        print(f"  [tv] authorized; subscribing to: {sorted(self._want)}")
        for contract in list(self._want):
            self._subscribe(contract)

    def _subscribe(self, contract: str):
        if contract in self._subscribed:
            return
        print(f"  [tv] subscribing to contract symbol: {contract!r}")
        # md/subscribequote body is a JSON object: {"symbol":"MNQM6"}
        self._send("md/subscribequote", json.dumps({"symbol": contract}))
        self._subscribed.add(contract)

    def _ingest_quote(self, q: dict):
        # Quote entry shape: {"symbol":..., "entries":{"Trade":{"price":...},
        #                      "OpeningPrice":{"price":...}, ...}}
        sym = q.get("symbol") or q.get("contractId")
        entries = q.get("entries", {})
        last = (entries.get("Trade") or {}).get("price")
        openp = (entries.get("OpeningPrice") or {}).get("price")
        if last is None:
            return
        chg_pct = 0.0
        if openp:
            try: chg_pct = (last - openp) / openp * 100.0
            except Exception: chg_pct = 0.0
        with self._lock:
            self._quotes[str(sym)] = {"price": float(last),
                                      "chg_pct": float(chg_pct)}

    def _on_error(self, ws, err):
        print(f"  [tv] ws error: {err}")

    def _on_close(self, ws, *a):
        self._subscribed.clear()
        # reconnect after a short delay if we're meant to keep running
        if self._running:
            time.sleep(5)
            self._connect()

    def _connect(self):
        if websocket is None:
            print("  [tv] websocket-client not installed (pip install websocket-client)")
            return
        url = _WSMD.get(TRADOVATE_ENV, _WSMD["demo"])
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # run_forever with a built-in ping keeps the TCP alive between heartbeats
        self._ws.run_forever(ping_interval=10, ping_timeout=5)

    # ---- public API --------------------------------------------------------
    def start(self, roots: list[str]):
        """Begin streaming the given futures roots (e.g. ['MNQ','MES'])."""
        if not (TRADOVATE_USERNAME and TRADOVATE_PASSWORD and TRADOVATE_SECRET):
            print("  [tv] not configured — set TRADOVATE_* in .env")
            return
        # Resolve real, currently-active contract names via Tradovate (needs a
        # token). Falls back to a heuristic per-root if the lookup fails.
        token = self._get_token()
        if not token:
            print("  [tv] could not get token to resolve contracts")
            return
        self._root_to_contract = {
            r: resolve_front_contract(r, token, TRADOVATE_ENV) for r in roots
        }
        self._want = set(self._root_to_contract.values())
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._connect, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._ws:
            try: self._ws.close()
            except Exception: pass

    def get(self, roots: list[str]) -> dict:
        """Return {root: {"price","chg_pct"}} for the requested futures roots,
        reading the latest cached quotes (non-blocking)."""
        out = {}
        with self._lock:
            for r in roots:
                contract = getattr(self, "_root_to_contract", {}).get(r)
                if contract and contract in self._quotes:
                    out[r] = dict(self._quotes[contract])
        return out


# module-level singleton the quotes layer can import
_client: Optional[TradovateMD] = None

def get_client() -> TradovateMD:
    global _client
    if _client is None:
        _client = TradovateMD()
    return _client


if __name__ == "__main__":
    # Manual smoke test (needs creds + websocket-client + a data subscription):
    #   pip install websocket-client
    #   TRADOVATE_* set in environment, then:  python korvus_tradovate.py
    roots = ["MNQ", "MES"]
    c = get_client()
    c.start(roots)
    print("connecting… (Ctrl+C to stop)")
    try:
        for _ in range(12):
            time.sleep(5)
            print("quotes:", c.get(roots))
    except KeyboardInterrupt:
        pass
    finally:
        c.stop()

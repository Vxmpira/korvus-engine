#!/usr/bin/env python3
"""
==============================================================================
 KORVUS QUOTES  ·  Phase 3   (by BlackCrownVxJ.LLC)
==============================================================================
 Live price provider for the Futures Board, SMT panel, and Funds Watch.

 Swappable, just like the news layer. Set QUOTES_PROVIDER in .env to:
   "alphavantage"  -> true live/realtime (PREMIUM key required)  [recommended]
   "finnhub"       -> free tier (~15-20 min delayed)             [no cost]
   "off"           -> panels stay on the dashboard's sample numbers

 Upgrading from free to live = change ONE line in .env. No code changes.

 IMPORTANT — market hours:
   US equities (and the ETF proxies QQQ/SPY/DIA) only trade ~9:30am-4:00pm ET.
   Outside that, this returns the last available close and flags market_open=false
   so the dashboard can show "last close" instead of pretending it's live.

 REDISTRIBUTION NOTE:
   Personal live quotes are fine for YOUR use. Re-serving real-time exchange
   quotes to other members is "redistribution" and needs a separate license.
   For the members site, keep this as each user's personal panel; members'
   execution prices should come from their own platform (e.g. Tradovate).
==============================================================================
"""
import os
import datetime as dt
import requests
from dotenv import load_dotenv

load_dotenv()

QUOTES_PROVIDER  = os.getenv("QUOTES_PROVIDER", "finnhub").lower().strip()
ALPHAVANTAGE_KEY = os.getenv("ALPHAVANTAGE_KEY", "")
FINNHUB_KEY      = os.getenv("FINNHUB_KEY", "")

# --- Tradovate (real CME futures; dormant until credentials are set) ---------
# NOTE: Tradovate does NOT use a single paste-in API key. Auth is:
#   POST auth/accessTokenRequest  with {name, password, appId, appVersion, cid, sec}
#   -> returns a Bearer accessToken that EXPIRES (~80 min) and must be renewed.
# Real-time quotes then stream over a WebSocket (md/subscribequote), and require
# a PAID Tradovate market-data subscription on the account (live data is not free
# just because the API works — see the gotcha in fetch notes below).
TRADOVATE_ENV       = os.getenv("TRADOVATE_ENV", "demo").lower().strip()   # 'demo' | 'live'
TRADOVATE_USERNAME  = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD  = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID    = os.getenv("TRADOVATE_APP_ID", "")     # "appId" from your Tradovate API app
TRADOVATE_CID       = os.getenv("TRADOVATE_CID", "")        # API "cid"
TRADOVATE_SECRET    = os.getenv("TRADOVATE_SECRET", "")     # API "sec" (personal secret key)

# simple in-memory cache so we don't hammer the API (quotes refresh every ~30s)
_cache = {"at": 0, "data": {}}
_CACHE_SECONDS = 25


def market_is_open() -> bool:
    """Stock RTH check in ET (Mon-Fri 9:30-16:00). Ignores holidays.
    This reflects whether the ETF PROXIES (QQQ/SPY/DIA) are actively trading,
    i.e. whether the price data is FRESH. See futures_session() for whether the
    futures themselves (MNQ/MES on Globex) are tradable."""
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))
    if now.weekday() >= 5:           # Sat/Sun
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins <= (16 * 60)


def futures_session() -> str:
    """CME index-futures (Globex) session state for MNQ/MES/etc., ET.
    Globex runs Sun 6:00pm ET -> Fri 5:00pm ET, with a daily maintenance
    halt 5:00pm-6:00pm ET (Mon-Thu). Returns one of:
       "rth"     - stock regular hours: futures open AND proxy prices fresh
       "globex"  - futures open, but ETF-proxy prices are at last close (stale)
       "closed"  - futures closed (weekend gap / daily maintenance break)
    Ignores holidays (good enough for a status label)."""
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))
    wd   = now.weekday()                 # Mon=0 .. Sun=6
    mins = now.hour * 60 + now.minute

    # Stock regular trading hours -> proxies are live
    if wd < 5 and (9 * 60 + 30) <= mins <= (16 * 60):
        return "rth"

    # Daily maintenance break: 17:00-18:00 ET, Mon-Thu (and Fri close at 17:00)
    # Weekend closed: Fri 17:00 -> Sun 18:00
    if wd == 5:                          # Saturday: fully closed
        return "closed"
    if wd == 6:                          # Sunday: closed until 18:00 ET
        return "globex" if mins >= 18 * 60 else "closed"
    if wd == 4:                          # Friday: closes 17:00 ET
        return "closed" if mins >= 17 * 60 else ("globex" if mins < (9*60+30) or mins > (16*60) else "rth")
    # Mon-Thu: open except the 17:00-18:00 maintenance hour
    if 17 * 60 <= mins < 18 * 60:
        return "closed"
    return "globex"


def get_quotes(symbols: list[str], force_delayed: bool = False) -> dict:
    """
    Returns { "SYM": {"price": float, "chg_pct": float}, ... } plus a
    "_meta" key with provider + market_open. Cached for ~25s.

    force_delayed=True (used for free-tier users) ignores the configured
    provider and uses the delayed feed (finnhub), so live data is never
    served to non-paying users even if alphavantage is configured.
    """
    import time
    now = time.time()
    cache_key = ("delayed:" if force_delayed else "live:") + ",".join(symbols)
    if _cache["data"].get(cache_key) and (now - _cache["at"] < _CACHE_SECONDS):
        return _cache["data"][cache_key]

    if force_delayed:
        # free tier: always delayed, never the premium live feed
        out = _finnhub_quotes(symbols) if FINNHUB_KEY else {}
    elif QUOTES_PROVIDER == "alphavantage":
        out = _alphavantage_quotes(symbols)
    elif QUOTES_PROVIDER == "finnhub":
        out = _finnhub_quotes(symbols)
    elif QUOTES_PROVIDER == "tradovate":
        out = _tradovate_quotes(symbols)
    else:
        out = {}  # 'off' -> dashboard keeps its sample numbers

    out["_meta"] = {"provider": ("finnhub" if force_delayed else QUOTES_PROVIDER),
                    "market_open": market_is_open(),
                    "futures_session": futures_session()}
    _cache["data"][cache_key] = out
    _cache["at"] = now
    return out


# --- Alpha Vantage PREMIUM (true live; bulk endpoint, up to 100 symbols) ----
def _alphavantage_quotes(symbols: list[str]) -> dict:
    if not ALPHAVANTAGE_KEY:
        print("  [quotes] no ALPHAVANTAGE_KEY — set it in .env")
        return {}
    out = {}
    try:
        # REALTIME_BULK_QUOTES is a premium endpoint; takes up to 100 symbols.
        url = ("https://www.alphavantage.co/query"
               f"?function=REALTIME_BULK_QUOTES&symbol={','.join(symbols)}"
               f"&apikey={ALPHAVANTAGE_KEY}")
        r = requests.get(url, timeout=20)
        data = r.json()
        rows = data.get("data", [])
        if not rows and ("Information" in data or "Note" in data):
            # usually means the key isn't premium yet
            print(f"  [quotes] Alpha Vantage: {data.get('Information') or data.get('Note')}")
            return {}
        for row in rows:
            sym = row.get("symbol")
            price = float(row.get("close") or row.get("price") or 0)
            chg = row.get("change_percent", "0").replace("%", "")
            out[sym] = {"price": price, "chg_pct": float(chg or 0)}
    except Exception as e:
        print(f"  [quotes] Alpha Vantage error: {e}")
    return out


# --- Finnhub FREE (delayed ~15-20 min; one call per symbol) ------------------
def _finnhub_quotes(symbols: list[str]) -> dict:
    if not FINNHUB_KEY:
        print("  [quotes] no FINNHUB_KEY — set it in .env")
        return {}
    out = {}
    for sym in symbols:
        try:
            r = requests.get("https://finnhub.io/api/v1/quote",
                             params={"symbol": sym, "token": FINNHUB_KEY}, timeout=15)
            q = r.json()
            # c=current, dp=percent change
            if q.get("c"):
                out[sym] = {"price": float(q["c"]), "chg_pct": float(q.get("dp") or 0)}
        except Exception as e:
            print(f"  [quotes] Finnhub error on {sym}: {e}")
    return out


# --- Tradovate (real CME futures) -------------------------------------------
# Activation: set in .env ->
#   QUOTES_PROVIDER=tradovate
#   TRADOVATE_ENV=live            (use 'demo' to test against the demo system)
#   TRADOVATE_USERNAME=...        your Tradovate login
#   TRADOVATE_PASSWORD=...        your Tradovate password
#   TRADOVATE_APP_ID=...          from your Tradovate API application
#   TRADOVATE_CID=...             API "cid"
#   TRADOVATE_SECRET=...          API "sec" (personal secret key)
#
# IMPORTANT — two things beyond credentials:
#   1) LIVE real-time CME data requires a PAID market-data subscription on your
#      Tradovate account. The API can authenticate and the WebSocket can connect,
#      yet quotes come back EMPTY if the data subscription isn't active. That is
#      a Tradovate/CME entitlement, not a bug in this code.
#   2) REDISTRIBUTION: showing YOUR live CME quotes to other members is exchange
#      "redistribution" and needs a separate CME license. For members, keep this
#      as YOUR personal panel only; do not re-serve live ticks to other users
#      until that licensing is sorted with an attorney. (See module header.)
# ----------------------------------------------------------------------------
_TV_BASE = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}
_tv_token = {"token": None, "expires": 0}

# MNQ/MES etc. -> Tradovate front-month contract symbols look like "MNQM6".
# The active month rolls quarterly (Mar=H, Jun=M, Sep=U, Dec=Z), so in
# production you resolve the front month via contract/find or contract/list
# rather than hardcoding. Left as a TODO so this stays a scaffold, not a
# half-working hardcode that silently goes stale at the next contract roll.
TRADOVATE_FUT = {"MNQ": "MNQ", "MES": "MES", "MYM": "MYM", "M2K": "M2K",
                 "CL": "CL", "GC": "GC", "ZN": "ZN", "6E": "6E"}


def _tradovate_token() -> str:
    """Acquire/cache a Tradovate Bearer access token via REST.
    Tokens expire (~80 min); we refresh a few minutes early. Returns '' if not
    configured or on failure (so the dashboard falls back to samples)."""
    import time
    if not (TRADOVATE_USERNAME and TRADOVATE_PASSWORD and TRADOVATE_SECRET):
        print("  [quotes] Tradovate not configured — set TRADOVATE_* in .env")
        return ""
    now = time.time()
    if _tv_token["token"] and now < _tv_token["expires"]:
        return _tv_token["token"]
    base = _TV_BASE.get(TRADOVATE_ENV, _TV_BASE["demo"])
    try:
        r = requests.post(f"{base}/auth/accessTokenRequest", json={
            "name": TRADOVATE_USERNAME,
            "password": TRADOVATE_PASSWORD,
            "appId": TRADOVATE_APP_ID,
            "appVersion": "1.0",
            "cid": TRADOVATE_CID,
            "sec": TRADOVATE_SECRET,
        }, timeout=20)
        data = r.json()
        # Tradovate may return a p-ticket time penalty instead of a token; if so,
        # it must be retried after p-time. Surface it rather than hammering.
        if data.get("p-ticket"):
            print(f"  [quotes] Tradovate time-penalty (p-ticket); retry after {data.get('p-time')}s")
            return ""
        tok = data.get("accessToken")
        if not tok:
            print(f"  [quotes] Tradovate auth failed: {data.get('errorText') or data}")
            return ""
        _tv_token["token"] = tok
        _tv_token["expires"] = now + 75 * 60      # refresh ~5 min before expiry
        return tok
    except Exception as e:
        print(f"  [quotes] Tradovate auth error: {e}")
        return ""


def _tradovate_quotes(symbols: list[str]) -> dict:
    """Live CME futures via the Tradovate WebSocket client (korvus_tradovate.py).

    On first call this starts a background WebSocket that authorizes and
    subscribes to the futures roots, then maintains a latest-quote cache. Each
    call here just reads that cache (non-blocking) and returns {root: {...}}.

    Returns {} when unconfigured, when websocket-client isn't installed, or
    before the first ticks arrive — in all cases the dashboard keeps its
    previous numbers, so nothing breaks.

    Caveats (see korvus_tradovate.py header): needs a PAID Tradovate market-data
    subscription for live ticks, and re-serving these to members is exchange
    redistribution requiring a separate CME license.
    """
    try:
        from korvus_tradovate import get_client
    except Exception as e:
        print(f"  [quotes] Tradovate client unavailable: {e}")
        return {}

    # which of the requested symbols are futures roots we know how to stream
    roots = [s for s in symbols if s in TRADOVATE_FUT]
    if not roots:
        return {}

    client = get_client()
    client.start(roots)          # idempotent: only starts the socket once
    quotes = client.get(roots)   # latest cached values (may be empty until ticks arrive)
    if not quotes:
        print("  [quotes] Tradovate: connected/starting — no ticks cached yet "
              "(check data subscription if this persists during RTH)")
    return quotes


if __name__ == "__main__":
    # quick manual test:  python korvus_quotes.py
    test = ["QQQ", "SPY", "DIA", "NVDA"]
    print(f"Provider: {QUOTES_PROVIDER}  |  market_open: {market_is_open()}")
    res = get_quotes(test)
    for s in test:
        if s in res:
            print(f"  {s:5} {res[s]['price']:>10.2f}  {res[s]['chg_pct']:+.2f}%")
        else:
            print(f"  {s:5} (no data — check key / provider)")

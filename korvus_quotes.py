#!/usr/bin/env python3
"""
==============================================================================
 KORVUS QUOTES  ·  Phase 3   (by BlackCrownVxJ.LLC)
==============================================================================
 Live price provider for the Futures Board, SMT panel, and Funds Watch.

 Swappable, just like the news layer. Set QUOTES_PROVIDER in .env to:
   "databento"     -> REAL CME futures (MNQ/MES/etc.), licensed feed  [accurate]
   "alphavantage"  -> ETF-proxy live/realtime (PREMIUM key required)
   "finnhub"       -> ETF-proxy free tier (~15-20 min delayed)        [no cost]
   "off"           -> panels stay on the dashboard's sample numbers

 Upgrading from free to live = change ONE line in .env. No code changes.

 NOTE ON ACCURACY: alphavantage/finnhub price the ETF PROXIES (QQQ for NQ, SPY
 for ES, ...), so the board shows the proxy's % move, not the real contract.
 "databento" pulls the ACTUAL CME contract (MNQ ~21,000), so the board, session
 map and prices match a futures chart. It is DORMANT until DATABENTO_API_KEY is
 set AND QUOTES_PROVIDER=databento (see korvus_databento.py).

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

# --- Databento (real CME futures; dormant until a licensed key is set) -------
# Set QUOTES_PROVIDER=databento and DATABENTO_API_KEY=... in .env to activate.
# With no key this provider returns {} and the board stays on the proxy feed.
DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "").strip()
# Futures roots Databento can serve from GLBX.MDP3 (VX is CBOE -> proxy only).
DATABENTO_FUT = {"MNQ", "MES", "MYM", "M2K", "CL", "GC", "ZN", "6E"}

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

# ---------------------------------------------------------------------------
# Market-holiday awareness
# ---------------------------------------------------------------------------
# US equity full-day closures (NYSE / Nasdaq). On these dates the QQQ/SPY/DIA
# proxies do not trade at all. Source: NYSE/Nasdaq published 2026-2027 calendars.
# Refresh this once a year; the live-feed freshness check below is the safety
# net for any date or early-close time not captured here.
_EQUITY_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Presidents' Day",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Presidents' Day",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving",
    "2027-12-24": "Christmas (observed)",
}
# US equity half-days: regular open, early close at 1:00 PM ET.
_EQUITY_EARLY_CLOSE = {
    "2026-11-27": "Day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "Day after Thanksgiving",
}
# A bar older than this means the live CME feed has gone quiet -> not trading.
# 1-min OHLCV bars arrive every minute while open, so ~2.5 min of silence is a
# reliable "market closed / halted" signal that needs no hardcoded clock.
_FEED_STALE_SECONDS = 150


def _et_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))


def market_holiday():
    """Return the holiday name if today is a full US equity-market closure,
    else None. Used so the board can honestly show CLOSED / the holiday name
    instead of pretending a frozen last price is live."""
    return _EQUITY_HOLIDAYS.get(_et_now().date().isoformat())


def _equity_early_close_today():
    return _EQUITY_EARLY_CLOSE.get(_et_now().date().isoformat())


def _databento_feed_age():
    """Seconds since the live CME feed last produced a bar (databento provider
    only), or None when that feed isn't the active source. None means 'no
    freshness signal available', so the clock-based logic is used instead."""
    if QUOTES_PROVIDER != "databento" or not DATABENTO_API_KEY:
        return None
    try:
        from korvus_databento import feed_age
        return feed_age()
    except Exception:
        return None


def market_is_open() -> bool:
    """Stock RTH check in ET (Mon-Fri 9:30-16:00), now holiday-aware.
    This reflects whether the ETF PROXIES (QQQ/SPY/DIA) are actively trading,
    i.e. whether the price data is FRESH. Returns False on weekends, full US
    equity holidays, and outside trading hours; on half-days it closes at 1pm.
    See futures_session() for whether the futures themselves are tradable."""
    now = _et_now()
    if now.weekday() >= 5:           # Sat/Sun
        return False
    if market_holiday():             # full equity closure (e.g. Juneteenth)
        return False
    mins = now.hour * 60 + now.minute
    close_min = (13 * 60) if _equity_early_close_today() else (16 * 60)
    return (9 * 60 + 30) <= mins <= close_min


def futures_session() -> str:
    """CME index-futures (Globex) session state for MNQ/MES/etc., ET.
    Returns "rth" | "globex" | "closed".

    Truth order:
      1) LIVE FEED FRESHNESS (databento): if real bars have stopped arriving,
         the market is not trading right now, full stop. This catches holiday
         early-closes, the daily maintenance halt, weekends, and trading halts
         without trusting a fixed clock (CME holiday hours shift yearly).
      2) FULL EQUITY HOLIDAY: cash is closed all day; CME runs a modified,
         usually early-closing schedule, so after ~1pm ET we call it closed.
      3) CLOCK fallback (no live feed): the standard Globex weekly schedule."""
    # 1) Live-feed freshness wins when we have it.
    age = _databento_feed_age()
    if age is not None and age > _FEED_STALE_SECONDS:
        return "closed"

    now = _et_now()
    wd   = now.weekday()                 # Mon=0 .. Sun=6
    mins = now.hour * 60 + now.minute

    # 2) Full US equity holiday (e.g. Juneteenth): proxies dark all day; CME
    #    futures typically early-close ~1pm ET. Before that, treat as globex
    #    (futures open, proxies stale); after, closed.
    if market_holiday():
        return "globex" if mins < (13 * 60) else "closed"

    # 3) Clock fallback. Stock regular trading hours -> proxies are live.
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
        # free tier / logged-out / public landing ticker: always delayed,
        # never the premium live feed. Prefer Finnhub if configured; otherwise
        # fall back to Alpha Vantage with entitlement=delayed so the public
        # ticker still shows real (15-min delayed) prices using the same key.
        if FINNHUB_KEY:
            out = _finnhub_quotes(symbols)
        elif ALPHAVANTAGE_KEY:
            out = _alphavantage_quotes(symbols, delayed=True)
        else:
            out = {}
        _delayed_provider = ("finnhub" if FINNHUB_KEY
                             else ("alphavantage-delayed" if ALPHAVANTAGE_KEY else "none"))
    elif QUOTES_PROVIDER == "databento":
        out = _databento_quotes(symbols)
    elif QUOTES_PROVIDER == "alphavantage":
        out = _alphavantage_quotes(symbols)
    elif QUOTES_PROVIDER == "finnhub":
        out = _finnhub_quotes(symbols)
    elif QUOTES_PROVIDER == "tradovate":
        out = _tradovate_quotes(symbols)
    else:
        out = {}  # 'off' -> dashboard keeps its sample numbers

    out["_meta"] = {"provider": (_delayed_provider if force_delayed else QUOTES_PROVIDER),
                    "market_open": market_is_open(),
                    "futures_session": futures_session(),
                    "holiday": market_holiday()}
    _cache["data"][cache_key] = out
    _cache["at"] = now
    return out


def native_futures() -> bool:
    """True when the configured feed serves REAL CME futures (Databento), so the
    dashboard can request the actual contracts (MNQ/MES/...) instead of proxies.
    The server gates this to Pro users in /api/me, since free users are always
    forced onto the delayed proxy feed regardless of provider."""
    return QUOTES_PROVIDER == "databento" and bool(DATABENTO_API_KEY)


# --- Alpha Vantage PREMIUM (true live; bulk endpoint, up to 100 symbols) ----
def _alphavantage_quotes(symbols: list[str], delayed: bool = False) -> dict:
    if not ALPHAVANTAGE_KEY:
        print("  [quotes] no ALPHAVANTAGE_KEY — set it in .env")
        return {}
    out = {}

    def _f(v):
        try:
            return float(str(v).replace("%", "").strip())
        except Exception:
            return 0.0

    try:
        # REALTIME_BULK_QUOTES is a premium endpoint; takes up to 100 symbols.
        # IMPORTANT: without entitlement, Alpha Vantage returns HISTORICAL data.
        #   entitlement=realtime -> true real-time US data (Pro members)
        #   entitlement=delayed  -> 15-min delayed (free / logged-out / public
        #                           landing ticker). Never serves real-time to
        #                           non-paying users even though the key is premium.
        entitlement = "delayed" if delayed else "realtime"
        url = ("https://www.alphavantage.co/query"
               f"?function=REALTIME_BULK_QUOTES&symbol={','.join(symbols)}"
               f"&entitlement={entitlement}"
               f"&apikey={ALPHAVANTAGE_KEY}")
        r = requests.get(url, timeout=20)
        data = r.json()
        rows = data.get("data", [])
        if not rows and ("Information" in data or "Note" in data or "Error Message" in data):
            # usually means the key isn't premium yet, or entitlement isn't set up
            print(f"  [quotes] Alpha Vantage: {data.get('Information') or data.get('Note') or data.get('Error Message')}")
            return {}
        for row in rows:
            sym = row.get("symbol")
            if not sym:
                continue
            price = _f(row.get("close") or row.get("price") or 0)
            # carry OHLC + prev close so the SMT range read and macro bands work
            # on the live feed too (parsed defensively — keys may be absent).
            out[sym] = {
                "price": price,
                "chg_pct": _f(row.get("change_percent", 0)),
                "high": _f(row.get("high") or 0),
                "low": _f(row.get("low") or 0),
                "open": _f(row.get("open") or 0),
                "prev_close": _f(row.get("previous_close") or row.get("prev_close") or 0),
            }
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
            # c=current, dp=percent change, h=day high, l=day low, o=open, pc=prev close
            if q.get("c"):
                out[sym] = {
                    "price": float(q["c"]),
                    "chg_pct": float(q.get("dp") or 0),
                    "high": float(q.get("h") or 0),
                    "low": float(q.get("l") or 0),
                    "open": float(q.get("o") or 0),
                    "prev_close": float(q.get("pc") or 0),
                }
        except Exception as e:
            print(f"  [quotes] Finnhub error on {sym}: {e}")
    return out


# --- Databento (real CME futures) -------------------------------------------
# Activation (.env):
#   QUOTES_PROVIDER=databento
#   DATABENTO_API_KEY=db-...        your 32-char Databento key (paid + licensed)
#   DATABENTO_ROLL=c                optional: c=calendar front (default), n=OI, v=volume
#
# On first call this starts a background live stream (korvus_databento.py) that
# subscribes to the CME Globex feed and maintains a latest-quote snapshot keyed
# by ROOT (MNQ, MES, ...). Each call here just reads that snapshot (non-blocking).
# Returns {} when unconfigured, when the `databento` package isn't installed, or
# before the first bars arrive — so the dashboard simply keeps its prior numbers.
def _databento_quotes(symbols: list[str]) -> dict:
    if not DATABENTO_API_KEY:
        print("  [quotes] no DATABENTO_API_KEY — set it in .env (provider dormant)")
        return {}
    try:
        from korvus_databento import get_client
    except Exception as e:
        print(f"  [quotes] Databento client unavailable: {e}")
        return {}
    roots = [s for s in symbols if s in DATABENTO_FUT]
    if not roots:
        return {}
    client = get_client(DATABENTO_API_KEY)
    client.start(sorted(DATABENTO_FUT))   # idempotent: stream starts once
    quotes = client.get(roots)
    if not quotes:
        print("  [quotes] Databento: connected/starting — no bars cached yet "
              "(first 1-min bar can take up to ~60s; check license if it persists)")
    return quotes



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

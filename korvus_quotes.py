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

# simple in-memory cache so we don't hammer the API (quotes refresh every ~30s)
_cache = {"at": 0, "data": {}}
_CACHE_SECONDS = 25


def market_is_open() -> bool:
    """Rough US equities RTH check in ET (Mon-Fri 9:30-16:00). Ignores holidays."""
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))
    if now.weekday() >= 5:           # Sat/Sun
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins <= (16 * 60)


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
    else:
        out = {}  # 'off' -> dashboard keeps its sample numbers

    out["_meta"] = {"provider": ("finnhub" if force_delayed else QUOTES_PROVIDER),
                    "market_open": market_is_open()}
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

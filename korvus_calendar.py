#!/usr/bin/env python3
"""
==============================================================================
 KORVUS CALENDAR  ·  Forex / Gov News economic calendar   (by BlackCrownVxJ.LLC)
==============================================================================
 Server-side provider for the Forex / Gov News page (korvus_forex_calendar.html).
 Served at /api/ff-calendar (see korvus_server.py). The page fetches that
 endpoint and renders the week's government data releases.

 WHY THIS LIVES ON THE SERVER (not the browser):
   The free Forex Factory weekly JSON is CORS-blocked AND rate-limited — polled
   from a browser it returns a "Request Denied" page, not data. So it MUST be
   fetched server-side and cached. This module does that, normalizes the shape,
   and caches it (~1 hour for Forex Factory, ~5 min for FMP) so the feed is
   never hammered.

 PROVIDERS (set CALENDAR_PROVIDER in .env):
   "fmp"          -> Financial Modeling Prep economic_calendar  [recommended]
                     The only one here that includes ACTUAL values. Needs a
                     paid FMP key (FMP_KEY). A legitimately licensed source —
                     NOT a paywall workaround.
   "forexfactory" -> Forex Factory free weekly JSON. No API key, but no
                     "actual" field (forecast/previous only). Good zero-cost
                     fallback; the page shows "—" in the Actual column.
   "auto"         -> use FMP if FMP_KEY is set, else Forex Factory. (default)

 ENTRY POINT:
   get_calendar() -> list[dict], each:
     { "title": str, "country": <CCY e.g. 'USD'>,
       "impact": "High"|"Medium"|"Low"|"Holiday",
       "date": <ISO 8601 string>,
       "forecast": str, "previous": str, "actual": str }
   Never raises — on any failure it returns the last good cache, or [].

 Quick manual test on the server:   python korvus_calendar.py
==============================================================================
"""
import os
import time
import datetime as dt

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

CALENDAR_PROVIDER = os.getenv("CALENDAR_PROVIDER", "auto").lower().strip()
FMP_KEY           = os.getenv("FMP_KEY", "").strip()

FF_URL  = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FMP_URL = "https://financialmodelingprep.com/api/v3/economic_calendar"

UA = "Mozilla/5.0 (compatible; korvus-engine/0.2; +https://korvus.industries)"

# cache TTLs (seconds)
TTL_FF  = 60 * 60        # Forex Factory: refresh hourly (it's rate-limited)
TTL_FMP = 5 * 60         # FMP: 5 minutes is plenty for actuals to land

# module-level cache: survives across requests within one server process
_cache = {"data": [], "ts": 0.0, "ttl": TTL_FF, "provider": ""}

# Map FMP 2-letter country codes -> the currency a futures/FX trader watches.
# Anything not listed falls back to the raw code from the feed.
_CCY = {
    "US": "USD", "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
    "GB": "GBP", "UK": "GBP", "JP": "JPY", "CN": "CNY", "CA": "CAD", "AU": "AUD",
    "NZ": "NZD", "CH": "CHF",
}

# normalize various impact spellings to the four the page understands
def _norm_impact(val: str) -> str:
    v = (val or "").strip().lower()
    if v in ("high", "3"):            return "High"
    if v in ("medium", "moderate", "2"): return "Medium"
    if v in ("low", "1"):             return "Low"
    if "holiday" in v:                return "Holiday"
    return "Low"


# ----------------------------------------------------------------------------
# Provider: Forex Factory free weekly JSON (no key; no "actual" field)
# ----------------------------------------------------------------------------
def _forexfactory() -> list:
    r = requests.get(FF_URL, timeout=20, headers={"User-Agent": UA})
    if r.status_code != 200:
        print(f"  [calendar] Forex Factory returned {r.status_code} — skipping")
        return []
    raw = r.json()
    out = []
    for e in raw:
        out.append({
            "title":    (e.get("title") or "").strip(),
            "country":  (e.get("country") or "").strip().upper(),  # FF already gives CCY
            "impact":   _norm_impact(e.get("impact")),
            "date":     e.get("date") or "",                       # ISO 8601 w/ offset
            "forecast": (e.get("forecast") or "").strip(),
            "previous": (e.get("previous") or "").strip(),
            "actual":   "",                                        # FF free feed has none
        })
    print(f"  [calendar] Forex Factory: {len(out)} events")
    return out


# ----------------------------------------------------------------------------
# Provider: Financial Modeling Prep (licensed; includes ACTUAL values)
# ----------------------------------------------------------------------------
def _fmp() -> list:
    if not FMP_KEY:
        print("  [calendar] no FMP_KEY set — cannot use FMP")
        return []
    today = dt.date.today()
    frm = today - dt.timedelta(days=today.weekday())     # Monday of this week
    to  = frm + dt.timedelta(days=6)                      # Sunday
    params = {"from": frm.isoformat(), "to": to.isoformat(), "apikey": FMP_KEY}
    r = requests.get(FMP_URL, params=params, timeout=20, headers={"User-Agent": UA})
    if r.status_code != 200:
        print(f"  [calendar] FMP returned {r.status_code} — skipping")
        return []
    raw = r.json()
    if not isinstance(raw, list):
        print(f"  [calendar] FMP unexpected response: {str(raw)[:120]}")
        return []
    out = []
    for e in raw:
        code = (e.get("country") or "").strip().upper()
        ccy = e.get("currency") or _CCY.get(code, code)
        out.append({
            "title":    (e.get("event") or "").strip(),
            "country":  (ccy or "").strip().upper(),
            "impact":   _norm_impact(e.get("impact")),
            "date":     e.get("date") or "",
            "forecast": str(e.get("estimate") if e.get("estimate") is not None else ""),
            "previous": str(e.get("previous") if e.get("previous") is not None else ""),
            "actual":   str(e.get("actual") if e.get("actual") is not None else ""),
        })
    print(f"  [calendar] FMP: {len(out)} events")
    return out


# ----------------------------------------------------------------------------
# Public entry point — cached, provider-switchable, never raises
# ----------------------------------------------------------------------------
def get_calendar(force_refresh: bool = False) -> list:
    """This-week economic calendar as a normalized list of dicts.
    Cached per-process so the upstream feed is never hammered. On any error,
    returns the last good cache (or [] if we never had one)."""
    now = time.time()
    if (not force_refresh) and _cache["data"] and (now - _cache["ts"] < _cache["ttl"]):
        return _cache["data"]

    provider = CALENDAR_PROVIDER
    if provider == "auto":
        provider = "fmp" if FMP_KEY else "forexfactory"

    data, ttl = [], TTL_FF
    try:
        if provider == "fmp":
            data = _fmp()
            ttl = TTL_FMP
            if not data:                       # FMP empty/failed -> fall back to free feed
                print("  [calendar] FMP empty — falling back to Forex Factory")
                data = _forexfactory()
                ttl = TTL_FF
        else:
            data = _forexfactory()
            ttl = TTL_FF
    except Exception as e:
        print(f"  [calendar] fetch error: {e}")
        data = []

    if data:
        _cache.update({"data": data, "ts": now, "ttl": ttl, "provider": provider})
        return data

    # fetch failed — serve whatever we had before rather than nothing
    return _cache["data"]


if __name__ == "__main__":
    prov = CALENDAR_PROVIDER if CALENDAR_PROVIDER != "auto" else ("fmp" if FMP_KEY else "forexfactory")
    print(f"Provider: {prov}  |  FMP_KEY set: {bool(FMP_KEY)}")
    events = get_calendar(force_refresh=True)
    print(f"Got {len(events)} events. First few:")
    for e in events[:8]:
        print(f"  {e['date'][:16]:18} {e['country']:4} {e['impact']:7} {e['title'][:48]}")

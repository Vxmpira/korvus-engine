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
   "merge"        -> Forex Factory's curated event list (majors only, correct
                     ET times, the releases that matter) with FMP's ACTUAL
                     value layered onto each event whose title corresponds.
                     Keeps the page clean AND shows actuals. Needs FMP_KEY.
   "fmp"          -> Financial Modeling Prep economic_calendar, raw. Includes
                     ACTUAL values but also every minor global print + ~60
                     currencies (noisy). Needs a paid FMP key (FMP_KEY).
   "forexfactory" -> Forex Factory free weekly JSON. No API key, but no
                     "actual" field (forecast/previous only). Clean but no
                     actuals; the page shows "—" in the Actual column.
   "auto"         -> use "merge" if FMP_KEY is set, else Forex Factory. (default)

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
import re
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
# FMP moved the economic calendar to the /stable/ path; the old /api/v3/ route
# now returns 403 for newer keys. Try stable first, fall back to legacy v3.
FMP_URLS = [
    "https://financialmodelingprep.com/stable/economic-calendar",   # current
    "https://financialmodelingprep.com/api/v3/economic_calendar",   # legacy fallback
]

UA = "Mozilla/5.0 (compatible; korvus-engine/0.2; +https://korvus.industries)"

# cache TTLs (seconds)
TTL_FF    = 60 * 60      # Forex Factory: refresh hourly (it's rate-limited)
TTL_FMP   = 5 * 60       # FMP: 5 minutes is plenty for actuals to land
TTL_MERGE = 5 * 60       # merge: recompute from the sub-caches every 5 min

# module-level cache: survives across requests within one server process
_cache = {"data": [], "ts": 0.0, "ttl": TTL_FF, "provider": ""}

# per-source sub-caches so merge mode can refresh FMP (actuals) every 5 min
# while only hitting the rate-limited Forex Factory feed hourly.
_sub = {"ff": {"data": None, "ts": 0.0}, "fmp": {"data": None, "ts": 0.0}}

def _cached(kind, fn, ttl):
    now = time.time()
    c = _sub[kind]
    if c["data"] is not None and (now - c["ts"] < ttl):
        return c["data"]
    d = fn()
    if d:
        c.update(data=d, ts=now)
    return c["data"] or []

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


# Normalize FMP's "YYYY-MM-DD HH:MM:SS" (UTC, no offset) into an unambiguous
# ISO-8601 string so the browser converts it to ET correctly. Forex Factory
# already sends an offset, so its dates pass through untouched.
def _fmp_date_iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "T" in s:                       # already ISO-ish (has offset/Z) — leave it
        return s
    if " " in s:                       # "YYYY-MM-DD HH:MM:SS" in UTC
        return s.replace(" ", "T", 1) + "+00:00"
    return s                           # date-only (holidays / all-day)


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

    raw = None
    for url in FMP_URLS:
        tag = url.rsplit("/", 1)[-1]
        try:
            r = requests.get(url, params=params, timeout=20, headers={"User-Agent": UA})
        except Exception as e:
            print(f"  [calendar] FMP {tag} request error: {e} — trying next")
            continue
        if r.status_code != 200:
            print(f"  [calendar] FMP {tag} returned {r.status_code} — trying next")
            continue
        try:
            j = r.json()
        except Exception:
            print(f"  [calendar] FMP {tag} returned non-JSON — trying next")
            continue
        if isinstance(j, dict) and (j.get("Error Message") or j.get("error")):
            print(f"  [calendar] FMP {tag} error: {str(j.get('Error Message') or j.get('error'))[:140]}")
            continue
        if isinstance(j, list):
            raw = j
            print(f"  [calendar] FMP via {tag}: {len(j)} raw rows")
            break
        print(f"  [calendar] FMP {tag} unexpected response: {str(j)[:120]}")

    if raw is None:
        return []

    out = []
    for e in raw:
        code = (e.get("country") or "").strip().upper()
        ccy = e.get("currency") or _CCY.get(code, code)
        out.append({
            "title":    (e.get("event") or "").strip(),
            "country":  (ccy or "").strip().upper(),
            "impact":   _norm_impact(e.get("impact")),
            "date":     _fmp_date_iso(e.get("date")),
            "forecast": str(e.get("estimate") if e.get("estimate") is not None else ""),
            "previous": str(e.get("previous") if e.get("previous") is not None else ""),
            "actual":   str(e.get("actual") if e.get("actual") is not None else ""),
        })
    print(f"  [calendar] FMP: {len(out)} events")
    return out


# ----------------------------------------------------------------------------
# Provider: MERGE — Forex Factory curation (the events that matter, correct ET
# times, majors only) enriched with FMP's ACTUAL values. FF is the whitelist;
# FMP supplies the released number where the event titles correspond. This is
# what keeps the page clean: only FF's curated set shows, but now with actuals.
# ----------------------------------------------------------------------------
_PERIOD_RE = re.compile(r"\s*\([^)]*\)\s*$")     # trailing "(May)", "(Q2)", "(May/29)"

# Canonicalize FF and FMP titles to a shared key so the two feeds correspond,
# bridging the handful of cases where they name the same release differently.
_TITLE_ALIASES = {
    "non farm employment change":       "nonfarm payrolls",
    "nonfarm employment change":        "nonfarm payrolls",
    "adp non farm employment change":   "adp employment change",
    "adp nonfarm employment change":    "adp employment change",
    "ism non manufacturing pmi":        "ism services pmi",
    "ism non manufacturing employment": "ism services employment",
    "ism non manufacturing prices":     "ism services prices",
    "unemployment claims":              "initial jobless claims",
    "jobless claims":                   "initial jobless claims",
    "average hourly earnings mom":      "average hourly earnings",
    "fomc statement":                   "fed interest rate decision",
    "federal funds rate":               "fed interest rate decision",
}

def _norm_title(t: str) -> str:
    t = (t or "").lower().strip()
    t = _PERIOD_RE.sub("", t)
    t = t.replace("m/m", "mom").replace("q/q", "qoq").replace("y/y", "yoy")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return re.sub(r"\s+", " ", t)

def _canon(t: str) -> str:
    n = _norm_title(t)
    if n in _TITLE_ALIASES:
        return _TITLE_ALIASES[n]
    # Concept-level canon for releases the two feeds name very differently.
    # Nonfarm payrolls is the worst offender: Forex Factory calls it
    # "Non-Farm Employment Change" while FMP calls it "Nonfarm Payrolls"
    # (plus variants like "Non Farm Payrolls" / "Total Nonfarm Payrolls").
    # After normalization those share ZERO tokens, so both the alias dict and
    # the token-overlap fallback miss them and the actual never attaches.
    # Collapse every nonfarm spelling to one key here. ADP's separate
    # private-payrolls print is routed to its own canon instead.
    has_nonfarm = ("nonfarm" in n) or ("non" in n and "farm" in n) or ("nfp" in n.split())
    if has_nonfarm and "private" not in n:
        return "adp employment change" if "adp" in n else "nonfarm payrolls"
    return n

def _ts(s):
    try:
        return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


def _merge() -> list:
    ff  = _cached("ff",  _forexfactory, TTL_FF)
    if not ff:
        return _cached("fmp", _fmp, TTL_FMP)   # no FF -> raw FMP (still has actuals)
    fmp = _cached("fmp", _fmp, TTL_FMP)
    if not fmp:
        return ff                              # no FMP -> FF as-is (no actuals)

    # index FMP rows by (canonical title, currency)
    idx = {}
    for e in fmp:
        idx.setdefault((_canon(e.get("title")), (e.get("country") or "").upper()), []).append(e)

    def _has_actual(c):
        a = c.get("actual") if c else None
        return a not in (None, "")

    def _pick(cands, ref_date):
        cands = [c for c in cands if c]            # drop any None from the fallback
        if not cands:
            return None
        # Prefer candidates that actually carry a released value, so a duplicate
        # or revision row with an empty actual can't shadow the real print
        # (this is what was eating the Non-Farm Payrolls actual).
        pool = [c for c in cands if _has_actual(c)] or cands
        if len(pool) == 1:
            return pool[0]
        ref = _ts(ref_date)
        if not ref:
            return pool[0]
        return min(pool, key=lambda c: abs(((_ts(c.get("date")) or ref) - ref).total_seconds()))

    matched = 0
    for e in ff:
        ccy   = (e.get("country") or "").upper()
        cands = idx.get((_canon(e.get("title")), ccy))
        if not cands:
            # token-overlap fallback within the same currency (handles small
            # wording diffs like "ISM Manufacturing Prices" vs "...Prices Paid")
            etoks = set(_norm_title(e.get("title")).split())
            best, best_ov = None, 0.0
            for fe in fmp:
                if (fe.get("country") or "").upper() != ccy:
                    continue
                ftoks = set(_norm_title(fe.get("title")).split())
                if not etoks or not ftoks:
                    continue
                ov = len(etoks & ftoks) / len(etoks | ftoks)
                if ov > best_ov:
                    best_ov, best = ov, fe
            cands = [best] if (best and best_ov >= 0.6) else None
        if not cands:
            continue
        m = _pick(cands, e.get("date"))
        if not m:
            continue
        act = m.get("actual")
        if act not in (None, ""):
            e["actual"] = str(act)
            matched += 1
    nfp = next((x for x in ff if _canon(x.get("title")) == "nonfarm payrolls"), None)
    if nfp is not None:
        print(f"  [calendar] merge: NFP '{nfp.get('title')}' actual -> "
              f"{nfp.get('actual') or '(none — FMP returned no actual for it at fetch time)'}")
    print(f"  [calendar] merge: {len(ff)} FF events · {matched} actuals matched from FMP")
    return ff


def get_calendar(force_refresh: bool = False) -> list:
    """This-week economic calendar as a normalized list of dicts.
    Cached per-process so the upstream feed is never hammered. On any error,
    returns the last good cache (or [] if we never had one)."""
    now = time.time()
    if (not force_refresh) and _cache["data"] and (now - _cache["ts"] < _cache["ttl"]):
        return _cache["data"]

    provider = CALENDAR_PROVIDER
    if provider == "auto":
        provider = "merge" if FMP_KEY else "forexfactory"

    data, ttl = [], TTL_FF
    try:
        if provider == "merge":
            data = _merge()
            ttl = TTL_MERGE
            if not data:                       # both feeds empty -> last resort
                data = _forexfactory()
                ttl = TTL_FF
        elif provider == "fmp":
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
    prov = CALENDAR_PROVIDER if CALENDAR_PROVIDER != "auto" else ("merge" if FMP_KEY else "forexfactory")
    print(f"Provider: {prov}  |  FMP_KEY set: {bool(FMP_KEY)}")
    events = get_calendar(force_refresh=True)
    print(f"Got {len(events)} events. First few:")
    for e in events[:12]:
        act = e.get("actual") or "—"
        print(f"  {e['date'][:16]:18} {e['country']:4} {e['impact']:7} act={act:>8}  {e['title'][:42]}")

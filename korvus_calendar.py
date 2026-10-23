#!/usr/bin/env python3
"""
==============================================================================
 KORVUS CALENDAR  ·  Forex / Gov News economic calendar   (by BlackCrownVxJ.LLC)
==============================================================================
 Server-side provider for the Forex / Gov News page (korvus_forex_calendar.html).
 Served at /api/ff-calendar (see korvus_server.py). The page fetches that
 endpoint and renders the week's government data releases.

 WHY THIS LIVES ON THE SERVER (not the browser):
   The free Forex Factory weekly JSON is CORS-blocked AND rate-limited - polled
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
                     actuals; the page shows "-" in the Actual column.
   "auto"         -> use "merge" if FMP_KEY is set, else Forex Factory. (default)

 ENTRY POINT:
   get_calendar() -> list[dict], each:
     { "title": str, "country": <CCY e.g. 'USD'>,
       "impact": "High"|"Medium"|"Low"|"Holiday",
       "date": <ISO 8601 string>,
       "forecast": str, "previous": str, "actual": str }
   Never raises - on any failure it returns the last good cache, or [].

 Quick manual test on the server:   python korvus_calendar.py
==============================================================================
"""
import os
import re
import json
import math
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
# Trading Economics: optional third actuals source. FMP does not redistribute
# S&P Global's licensed PMI, so those rows stay blank no matter how good the
# matching is. TE sources actuals from official releases and is permitted to
# carry PMI. Set TE_KEY in .env to switch it on; with no key this is a no-op and
# the calendar behaves exactly as before.
TE_KEY            = os.getenv("TE_KEY", "").strip()

FF_URL  = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# FMP moved the economic calendar to the /stable/ path; the old /api/v3/ route
# now returns 403 for newer keys. Try stable first, fall back to legacy v3.
FMP_URLS = [
    "https://financialmodelingprep.com/stable/economic-calendar",   # current
    "https://financialmodelingprep.com/api/v3/economic_calendar",   # legacy fallback
]
TE_URL  = "https://api.tradingeconomics.com/calendar"

UA = "Mozilla/5.0 (compatible; korvus-engine/0.2; +https://korvus.industries)"

# cache TTLs (seconds)
TTL_FF    = 60 * 60      # Forex Factory: refresh hourly (it's rate-limited)
TTL_FMP   = 5 * 60       # FMP: 5 minutes is plenty for actuals to land
TTL_TE    = 5 * 60       # Trading Economics: 5 minutes
TTL_MERGE = 5 * 60       # merge: recompute from the sub-caches every 5 min

# module-level cache: survives across requests within one server process
_cache = {"data": [], "ts": 0.0, "ttl": TTL_FF, "provider": ""}

# Disk-backed last-good cache so a server restart never shows a blank calendar
# while the first live fetch is in flight (or if the feed is rate-limited at that
# moment). Only reused when recent enough to still be "this week".
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_cache.json")
_DISK_MAX_AGE = 24 * 60 * 60     # don't reuse a saved calendar older than ~1 day

def _load_disk_cache():
    try:
        with open(_CACHE_FILE, "r") as f:
            d = json.load(f)
    except Exception:
        return
    data = d.get("data") if isinstance(d, dict) else None
    ts   = float(d.get("ts", 0) or 0) if isinstance(d, dict) else 0
    if data and (time.time() - ts) < _DISK_MAX_AGE:
        _cache.update({"data": data, "ts": ts,
                       "ttl": d.get("ttl", TTL_FF), "provider": d.get("provider", "")})
        print(f"  [calendar] restored {len(data)} events from disk cache "
              f"({int((time.time()-ts)/60)} min old)")

def _save_disk_cache():
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump({"data": _cache["data"], "ts": _cache["ts"],
                       "ttl": _cache["ttl"], "provider": _cache["provider"]}, f)
    except Exception as e:
        print(f"  [calendar] disk cache save failed: {e}")

# per-source sub-caches so merge mode can refresh FMP (actuals) every 5 min
# while only hitting the rate-limited Forex Factory feed hourly.
_sub = {"ff": {"data": None, "ts": 0.0}, "fmp": {"data": None, "ts": 0.0}, "te": {"data": None, "ts": 0.0}}

def _cached(kind, fn, ttl):
    now = time.time()
    # setdefault so a provider name that was never pre-registered in _sub creates
    # its own slot instead of throwing KeyError and taking down the whole merge.
    c = _sub.setdefault(kind, {"data": None, "ts": 0.0})
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
        print(f"  [calendar] Forex Factory returned {r.status_code} - skipping")
        return []
    raw = r.json()
    out = []
    n_actual = 0
    for e in raw:
        act = (e.get("actual") or "").strip()
        if act:
            n_actual += 1
        out.append({
            "title":    (e.get("title") or "").strip(),
            "country":  (e.get("country") or "").strip().upper(),  # FF already gives CCY
            "impact":   _norm_impact(e.get("impact")),
            "date":     e.get("date") or "",                       # ISO 8601 w/ offset
            "forecast": (e.get("forecast") or "").strip(),
            "previous": (e.get("previous") or "").strip(),
            "actual":   act,            # some FF weekly feeds DO carry released actuals
        })
    print(f"  [calendar] Forex Factory: {len(out)} events ({n_actual} with an actual)")
    return out


# Normalize FMP's "YYYY-MM-DD HH:MM:SS" (UTC, no offset) into an unambiguous
# ISO-8601 string so the browser converts it to ET correctly. Forex Factory
# already sends an offset, so its dates pass through untouched.
def _fmp_date_iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "T" in s:                       # already ISO-ish (has offset/Z) - leave it
        return s
    if " " in s:                       # "YYYY-MM-DD HH:MM:SS" in UTC
        return s.replace(" ", "T", 1) + "+00:00"
    return s                           # date-only (holidays / all-day)


# ----------------------------------------------------------------------------
# Provider: Financial Modeling Prep (licensed; includes ACTUAL values)
# ----------------------------------------------------------------------------
def _fmp() -> list:
    if not FMP_KEY:
        print("  [calendar] no FMP_KEY set - cannot use FMP")
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
            print(f"  [calendar] FMP {tag} request error: {e} - trying next")
            continue
        if r.status_code != 200:
            print(f"  [calendar] FMP {tag} returned {r.status_code} - trying next")
            continue
        try:
            j = r.json()
        except Exception:
            print(f"  [calendar] FMP {tag} returned non-JSON - trying next")
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
# Provider: Trading Economics (optional). Actuals from official releases; unlike
# FMP it is licensed to carry S&P Global PMI. Only used to fill actuals FMP/FF
# can't supply. Inactive unless TE_KEY is set.
# ----------------------------------------------------------------------------
_TE_COUNTRY_CCY = {
    "united states": "USD", "euro area": "EUR", "germany": "EUR", "france": "EUR",
    "italy": "EUR", "spain": "EUR", "netherlands": "EUR", "united kingdom": "GBP",
    "japan": "JPY", "china": "CNY", "canada": "CAD", "australia": "AUD",
    "new zealand": "NZD", "switzerland": "CHF",
}

def _te_clean(v) -> str:
    v = ("" if v is None else str(v)).strip()
    return "" if v.lower() in ("null", "none") else v

def _te_date_iso(s: str) -> str:
    # TE sends "YYYY-MM-DDTHH:MM:SS" in UTC with no offset; tag it so the browser
    # converts to ET correctly (same reasoning as the FMP normalizer).
    s = (s or "").strip()
    if not s:
        return ""
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if "T" in s:
        tail = s.split("T", 1)[1]
        if not (tail.endswith("Z") or "+" in tail or "-" in tail):
            return s + "+00:00"
    return s

def _te_impact(v) -> str:
    try:
        return {3: "High", 2: "Medium", 1: "Low"}.get(int(v), "Low")
    except Exception:
        return _norm_impact(v)

def _tradingeconomics() -> list:
    if not TE_KEY:
        return []
    today = dt.date.today()
    frm = today - dt.timedelta(days=today.weekday())     # Monday of this week
    to  = frm + dt.timedelta(days=6)                      # Sunday
    url = f"{TE_URL}/country/All/{frm.isoformat()}/{to.isoformat()}"
    try:
        r = requests.get(url, params={"c": TE_KEY, "f": "json"}, timeout=20,
                         headers={"User-Agent": UA})
    except Exception as e:
        print(f"  [calendar] Trading Economics request error: {e}")
        return []
    if r.status_code != 200:
        print(f"  [calendar] Trading Economics returned {r.status_code} "
              f"({(r.text or '')[:140]})")
        return []
    try:
        raw = r.json()
    except Exception:
        print("  [calendar] Trading Economics returned non-JSON")
        return []
    if not isinstance(raw, list):
        print(f"  [calendar] Trading Economics unexpected response: {str(raw)[:140]}")
        return []
    out = []
    for e in raw:
        cname = (e.get("Country") or "").strip().lower()
        ccy = _TE_COUNTRY_CCY.get(cname, "")
        out.append({
            "title":    (e.get("Event") or "").strip(),
            "country":  ccy,
            "impact":   _te_impact(e.get("Importance")),
            "date":     _te_date_iso(e.get("Date")),
            "forecast": _te_clean(e.get("Forecast")) or _te_clean(e.get("TEForecast")),
            "previous": _te_clean(e.get("Previous")),
            "actual":   _te_clean(e.get("Actual")),
        })
    n_actual = sum(1 for e in out if e["actual"])
    print(f"  [calendar] Trading Economics: {len(out)} events ({n_actual} with an actual)")
    return out


# ----------------------------------------------------------------------------
# Provider: MERGE - Forex Factory curation (the events that matter, correct ET
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
    # FF name -> the name FMP uses for the same release (verified from the feed)
    "richmond manufacturing index":     "richmond fed manufacturing index",
    "natural gas storage":              "eia natural gas stocks change",
    "core durable goods orders mom":    "durable goods orders ex transp mom",
    "crude oil inventories":            "eia crude oil stocks change",
}

def _norm_title(t: str) -> str:
    t = (t or "").lower().strip()
    t = _PERIOD_RE.sub("", t)
    t = t.replace("m/m", "mom").replace("q/q", "qoq").replace("y/y", "yoy")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return re.sub(r"\s+", " ", t)

# Vendor prefixes and flash/prelim qualifiers that ONE feed prints and the other
# omits, so the same release ends up with names that share too few tokens to
# match. Forex Factory says "Flash Manufacturing PMI"; FMP says "S&P Global
# Manufacturing PMI" - after normalization those share only {manufacturing, pmi}
# (2 of 6 tokens, 33% overlap) and the actual never attaches. Stripping these
# noise tokens collapses both to "manufacturing pmi" so they correspond.
# Deliberately CONSERVATIVE: distinguishing words (ism, services, manufacturing,
# composite, final, revised) are NOT in here, so genuinely different releases
# (ISM vs S&P Global, Flash vs Final) never collapse into one another.
_NOISE = {
    "s", "p", "sp", "global", "markit", "hcob", "ihs", "caixin", "jibun", "au",
    "flash", "prelim", "preliminary", "advance", "adv",
}

def _strip_noise(n: str) -> str:
    """Drop vendor/qualifier tokens from a normalized title. Never returns empty
    (if every token were noise, keep the original)."""
    toks = [t for t in n.split() if t not in _NOISE]
    return " ".join(toks) if toks else n

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
    # GDP is the other big naming split: Forex Factory prints "Final GDP q/q"
    # while FMP calls the same release "GDP Growth Rate QoQ" (with Advance /
    # Prelim / Final vintages). After normalization those share only {gdp, qoq}
    # (2 of up to 5 tokens -> 0.4 overlap, below the 0.6 fallback bar) so the
    # actual never attaches. Collapse every GDP-headline spelling to one key,
    # keyed on the period, while keeping the "GDP Price Index / Deflator" sibling
    # on its OWN key so the two never swap values. Advance/Prelim/Final are weeks
    # apart, so the same-occurrence date window keeps those vintages separate.
    toks = set(n.split())
    if "gdp" in toks:
        period = ("qoq" if "qoq" in toks else
                  "yoy" if "yoy" in toks else
                  "mom" if "mom" in toks else "")
        if period:
            is_price = ("price" in toks) or ("deflator" in toks)
            return f"gdp price index {period}" if is_price else f"gdp {period}"
    return _strip_noise(n)

def _ts(s):
    try:
        return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None


# --- actual-value sanity + unit normalization -------------------------------
# FMP returns actuals as bare numbers ("0.5", "229", "336.12") with no unit,
# while Forex Factory's forecast/previous carry the unit ("0.3%", "220K"). Two
# things were garbling the Actual column:
#   1) the unit was dropped, so "0.5" sat next to "0.3%" and "229" next to "220K".
#   2) on a loose title match, FMP's INDEX level (e.g. Core CPI = 336.12) got
#      attached to a y/y % event, printing nonsense like "336.12" beside "2.9%".
# Fix both: reject an actual whose magnitude is incompatible with the event's own
# forecast/previous (it's the wrong series), and append the event's unit to a
# good one so the column reads consistently.
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")

def _num(s):
    """Leading numeric value of a string like '0.3%', '220K', '1.4M', '336.12'."""
    if s is None:
        return None
    m = _NUM_RE.search(str(s).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group())
    except Exception:
        return None

def _unit(s):
    """Unit suffix of a value string: '%', 'K', 'M', 'B' or ''."""
    su = str(s or "").strip().upper()
    if "%" in su:        return "%"
    if su.endswith("K"): return "K"
    if su.endswith("M"): return "M"
    if su.endswith("B"): return "B"
    return ""

def _ref_unit(forecast, previous):
    return _unit(forecast) or _unit(previous)

# Multiplier each unit suffix stands for, so values from the two feeds can be
# compared on the SAME scale. Forex Factory writes "220K"; FMP may send the same
# figure as "220000" (full), "220" (in thousands), or "220K". Comparing the bare
# numbers (220000 vs 220) is what garbled the column and rejected good actuals.
_MULT = {"%": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}

def _num_scaled(s):
    """Numeric value of a string scaled by its own unit: '220K'->220000,
    '7.74M'->7740000, '0.3%'->0.3. None if there's no number."""
    n = _num(s)
    if n is None:
        return None
    return n * _MULT.get(_unit(s), 1.0)

def _best_scale(fa, ref_scaled, u):
    """Reconcile FMP giving a figure in full ('220000') vs abbreviated in FF's
    unit ('220'). ONLY a K/M/B event allows the 1000/1e6/1e9 reading; '%' and
    plain numbers are never rescaled, so a wrong-series value (an index 336 on a
    2.9% event) can't be shrunk to look compatible - it stays 336 and is rejected
    by the magnitude check."""
    mult = _MULT.get(u, 1.0)
    if ref_scaled is None or fa is None or abs(fa) < 1e-12 or mult == 1.0:
        return 1.0
    target = math.log10(abs(ref_scaled)) if abs(ref_scaled) > 1e-12 else 0.0
    return min({1.0, mult}, key=lambda s: abs(math.log10(abs(fa * s)) - target))

def _actual_scaled(act, forecast, previous):
    """FMP actual expressed on the FF reference's true scale, plus that reference.
    Returns (scaled_actual, ref_scaled) or (None, ref_scaled)."""
    fa = _num(act)
    ref = _num_scaled(forecast)
    if ref is None:
        ref = _num_scaled(previous)
    if fa is None:
        return None, ref
    return fa * _best_scale(fa, ref, _ref_unit(forecast, previous)), ref

def _fmt_actual(act, forecast, previous):
    """Display-ready actual in Forex Factory's own unit, or None to reject (wrong
    series / nonsense magnitude). The number is scaled to FF's reference first, so
    an FMP '197000' against FF's '199K' renders as '197K', not '197000K'."""
    scaled, ref = _actual_scaled(act, forecast, previous)
    if scaled is None:
        return None
    # magnitude sanity vs FF's own forecast/previous (now both on the same scale):
    # an index level (336) attached to a % event (2.9) is still rejected.
    if ref is not None and abs(ref) > 1e-9 and not (0.1 <= abs(scaled) / abs(ref) <= 10):
        return None
    u = _ref_unit(forecast, previous)
    disp = scaled / _MULT.get(u, 1.0)     # back into the display unit (K/M/B/%)
    txt = f"{disp:.4f}".rstrip("0").rstrip(".")
    if txt in ("", "-", "-0"):
        txt = "0"
    return txt + (u if u in ("%", "K", "M", "B") else "")


# --- cross-feed confirmation ------------------------------------------------
# Forex Factory is the source of truth for WHAT an event is and its forecast /
# previous. Before trusting FMP's released number, confirm FMP is really on the
# same release by checking its forecast/previous against FF's (unit-aware). A
# clash means FMP matched a different series - reject it so a wrong number never
# lands on a correct FF row.
def _vals_agree(ff_val, fmp_val, tol=0.05) -> bool:
    fref = _num_scaled(ff_val)
    fm   = _num(fmp_val)
    if fref is None or fm is None:
        return False
    if abs(fref) < 1e-9 and abs(fm) < 1e-9:
        return True
    scaled = fm * _best_scale(fm, fref, _unit(ff_val))
    if (fref < 0) != (scaled < 0) and not (abs(fref) < 1e-9 and abs(scaled) < 1e-9):
        return False                      # sign disagreement -> not the same value
    return abs(abs(scaled) - abs(fref)) / max(abs(fref), 1e-9) <= tol

def _corroborate(ff_e, fmp_row) -> str:
    """'confirm' if a FF/FMP forecast-or-previous pair agrees (same release),
    'contradict' if a settled previous value clashes, else 'unknown'."""
    f_prev, m_prev = ff_e.get("previous"), fmp_row.get("previous")
    f_fc,   m_fc   = ff_e.get("forecast"), fmp_row.get("forecast")
    prev_pair = _num(f_prev) is not None and _num(m_prev) is not None
    fc_pair   = _num(f_fc)   is not None and _num(m_fc)   is not None
    agree_prev = prev_pair and _vals_agree(f_prev, m_prev)
    agree_fc   = fc_pair   and _vals_agree(f_fc,   m_fc)
    if agree_prev or agree_fc:
        return "confirm"
    if prev_pair and not agree_prev:      # a known historical number disagrees
        return "contradict"
    return "unknown"


def _has_actual(c):
    a = c.get("actual") if c else None
    return a not in (None, "")


# --- future-event safeguard -------------------------------------------------
# An economic release has NO actual until it prints. If a row shows an actual on
# an event whose scheduled time is still ahead of us, that number was carried
# over from a PRIOR occurrence of the same release (a stale title match, or a
# week-boundary mismatch between the FF "this week" feed and FMP's Mon-Sun
# window). Suppress it. This is a reality check applied to every provider's
# output on every serve, so no matching logic can ever surface an actual for an
# event that has not happened yet.
_FUTURE_GRACE = dt.timedelta(minutes=2)   # tolerate tiny clock skew at release

def _is_future(date_str) -> bool:
    t = _ts(date_str)
    if not t:
        return False                      # undated / all-day -> never suppress
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t > (dt.datetime.now(dt.timezone.utc) + _FUTURE_GRACE)

def _scrub_future_actuals(events) -> list:
    """Blank the actual on any event still in the future. Idempotent, and cheap
    enough to run on every serve (fresh fetch AND cached), so a number never
    lingers past nothing and a stale cache can't leak one."""
    n = 0
    for e in events or []:
        if _has_actual(e) and _is_future(e.get("date")):
            e["actual"] = ""
            n += 1
    if n:
        print(f"  [calendar] scrubbed {n} future-dated actual(s) - not released yet")
    return events


def _pick(cands, ref_date):
    cands = [c for c in cands if c]            # drop any None from the fallback
    if not cands:
        return None
    # Prefer candidates that actually carry a released value, so a duplicate or
    # revision row with an empty actual can't shadow the real print (this is what
    # was eating the Non-Farm Payrolls actual).
    pool = [c for c in cands if _has_actual(c)] or cands
    if len(pool) == 1:
        return pool[0]
    ref = _ts(ref_date)
    if not ref:
        return pool[0]
    return min(pool, key=lambda c: abs(((_ts(c.get("date")) or ref) - ref).total_seconds()))

def _index_rows(rows):
    """Index a provider's rows by (canonical title, currency)."""
    idx = {}
    for e in rows:
        idx.setdefault((_canon(e.get("title")), (e.get("country") or "").upper()), []).append(e)
    return idx

# A matched provider actual must belong to the SAME release occurrence as the FF
# event, not a prior week's or month's print of the same series. Both feeds date
# a release on its real day, and the soonest a release repeats is weekly, so a
# window comfortably under 7 days cleanly rejects a stale carry-over while still
# tolerating a timezone/rounding gap on the genuine match.
_MATCH_WINDOW_S = 3 * 24 * 60 * 60        # 3 days

def _lookup_actual(e, rows, idx):
    """Display-ready actual for FF event `e` from one provider, or None. Tries an
    exact (canon, ccy) hit first, then a token-overlap fallback within the same
    currency. Identical matcher for every provider, so FMP and Trading Economics
    behave the same."""
    ccy   = (e.get("country") or "").upper()
    # An exact (canon, ccy) hit is high confidence. A loose token hit is not, so
    # it is only allowed through when the numbers confirm it (below).
    cands = idx.get((_canon(e.get("title")), ccy))
    exact = bool(cands)
    if not cands:
        etoks = set(_strip_noise(_norm_title(e.get("title"))).split())
        best, best_ov = None, 0.0
        for fe in rows:
            if (fe.get("country") or "").upper() != ccy:
                continue
            ftoks = set(_strip_noise(_norm_title(fe.get("title"))).split())
            if not etoks or not ftoks:
                continue
            ov = len(etoks & ftoks) / len(etoks | ftoks)
            if ov > best_ov:
                best_ov, best = ov, fe
        # Looser bar than before (0.6 -> 0.34): a near-name now gets a chance,
        # because the forecast/previous cross-check below is the real gate - a
        # similar name alone can never inject a value.
        cands = [best] if (best and best_ov >= 0.34) else None
    if not cands:
        return None
    m = _pick(cands, e.get("date"))
    if not m:
        return None
    # Same-occurrence guard: reject a matched row whose date sits too far from the
    # FF event's - that's a previous print of the same series, not this release.
    ref, md = _ts(e.get("date")), _ts(m.get("date"))
    if ref and md and abs((md - ref).total_seconds()) > _MATCH_WINDOW_S:
        return None
    act = m.get("actual")
    if act in (None, ""):
        return None
    # Cross-feed confirmation. FF's forecast/previous are authoritative; check
    # FMP's against them before trusting FMP's number.
    #   contradict -> FMP is on a different series: reject (keeps wrong data out).
    #   loose name  -> require a positive confirm.
    #   exact name  -> accept even when we simply can't corroborate.
    status = _corroborate(e, m)
    if status == "contradict":
        return None
    if status != "confirm" and not exact:
        return None
    return _fmt_actual(act, e.get("forecast"), e.get("previous"))   # None if nonsense


def _merge() -> list:
    ff  = _cached("ff",  _forexfactory, TTL_FF)
    fmp = _cached("fmp", _fmp, TTL_FMP) or []
    te  = _cached("te",  _tradingeconomics, TTL_TE) or []
    if not ff:
        # no FF curation -> serve whichever raw provider has data (still has actuals)
        return fmp or te
    if not fmp and not te:
        return ff                              # nothing to enrich with

    fmp_idx = _index_rows(fmp)
    te_idx  = _index_rows(te)

    m_fmp = m_te = 0
    for e in ff:
        if _has_actual(e):                     # FF's own feed already carried it
            continue
        a = _lookup_actual(e, fmp, fmp_idx)    # FMP first (movers, already proven)
        if a is not None:
            e["actual"] = a
            m_fmp += 1
            continue
        a = _lookup_actual(e, te, te_idx)      # then Trading Economics (PMI, gaps)
        if a is not None:
            e["actual"] = a
            m_te += 1

    nfp = next((x for x in ff if _canon(x.get("title")) == "nonfarm payrolls"), None)
    if nfp is not None:
        print(f"  [calendar] merge: NFP '{nfp.get('title')}' actual -> "
              f"{nfp.get('actual') or '(none at fetch time)'}")
    print(f"  [calendar] merge: {len(ff)} FF events · {m_fmp} from FMP · "
          f"{m_te} from Trading Economics")
    return ff


def get_calendar(force_refresh: bool = False) -> list:
    """This-week economic calendar as a normalized list of dicts.
    Cached per-process so the upstream feed is never hammered. On any error,
    returns the last good cache (or [] if we never had one)."""
    now = time.time()
    if (not force_refresh) and _cache["data"] and (now - _cache["ts"] < _cache["ttl"]):
        # Re-scrub on serve: as real time advances a cached row can age toward its
        # release, and (defensively) a disk cache written by an older build may
        # still carry a future actual. Idempotent - a real, past actual stays.
        return _scrub_future_actuals(_cache["data"])

    provider = CALENDAR_PROVIDER
    if provider == "auto":
        # Default to pure Forex Factory so the page matches forexfactory.com
        # field-for-field (title, time, impact, forecast, previous, actual all
        # come straight from FF's own feed). "merge"/"fmp" stay available via
        # CALENDAR_PROVIDER for faster actuals, but they can surface a value FF
        # has not posted yet (different revision, unit, or rounding), which is
        # exactly what reads as a mismatch on the page.
        provider = "forexfactory"

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
                print("  [calendar] FMP empty - falling back to Forex Factory")
                data = _forexfactory()
                ttl = TTL_FF
        else:
            data = _forexfactory()
            ttl = TTL_FF
    except Exception as e:
        print(f"  [calendar] fetch error: {e}")
        data = []

    if data:
        data = _scrub_future_actuals(data)  # never cache or serve an actual for
                                            # an event that hasn't printed yet
        _cache.update({"data": data, "ts": now, "ttl": ttl, "provider": provider})
        _save_disk_cache()                  # survive restarts with last-good data
        return data

    # fetch failed - serve whatever we had before rather than nothing
    return _scrub_future_actuals(_cache["data"])


# On import, seed the in-memory cache from disk so the very first request after a
# restart serves last-good data instead of a blank week while the feed is fetched.
_load_disk_cache()


if __name__ == "__main__":
    import sys
    prov = CALENDAR_PROVIDER if CALENDAR_PROVIDER != "auto" else (
        "merge" if (FMP_KEY or TE_KEY) else "forexfactory")

    # `python korvus_calendar.py te` -> validate a Trading Economics key BEFORE
    # paying: shows the HTTP status, how many USD rows came back, and whether the
    # S&P Global PMIs (the whole reason for adding TE) arrive WITH an actual.
    if len(sys.argv) > 1 and sys.argv[1].lower() == "te":
        if not TE_KEY:
            print("TE_KEY is not set. Add TE_KEY=your:key to .env (or export it) and re-run.")
            sys.exit(1)
        te = _tradingeconomics()
        te_usd = [e for e in te if (e.get("country") or "").upper() == "USD"]
        print(f"\nTrading Economics USD rows: {len(te_usd)}")
        for e in sorted(te_usd, key=lambda x: x.get("date", "")):
            print(f"  {e.get('date','')[:16]:18} {e.get('impact',''):7} "
                  f"act={str(e.get('actual') or '-'):>8}  {e.get('title','')[:44]}")
        pmi = [e for e in te_usd if "pmi" in (e.get("title") or "").lower()]
        print(f"\nUSD PMI rows from TE: {len(pmi)}  (this is the FMP gap we're filling)")
        for e in pmi:
            tag = "HAS ACTUAL" if e.get("actual") else "no actual yet"
            print(f"  [{tag}] {e.get('title','')}: act={e.get('actual') or '-'} "
                  f"fcst={e.get('forecast') or '-'} prev={e.get('previous') or '-'}")
        if pmi and any(e.get("actual") for e in pmi):
            print("\n=> Your TE plan returns USD PMI WITH actuals. Worth keeping.")
        elif pmi:
            print("\n=> TE lists the USD PMI but no actual right now (fine if it "
                  "hasn't released this week; re-check just after a PMI prints).")
        else:
            print("\n=> Your TE plan returned NO USD PMI rows. This tier does not "
                  "cover it - cancel the trial before it charges.")
        sys.exit(0)

    # `python korvus_calendar.py usd` -> show the USD side of the merge so a
    # missing actual is easy to trace: what FMP returns, what FF shows, and which
    # FF events couldn't find an FMP actual (and what their canon key resolved to).
    if len(sys.argv) > 1 and sys.argv[1].lower() == "usd":
        ff  = _cached("ff",  _forexfactory, TTL_FF) or []
        fmp = _cached("fmp", _fmp, TTL_FMP) or []
        fmp_usd = [e for e in fmp if (e.get("country") or "").upper() == "USD"]
        ff_usd  = [e for e in ff  if (e.get("country") or "").upper() == "USD"]
        print(f"\nFMP USD rows: {len(fmp_usd)}  (title -> canon -> actual)")
        for e in sorted(fmp_usd, key=lambda x: x.get("date", "")):
            print(f"  {e.get('date','')[:16]:18} act={str(e.get('actual') or '-'):>8}  "
                  f"{e.get('title','')[:38]:40} -> {_canon(e.get('title'))}")
        idx = {}
        for e in fmp_usd:
            idx.setdefault(_canon(e.get("title")), []).append(e)
        print(f"\nFF USD events: {len(ff_usd)}  (✓ = an FMP actual matched its canon)")
        for e in sorted(ff_usd, key=lambda x: x.get("date", "")):
            ck = _canon(e.get("title"))
            hit = any((c.get("actual") not in (None, "")) for c in idx.get(ck, []))
            mark = "✓" if ck in idx else ("·" if not hit else "✓")
            print(f"  {mark} {e.get('date','')[:16]:18} {e.get('title','')[:38]:40} -> {ck}")
        miss = [e for e in ff_usd if _canon(e.get("title")) not in idx]
        if miss:
            print(f"\nUnmatched FF USD events ({len(miss)}) - no FMP row shares their canon:")
            for e in miss:
                print(f"  {e.get('title','')}  -> {_canon(e.get('title'))}")
        sys.exit(0)

    print(f"Provider: {prov}  |  FMP_KEY: {bool(FMP_KEY)}  |  TE_KEY: {bool(TE_KEY)}")
    events = get_calendar(force_refresh=True)
    print(f"Got {len(events)} events. First few:")
    for e in events[:12]:
        act = e.get("actual") or "-"
        print(f"  {e['date'][:16]:18} {e['country']:4} {e['impact']:7} act={act:>8}  {e['title'][:42]}")

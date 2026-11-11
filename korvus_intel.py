#!/usr/bin/env python3
"""
==============================================================================
 KORVUS INTEL  ·  small-stock web crawler -> Obsidian vault   (BlackCrownVxJ LLC)
==============================================================================
 A SEPARATE ECOSYSTEM from the terminal. Own process (korvus-intel.service),
 own database (korvus_intel.db), own vault folder (intel/ in blackcrown-brain).
 Nothing here writes to korvus.db or posts to the terminal.

 WHAT IT DOES
   1. UNIVERSE   once a day: US common stocks trading near $100 (default
                 $80 to $120), liquid enough to trade, below a market-cap
                 ceiling so it stays on smaller names. Source: FMP screener.
   2. FILINGS    every 20s while EDGAR is open: 8-K / 6-K filings from those
                 companies, read straight from EDGAR (same parsers as the
                 terminal's Primary Source watcher), scored by Claude Haiku
                 for the STOCK itself (not index futures).
   3. SNAPSHOTS  price at the moment each filing is seen, plus every quote the
                 paper bot pulls, so a strategy has real history to test on.
   4. PAPER BOT  korvus_paperbot.py trades the catalysts on paper and keeps an
                 honest win-rate scoreboard. No real orders, ever.
   5. VAULT      korvus_vault.py renders all of it into intel/ in the
                 blackcrown-brain repo every 10 min; Obsidian Git pulls it to
                 the PC, and the BlackCrown agents can read it.

 CONFIG (.env, all optional except the keys the server already has)
   FMP_KEY, FINNHUB_KEY, ANTHROPIC_API_KEY   (already set for the terminal)
   INTEL_PRICE_MIN / INTEL_PRICE_MAX         default 80 / 120
   INTEL_MCAP_MAX                            default 10000000000 ($10B, "small")
   INTEL_MIN_VOLUME                          default 300000 shares/day
   INTEL_UNIVERSE_MAX                        default 400 names
   INTEL_EXTRA_TICKERS                       always include these (comma list)
   INTEL_POLL_SECONDS                        default 20
   VAULT_*                                   see korvus_vault.py

 CLI
   python korvus_intel.py --universe         refresh + print the universe
   python korvus_intel.py --dry-run          universe filings on the wire now (no AI, no DB)
   python korvus_intel.py --once             one filings poll + one bot tick
   python korvus_intel.py --loop             run everything forever (the service)
   python korvus_intel.py --stats            paper-bot scoreboard
==============================================================================
"""
import os
import re
import json
import time
import sqlite3
import argparse
import threading
import datetime as dt
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import korvus_edgar as edgar   # parsers + polite SEC session (shared rate limit)
import korvus_intel_status as status   # live heartbeat + activity log for the admin

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus_intel.db")


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key, default)
    v = (v or "").split(" #")[0].strip().strip('"').strip("'").strip()
    return v or default


def _num(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


FMP_KEY       = _env("FMP_KEY")
FINNHUB_KEY   = _env("FINNHUB_KEY")
PRICE_MIN     = _num("INTEL_PRICE_MIN", 80)
PRICE_MAX     = _num("INTEL_PRICE_MAX", 120)
MCAP_MAX      = _num("INTEL_MCAP_MAX", 10_000_000_000)
MIN_VOLUME    = _num("INTEL_MIN_VOLUME", 300_000)
UNIVERSE_MAX  = int(_num("INTEL_UNIVERSE_MAX", 400))
COUNTRY       = _env("INTEL_COUNTRY", "").upper()
EXTRA_TICKERS = [t.strip().upper() for t in _env("INTEL_EXTRA_TICKERS").split(",") if t.strip()]
POLL_SECONDS  = max(10, int(_num("INTEL_POLL_SECONDS", 20)))
CLAUDE_MODEL  = "claude-haiku-4-5"

# Which brain reads the filings:
#   haiku  (default) Claude Haiku decides.
#   crown  BlackCrown Intelligence's model (Crown, on OpenRouter) decides; if it is
#          down or answers in an unusable shape, Haiku steps in for that filing.
#   shadow Haiku decides (the bot trades on it) AND Crown reads every filing on
#          the side, so the two can be compared before Crown is trusted.
SCORER        = _env("INTEL_SCORER", "haiku").lower()
CROWN_BASE    = _env("CROWN_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
CROWN_KEY     = _env("CROWN_API_KEY") or _env("OPENROUTER_API_KEY")
CROWN_MODEL   = _env("CROWN_MODEL")              # the OpenRouter model id Crown runs on
CROWN_TIMEOUT = int(_num("CROWN_TIMEOUT", 120))

SCREENER_URLS = [
    "https://financialmodelingprep.com/stable/company-screener",   # current
    "https://financialmodelingprep.com/api/v3/stock-screener",     # legacy fallback
]


# ----------------------------------------------------------------------------
# DATABASE (separate from korvus.db on purpose)
# ----------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS universe (
            symbol   TEXT PRIMARY KEY,
            name     TEXT, cik INTEGER, price REAL, mcap REAL, volume REAL,
            sector   TEXT, industry TEXT, exchange TEXT, asof TEXT
        );
        CREATE TABLE IF NOT EXISTS filings (
            accession  TEXT PRIMARY KEY,
            symbol     TEXT NOT NULL, cik INTEGER, company TEXT, form TEXT,
            items      TEXT,              -- JSON list of 8-K item codes
            filed_utc  TEXT,              -- SEC acceptance time
            seen_utc   TEXT NOT NULL,     -- when Korvus Intel had it
            url        TEXT, headline TEXT, excerpt TEXT,
            ref_price  REAL,              -- last price when seen (pre-reaction anchor)
            prev_close REAL,
            summary    TEXT, read TEXT, catalyst TEXT,
            impact     TEXT, direction TEXT, confidence INTEGER,
            scored     INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_filings_seen ON filings(seen_utc);
        CREATE INDEX IF NOT EXISTS idx_filings_sym ON filings(symbol, seen_utc);
        CREATE TABLE IF NOT EXISTS snapshots (
            symbol TEXT NOT NULL, ts_utc TEXT NOT NULL,
            price REAL, chg_pct REAL, open REAL, high REAL, low REAL, prev_close REAL,
            PRIMARY KEY (symbol, ts_utc)
        );
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy    TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            accession   TEXT,             -- the filing that triggered it
            side        TEXT DEFAULT 'long',  -- long | short
            qty         INTEGER NOT NULL,
            entry_utc   TEXT NOT NULL, entry_px REAL NOT NULL,
            target_px   REAL, stop_px REAL,
            exit_utc    TEXT, exit_px REAL, exit_reason TEXT,
            pnl         REAL, pnl_pct REAL,
            status      TEXT NOT NULL DEFAULT 'open'
        );
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "side" not in tcols:
        conn.execute("ALTER TABLE trades ADD COLUMN side TEXT DEFAULT 'long'")   # long | short
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filings)")}
    if "scorer" not in cols:
        conn.execute("ALTER TABLE filings ADD COLUMN scorer TEXT")        # who decided: haiku | crown
    if "shadow_json" not in cols:
        conn.execute("ALTER TABLE filings ADD COLUMN shadow_json TEXT")   # Crown's read in shadow mode
    conn.commit()
    conn.close()


def meta_get(k: str, default: str = "") -> str:
    conn = db()
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    conn.close()
    return r["v"] if r else default


def meta_set(k: str, v: str):
    conn = db()
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    conn.commit()
    conn.close()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def et_now() -> dt.datetime:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York"))


# ----------------------------------------------------------------------------
# 1) UNIVERSE: stocks near $100 (FMP screener) mapped to SEC CIKs
# ----------------------------------------------------------------------------
def parse_screener(rows: list) -> list:
    """FMP screener rows -> clean universe rows. Drops ETFs/funds, keeps the
    price band, and is tolerant of the stable vs legacy field names."""
    out = []
    for r in rows or []:
        sym = (r.get("symbol") or "").upper().strip()
        if not sym or "." in sym or "-" in sym and not sym.endswith(("-A", "-B")):
            continue
        if r.get("isEtf") or r.get("isFund"):
            continue
        price = float(r.get("price") or 0)
        if not (PRICE_MIN <= price <= PRICE_MAX):
            continue
        out.append({
            "symbol": sym,
            "name": r.get("companyName") or r.get("name") or "",
            "price": price,
            "mcap": float(r.get("marketCap") or 0),
            "volume": float(r.get("volume") or 0),
            "sector": r.get("sector") or "",
            "industry": r.get("industry") or "",
            "exchange": r.get("exchangeShortName") or r.get("exchange") or "",
        })
    out.sort(key=lambda x: -x["volume"])
    return out[:UNIVERSE_MAX]


def fetch_screener() -> list:
    if not FMP_KEY:
        print("  [intel] no FMP_KEY, cannot screen the universe")
        return []
    params = {
        "priceMoreThan": PRICE_MIN, "priceLowerThan": PRICE_MAX,
        "marketCapLowerThan": int(MCAP_MAX), "volumeMoreThan": int(MIN_VOLUME),
        "isEtf": "false", "isFund": "false", "isActivelyTrading": "true",
        "exchange": "NASDAQ,NYSE,AMEX", "limit": 2000, "apikey": FMP_KEY,
    }
    # INTEL_COUNTRY="" (default) takes every US-listed company in the band,
    # including foreign companies listed here (they file 6-K / 20-F with the
    # SEC). INTEL_COUNTRY=US restores US companies only.
    if COUNTRY:
        params["country"] = COUNTRY
    for url in SCREENER_URLS:
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code != 200:
                print(f"  [intel] screener {url.split('/')[-1]} HTTP {r.status_code}")
                continue
            data = r.json()
            if isinstance(data, list) and data:
                return data
        except Exception as e:
            print(f"  [intel] screener error: {e}")
    return []


def refresh_universe(verbose: bool = True) -> int:
    rows = parse_screener(fetch_screener())
    have = {r["symbol"] for r in rows}
    sec_map = {}
    try:
        edgar.load_watchlist()      # refreshes the cached SEC ticker map on disk
        with open(edgar.TICKER_MAP_CACHE) as f:
            sec_map = json.load(f)
    except Exception as e:
        print(f"  [intel] SEC ticker map unavailable: {e}")
    for t in EXTRA_TICKERS:
        if t not in have:
            rows.append({"symbol": t, "name": "", "price": 0, "mcap": 0, "volume": 0,
                         "sector": "", "industry": "", "exchange": "extra"})
    if not rows:
        print("  [intel] universe refresh returned nothing; keeping the previous list")
        status.error("universe", "screener returned nothing; kept the previous list")
        return 0
    asof = utcnow().isoformat()
    conn = db()
    conn.execute("DELETE FROM universe")
    n = 0
    for r in rows:
        cik = sec_map.get(r["symbol"]) or sec_map.get(r["symbol"].replace(".", "-"))
        if not cik:
            continue          # no SEC filer, nothing for the crawler to read
        conn.execute("""INSERT OR REPLACE INTO universe
            (symbol,name,cik,price,mcap,volume,sector,industry,exchange,asof)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (r["symbol"], r["name"], int(cik), r["price"], r["mcap"], r["volume"],
             r["sector"], r["industry"], r["exchange"], asof))
        n += 1
    conn.commit()
    conn.close()
    meta_set("universe_asof", asof)
    status.record("universe", last_ok_utc=asof, count=n,
                  band=[PRICE_MIN, PRICE_MAX], mcap_max=MCAP_MAX)
    status.event("universe", f"screened {n} stocks between ${PRICE_MIN:.0f} and ${PRICE_MAX:.0f}")
    if verbose:
        print(f"  [intel] universe: {n} stocks between ${PRICE_MIN:.0f} and ${PRICE_MAX:.0f}")
    return n


def universe_by_cik() -> dict:
    conn = db()
    rows = conn.execute("SELECT symbol, cik FROM universe").fetchall()
    conn.close()
    return {int(r["cik"]): r["symbol"] for r in rows if r["cik"]}


# ----------------------------------------------------------------------------
# QUOTES (Finnhub, already keyed on the server)
# ----------------------------------------------------------------------------
_q_lock = threading.Lock()
_q_last = [0.0]


def _regular_hours() -> bool:
    n = et_now()
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)


def extended_trade(symbol: str) -> Optional[tuple]:
    """Latest pre-market / after-hours / overnight trade from FMP as
    (price, unix_seconds), or None. Used only outside the regular session."""
    if not FMP_KEY:
        return None
    try:
        r = requests.get("https://financialmodelingprep.com/stable/aftermarket-trade",
                         params={"symbol": symbol, "apikey": FMP_KEY}, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        d = d[0] if isinstance(d, list) and d else d
        price, ts = float(d.get("price") or 0), float(d.get("timestamp") or 0)
        if price <= 0 or ts <= 0:
            return None
        return price, (ts / 1000.0 if ts > 1e11 else ts)
    except Exception:
        return None


def apply_extended(snap: dict, ext: Optional[tuple]) -> dict:
    """Use the extended-hours trade when it is newer than the regular quote."""
    if not ext:
        return snap
    price, ts = ext
    if ts <= (snap.get("ts") or 0):
        return snap
    out = dict(snap, price=price, ts=ts)
    pc = snap.get("prev_close") or 0
    if pc:
        out["chg_pct"] = round((price / pc - 1) * 100, 4)
    return out


def quote(symbol: str) -> Optional[dict]:
    """One Finnhub quote, spaced to stay under the free 60/min limit."""
    if not FINNHUB_KEY:
        return None
    with _q_lock:
        wait = 1.1 - (time.time() - _q_last[0])
        if wait > 0:
            time.sleep(wait)
        _q_last[0] = time.time()
    try:
        r = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": symbol, "token": FINNHUB_KEY}, timeout=8)
        q = r.json()
        if not q.get("c"):
            status.record("quotes", inc_empty_today=1)
            return None
        status.record("quotes", last_ok_utc=utcnow().isoformat(), last_symbol=symbol, inc_calls_today=1)
        snap = {"price": float(q["c"]), "chg_pct": float(q.get("dp") or 0),
                "open": float(q.get("o") or 0), "high": float(q.get("h") or 0),
                "low": float(q.get("l") or 0), "prev_close": float(q.get("pc") or 0),
                "ts": float(q.get("t") or 0) or None}      # when the last trade printed
        if not _regular_hours():
            snap = apply_extended(snap, extended_trade(symbol))
        save_snapshot(symbol, snap)
        return snap
    except Exception as e:
        print(f"  [intel] quote error {symbol}: {e}")
        status.error("quotes", f"{symbol}: {e}")
        return None


def save_snapshot(symbol: str, s: dict):
    conn = db()
    conn.execute("""INSERT OR IGNORE INTO snapshots
        (symbol, ts_utc, price, chg_pct, open, high, low, prev_close) VALUES (?,?,?,?,?,?,?,?)""",
        (symbol, utcnow().isoformat(), s["price"], s["chg_pct"], s["open"], s["high"],
         s["low"], s["prev_close"]))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# 2) FILINGS: universe 8-K / 6-K straight from EDGAR, scored for the stock
# ----------------------------------------------------------------------------
STOCK_PROMPT = """You are the equity analyst behind Korvus Intel. You read a company's \
SEC filing the moment it is accepted and judge its likely effect on THAT company's \
stock over the next session. Return STRICT JSON only:
{
  "summary": "<=2 sentences: what the filing says, with the real figures>",
  "read": "<2-4 sentences: why it matters for this stock, the mechanism, the base rate for this kind of filing, and what would confirm or invalidate the read>",
  "catalyst": "earnings" | "guidance" | "m_and_a" | "offering" | "buyback" | "dividend" | "exec_change" | "contract" | "legal" | "debt" | "restructuring" | "other",
  "impact": "high" | "med" | "low",
  "direction": "bull" | "bear" | "neut",
  "confidence": 0-100
}
Rules: you do not know analyst consensus, so judge earnings from growth, margins, guidance \
and tone and say the street comparison is unknown. Equity offerings and convertible notes \
are usually dilutive (bear). Routine items (vote results, bylaw edits, routine officer \
appointments) are low and neut. Never invent a number the filing does not contain. \
Plain prose, no em-dashes, no markdown. This is analysis, never advice."""


_client = [None]


def _brain():
    if _client[0] is None:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        import anthropic
        _client[0] = anthropic.Anthropic(api_key=key)
    return _client[0]


def _first_json(text: str) -> Optional[dict]:
    s = (text or "").find("{")
    if s < 0:
        return None
    depth, ins, esc = 0, False, False
    for i in range(s, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
        elif c == '"':
            ins = not ins
        elif not ins and c == "{":
            depth += 1
        elif not ins and c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[s:i + 1], strict=False)
                except ValueError:
                    return None
    return None


def normalize_score(d: dict) -> dict:
    em = chr(0x2014)
    def plain(t):
        return (t or "").replace(" " + em + " ", ", ").replace(em, ", ").strip()
    cats = {"earnings", "guidance", "m_and_a", "offering", "buyback", "dividend", "exec_change",
            "contract", "legal", "debt", "restructuring", "other"}
    try:
        conf = max(0, min(100, int(float(d.get("confidence") or 0))))
    except (TypeError, ValueError):
        conf = 0
    return {
        "summary": plain(d.get("summary")),
        "read": plain(d.get("read")),
        "catalyst": d.get("catalyst") if d.get("catalyst") in cats else "other",
        "impact": d.get("impact") if d.get("impact") in ("high", "med", "low") else "low",
        "direction": d.get("direction") if d.get("direction") in ("bull", "bear", "neut") else "neut",
        "confidence": conf,
    }


def _filing_blob(symbol, company, form, items, text, limit=12000) -> str:
    return (f"TICKER: {symbol}\nCOMPANY: {company}\nFORM: {form}\n"
            f"ITEMS: {', '.join(items) or 'n/a'} ({edgar.describe_items(items) or 'n/a'})\n\n"
            f"FILING TEXT:\n{text[:limit]}")


def crown_ready() -> bool:
    return bool(CROWN_KEY and CROWN_MODEL)


def score_with_crown(symbol: str, company: str, form: str, items: list, text: str) -> Optional[dict]:
    """Ask Crown's model on OpenRouter (OpenAI-compatible chat completions) for
    the same JSON read Haiku gives. Reasoning models may wrap their thinking
    in <think> tags; those are stripped before the JSON is read."""
    if not crown_ready():
        return None
    import re as _re
    t0 = time.time()
    try:
        r = requests.post(f"{CROWN_BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {CROWN_KEY}", "Content-Type": "application/json",
                                   "HTTP-Referer": "https://korvus.industries", "X-Title": "Korvus Intel"},
                          json={"model": CROWN_MODEL, "max_tokens": 1200, "temperature": 0.2,
                                "messages": [
                                    {"role": "system", "content": STOCK_PROMPT
                                     + "\nReturn ONLY the JSON object, nothing before or after it."},
                                    {"role": "user", "content": _filing_blob(symbol, company, form, items, text, 8000)},
                                ]},
                          timeout=CROWN_TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or body.get("error"):
            err = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
            status.error("crown", f"HTTP {r.status_code} from OpenRouter{': ' + str(err)[:140] if err else ''}")
            return None
        out = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        out = _re.sub(r"(?is)<think>.*?</think>", "", out)
        d = _first_json(out)
        if not d:
            status.error("crown", f"{symbol}: {CROWN_MODEL} answered without usable JSON")
            return None
        status.record("crown", last_ok_utc=utcnow().isoformat(), last_ms=ms, model=CROWN_MODEL, inc_reads_today=1)
        return normalize_score(d)
    except Exception as e:
        status.error("crown", f"{symbol}: {e.__class__.__name__}: {str(e)[:160]}")
        return None


def score_haiku(symbol: str, company: str, form: str, items: list, text: str) -> Optional[dict]:
    client = _brain()
    if client is None:
        return None
    blob = _filing_blob(symbol, company, form, items, text)
    for attempt in (1, 2):
        try:
            resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=900, system=STOCK_PROMPT,
                                          messages=[{"role": "user", "content": blob}])
            out = "".join(b.text for b in resp.content if b.type == "text")
            d = _first_json(out)
            if d:
                return normalize_score(d)
        except Exception as e:
            print(f"  [intel] scoring error ({attempt}): {e}")
            status.error("scoring", f"{symbol}: {e}")
            time.sleep(4)
    return None


def score_filing(symbol: str, company: str, form: str, items: list, text: str) -> Optional[dict]:
    """Route by INTEL_SCORER. Returns the deciding read with 'scorer' set, and
    in shadow mode Crown's read under 'shadow'."""
    mode = SCORER if SCORER in ("haiku", "crown", "shadow") else "haiku"
    if mode == "crown" and crown_ready():
        d = score_with_crown(symbol, company, form, items, text)
        if d:
            return {**d, "scorer": "crown"}
        status.event("crown", f"{symbol}: Crown unavailable, Haiku read it instead", level="error")
    d = score_haiku(symbol, company, form, items, text)
    if d is None:
        return None
    d["scorer"] = "haiku"
    if mode == "shadow" and crown_ready():
        sh = score_with_crown(symbol, company, form, items, text)
        if sh:
            d["shadow"] = sh
            agree = sh["direction"] == d["direction"] and sh["impact"] == d["impact"]
            status.record("crown", inc_agree_today=1 if agree else 0, inc_compared_today=1)
            status.event("crown", f"{symbol} shadow read {sh['impact'].upper()} {sh['direction']} "
                         f"{sh['confidence']}% ({'agrees' if agree else 'differs'} with Haiku)", symbol=symbol)
    return d


def _seen(accession: str) -> bool:
    conn = db()
    r = conn.execute("SELECT 1 FROM filings WHERE accession=?", (accession,)).fetchone()
    conn.close()
    return r is not None


def handle_filing(e: dict, symbol: str, verbose: bool = True) -> Optional[str]:
    idx = edgar.parse_index(edgar._get(e["url"]) or "")
    doc = edgar.pick_document(idx["docs"], e["form"])
    text = edgar.html_to_text(edgar._get(doc["url"]) or "") if doc else ""
    if not text:
        text = f"{e['company']} filed Form {e['form']}. {edgar.describe_items(e.get('items') or [])}"
    q = quote(symbol)
    filed = edgar.accepted_et_to_utc(idx.get("accepted", "")) or edgar.to_utc_iso(e.get("updated", ""))
    headline = edgar.pick_headline(text, symbol, e["company"], e.get("items") or [], e["form"])
    conn = db()
    conn.execute("""INSERT OR IGNORE INTO filings
        (accession,symbol,cik,company,form,items,filed_utc,seen_utc,url,headline,excerpt,ref_price,prev_close)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (e["accession"], symbol, e["cik"], e["company"], e["form"], json.dumps(e.get("items") or []),
         filed, utcnow().isoformat(), e["url"], headline, text[:4000],
         q["price"] if q else None, q["prev_close"] if q else None))
    conn.commit()
    conn.close()
    status.event("edgar", f"{symbol} filed {e['form']}: {headline[:110]}", symbol=symbol)
    s = score_filing(symbol, e["company"], e["form"], e.get("items") or [], text)
    if s:
        conn = db()
        conn.execute("""UPDATE filings SET summary=?, read=?, catalyst=?, impact=?, direction=?,
                        confidence=?, scored=1, scorer=?, shadow_json=? WHERE accession=?""",
                     (s["summary"], s["read"], s["catalyst"], s["impact"], s["direction"],
                      s["confidence"], s.get("scorer", "haiku"),
                      json.dumps(s["shadow"]) if s.get("shadow") else None, e["accession"]))
        conn.commit()
        conn.close()
        status.record("scoring", last_ok_utc=utcnow().isoformat(), last_symbol=symbol,
                      last_scorer=s.get("scorer", "haiku"),
                      last_result=f"{s['impact']} {s['direction']} {s['confidence']}% {s['catalyst']}",
                      inc_scored_today=1)
        status.event("scoring", f"{symbol} scored by {s.get('scorer', 'haiku').title()}: "
                     f"{s['impact'].upper()} {s['direction']} "
                     f"{s['confidence']}% ({s['catalyst'].replace('_', ' ')})",
                     symbol=symbol, impact=s["impact"], direction=s["direction"])
        if verbose:
            print(f"    ✓ [{s['impact']:>4}|{s['direction']:>4}|{s['confidence']:>3}%] {symbol} "
                  f"{s['catalyst']}: {headline[:60]}")
    return e["accession"]


def poll_filings(verbose: bool = True) -> int:
    watch = universe_by_cik()
    if not watch:
        status.error("edgar", "universe is empty, nothing to watch")
        return 0
    n = scanned = matched = 0
    t0 = time.time()
    fetched_ok = 0
    for form in edgar.FORMS:
        raw = edgar._get(edgar.FEED_URL.format(form=form))
        if raw:
            fetched_ok += 1
        entries = edgar.parse_feed(raw or "")
        scanned += len(entries)
        for e in entries:
            sym = watch.get(e["cik"])
            if sym:
                matched += 1
            if not sym or not e["accession"] or _seen(e["accession"]):
                continue
            if not edgar.worth_it(e) or not edgar._fresh(e):
                # remember it so we don't re-check, but don't spend a score on it
                conn = db()
                conn.execute("""INSERT OR IGNORE INTO filings
                    (accession,symbol,cik,company,form,items,filed_utc,seen_utc,url,headline,scored,impact,direction,catalyst)
                    VALUES (?,?,?,?,?,?,?,?,?,?,1,'low','neut','other')""",
                    (e["accession"], sym, e["cik"], e["company"], e["form"], json.dumps(e.get("items") or []),
                     edgar.to_utc_iso(e.get("updated", "")), utcnow().isoformat(), e["url"],
                     f"{sym} files {e['form']}: {edgar.describe_items(e.get('items') or []) or 'routine'}"))
                conn.commit()
                conn.close()
                continue
            try:
                handle_filing(e, sym, verbose=verbose)
                n += 1
            except Exception as ex:
                print(f"  [intel] filing error {sym}: {ex}")
                status.error("edgar", f"{sym}: {ex}")
    is_open = edgar._edgar_open_now()
    interval = POLL_SECONDS if is_open else 300
    now = utcnow()
    status.record("edgar", last_poll_utc=now.isoformat(),
                  next_poll_utc=(now + dt.timedelta(seconds=interval)).isoformat(),
                  interval_s=interval, open=is_open, watching=len(watch),
                  last_scanned=scanned, last_new=n, feeds_ok=fetched_ok, feeds=len(edgar.FORMS),
                  last_duration_ms=int((time.time() - t0) * 1000),
                  inc_polls_today=1, inc_scanned_today=scanned, inc_new_today=n)
    if fetched_ok < len(edgar.FORMS):
        status.error("edgar", f"only {fetched_ok} of {len(edgar.FORMS)} EDGAR feeds answered")
    if n:
        status.record("edgar", last_match_utc=now.isoformat())
    return n


# ----------------------------------------------------------------------------
# LOOP
# ----------------------------------------------------------------------------
def _universe_stale() -> bool:
    asof = meta_get("universe_asof")
    if not asof:
        return True
    try:
        return utcnow() - dt.datetime.fromisoformat(asof) > dt.timedelta(hours=20)
    except ValueError:
        return True


def _every(seconds_fn, fn, name):
    def run():
        while True:
            try:
                fn()
            except Exception as e:
                print(f"  [intel:{name}] error: {e}")
                status.error({"filings": "edgar", "paperbot": "bot"}.get(name, name), e)
            time.sleep(seconds_fn())
    t = threading.Thread(target=run, name=f"intel-{name}", daemon=True)
    t.start()
    return t


def run_forever():
    import korvus_paperbot as bot
    import korvus_vault as vault
    db_init()
    # This crawler's slice of the SEC's 10 requests a second. Korvus Compass and
    # the terminal watcher hold the rest (4 + 4 + 2).
    edgar.MIN_GAP = 1.0 / max(0.5, min(6.0, _num("INTEL_SEC_RPS", 4)))
    status.start_flusher()
    status.record("scoring", mode=SCORER, crown_configured=crown_ready(), crown_model=CROWN_MODEL)
    status.event("system", f"Korvus Intel started (scorer: {SCORER}"
                 f"{', Crown linked' if crown_ready() else ''})")
    if _universe_stale():
        refresh_universe()
    else:
        status.record("universe", last_ok_utc=meta_get("universe_asof"), count=len(universe_by_cik()),
                      band=[PRICE_MIN, PRICE_MAX], mcap_max=MCAP_MAX)

    def bot_job():
        out = bot.tick()
        now = utcnow()
        st = {"last_tick_utc": now.isoformat(), "interval_s": bot.TICK_SECONDS,
              "session_open": out.get("session_open"), "in_window": out.get("in_window"),
              "phase": out.get("phase"), "session_mode": bot.SESSION,
              "candidates": out.get("candidates", 0), "open_positions": out.get("open_positions", 0),
              "inc_entries_today": out["entries"], "inc_exits_today": out["exits"]}
        if out.get("skipped"):
            st["last_skip"] = f"{out['skipped'][-1][0]}: {out['skipped'][-1][1]}"
        status.record("bot", **st)
        for kind, sym, text in out.get("log", []):
            status.event("bot", text, symbol=sym, kind=kind)

    def vault_job():
        try:
            r = vault.sync()
            status.record("vault", last_sync_utc=utcnow().isoformat(), interval_s=vault.SYNC_MINUTES * 60,
                          last_changed=r.get("changed", 0))
            if r.get("pushed"):
                status.record("vault", last_push_utc=utcnow().isoformat(), inc_pushes_today=1)
                status.event("vault", f"pushed {r['changed']} note(s) to {vault.SUBDIR}/ in blackcrown-brain")
        except Exception as e:
            status.error("vault", e)
            print(f"  [intel:vault] error: {e}")

    def universe_job():
        n = et_now()
        if n.weekday() < 5 and n.hour == 9 and n.minute < 15 and _universe_stale():
            refresh_universe()

    _every(lambda: 300, universe_job, "universe")
    _every(lambda: POLL_SECONDS if edgar._edgar_open_now() else 300, poll_filings, "filings")
    _every(lambda: bot.TICK_SECONDS, bot_job, "paperbot")
    _every(lambda: vault.SYNC_MINUTES * 60, vault_job, "vault")
    print(f"  [intel] running: universe {len(universe_by_cik())} names, filings every {POLL_SECONDS}s, "
          f"paper bot every {bot.TICK_SECONDS}s, vault every {vault.SYNC_MINUTES} min")
    while True:
        time.sleep(3600)


def main():
    ap = argparse.ArgumentParser(description="Korvus Intel: small-stock crawler + paper bot + vault")
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--crown-test", action="store_true", help="send one sample filing to Crown and Haiku, compare")
    a = ap.parse_args()
    db_init()
    if a.universe:
        refresh_universe()
        conn = db()
        for r in conn.execute("SELECT * FROM universe ORDER BY volume DESC LIMIT 40"):
            print(f"  {r['symbol']:<6} ${r['price']:>7.2f}  mcap ${r['mcap']/1e9:5.1f}B  {r['sector']}")
        conn.close()
        return
    if a.dry_run:
        if _universe_stale():
            refresh_universe()
        watch = universe_by_cik()
        hits = []
        for form in edgar.FORMS:
            for e in edgar.parse_feed(edgar._get(edgar.FEED_URL.format(form=form)) or ""):
                if e["cik"] in watch:
                    hits.append((e["updated"][:19], watch[e["cik"]], e["form"], edgar.describe_items(e["items"])))
        print(f"  [intel] universe {len(watch)} names; {len(hits)} of their filings in the latest EDGAR feed")
        for h in hits[:40]:
            print("   ", *h)
        return
    if a.crown_test:
        sample = ("NVIDIA Announces Financial Results for Second Quarter Fiscal 2027\n"
                  "Revenue of $96.2 billion, up 106% from a year ago\n"
                  "Data Center revenue of $89.0 billion, up 117% from a year ago\n"
                  "GAAP and non-GAAP gross margins were both 75.0%.\n"
                  "Outlook: revenue is expected to be $108.0 billion, plus or minus 2%.")
        print(f"  Crown: {CROWN_MODEL or '<CROWN_MODEL not set>'} via {CROWN_BASE}")
        if not crown_ready():
            print("  set OPENROUTER_API_KEY (or CROWN_API_KEY) and CROWN_MODEL in .env first")
            return
        for name, fn in (("crown", score_with_crown), ("haiku", score_haiku)):
            t0 = time.time()
            r = fn("NVDA", "NVIDIA CORP", "8-K", ["2.02", "9.01"], sample)
            print(f"\n  {name.upper()} ({time.time() - t0:.1f}s):")
            print(json.dumps(r, indent=2) if r else "  no usable answer: "
                  + str(status.snapshot()["stages"].get("crown", {}).get("last_error", "")))
        return
    if a.stats:
        import korvus_paperbot as bot
        print(json.dumps(bot.scoreboard(), indent=2))
        return
    if a.loop:
        run_forever()
        return
    import korvus_paperbot as bot
    if _universe_stale():
        refresh_universe()
    print(f"  [intel] new filings handled: {poll_filings()}")
    bot.tick()


if __name__ == "__main__":
    main()

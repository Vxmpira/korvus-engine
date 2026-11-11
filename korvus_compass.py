"""
==============================================================================
 KORVUS COMPASS  (the long-term investing board, Operator feature)
==============================================================================
 WHAT IT IS
   A crawler that walks the S&P 500 + Nasdaq-100 plus the largest non-US
   companies listed in the US (about 650 names in all) and keeps a
   research file on every stock for people who buy and hold (Robinhood, Cash
   App, any brokerage): long-term price trend, what the company actually earns
   according to its own SEC filings, Wall Street's consensus, recent Korvus
   reads, and one transparent Compass score that ranks the board.

   It is research, never a buy call. Every number shows where it came from and
   the score shows its own math, so a member can disagree with it.

 DATA (all on keys the server already has)
   Universe      FMP index constituents, then Wikipedia, then the last good
                 copy on disk, then the SEC watcher's base list.
   Prices        FMP end-of-day history (FMP_KEY), once per trading day.
                 Trend = price vs its 50 / 200-day averages + 52-week range.
   Fundamentals  SEC XBRL companyfacts (free, public domain), refreshed when a
                 new 10-K / 10-Q / 20-F / 40-F lands or every 35 days.
   Filings       SEC submissions feed, every 6 hours (new filings -> updates).
   Analysts      Finnhub recommendation trends + key metrics (FINNHUB_KEY),
                 weekly, paced so it never starves the terminal's quote feed.
   Korvus reads  The terminal's SEC reads (korvus.db) and Korvus Intel's reads
                 (korvus_intel.db) for the same ticker, last 45 days.

 LIVE LAYERS
   Filings       The EDGAR "latest filings" feed is polled every minute, so a
                 new 8-K / 10-Q / 10-K from any board company shows up as an
                 update within about a minute and its numbers are re-read.
   Prices        COMPASS_LIVE=1 adds FMP batch quotes every minute during the
                 session (display only; scores stay on daily closes). Off by
                 default until the data-display terms are confirmed.
   Index funds   SPY / QQQ / DIA / IWM ride along as trend-only tiles.
   Analyst       korvus_compass_agent.py writes a brief for every stock so the
                 pages are filled before anyone opens them.

 RUNS
   As its own service (korvus-compass.service): the second Korvus crawler, a
   sibling of Korvus Intel. Own DB (korvus_compass.db), own heartbeat, shown on
   the BlackCrown admin. The two crawlers share one SEC request budget
   (COMPASS_SEC_RPS + INTEL_SEC_RPS + the terminal watcher stay under 10/s) and
   one vault: Intel's sync publishes both crawlers' notes to blackcrown-brain.
   COMPASS_ENABLED=0 switches it off.

 CLI
   python korvus_compass.py --probe AAPL      test every data source for one name
   python korvus_compass.py --universe        refresh the stock list now
   python korvus_compass.py --once [--limit N] run every job once (N names)
   python korvus_compass.py --stock AAPL      refresh + print one stock
   python korvus_compass.py --top 20          print the current board
   python korvus_compass.py --backfill        fill the whole board now, start to finish
   python korvus_compass.py --loop            what the service runs
==============================================================================
"""

import os
import re
import json
import math
import time
import sqlite3
import argparse
import threading
import datetime as dt
from html.parser import HTMLParser
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus_compass.db")
UNIVERSE_CACHE = os.path.join(HERE, "compass_universe.json")
KORVUS_DB = os.path.join(HERE, "korvus.db")
INTEL_DB = os.path.join(HERE, "korvus_intel.db")


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key, default)
    v = (v or "").split(" #")[0].strip().strip('"').strip("'").strip()
    return v or default


def _num(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


ENABLED      = _env("COMPASS_ENABLED", "1").lower() not in ("0", "false", "no", "off")
FMP_KEY      = _env("FMP_KEY")
FINNHUB_KEY  = _env("FINNHUB_KEY")
FMP_GAP      = max(0.05, _num("COMPASS_FMP_GAP", 0.15))      # seconds between FMP calls
FINNHUB_GAP  = max(1.0, _num("COMPASS_FINNHUB_GAP", 2.0))    # ~24/min, leaves room for quotes
EXTRA        = [t.strip().upper() for t in _env("COMPASS_EXTRA_TICKERS").split(",") if t.strip()]
LIVE         = _env("COMPASS_LIVE", "0").lower() in ("1", "true", "yes", "on")
LIVE_SECONDS = max(30, int(_num("COMPASS_LIVE_SECONDS", 60)))
SEC_RPS      = max(0.5, min(6.0, _num("COMPASS_SEC_RPS", 4)))   # this crawler's slice of the SEC's 10/s
TICK_SECONDS = 300
FEED_FORMS   = ["8-K", "10-Q", "10-K", "20-F", "40-F", "6-K"]

GLOBAL_MAX      = int(_num("COMPASS_GLOBAL_MAX", 150))          # largest non-US companies listed in the US
GLOBAL_MIN_MCAP = _num("COMPASS_GLOBAL_MIN_MCAP", 15_000_000_000)

COUNTRY_NAMES = {
    "US": "United States", "CN": "China", "TW": "Taiwan", "JP": "Japan", "GB": "United Kingdom", "NL": "Netherlands",
    "DE": "Germany", "FR": "France", "CH": "Switzerland", "IE": "Ireland", "CA": "Canada", "IN": "India",
    "KR": "South Korea", "BR": "Brazil", "AU": "Australia", "DK": "Denmark", "IL": "Israel", "SG": "Singapore",
    "HK": "Hong Kong", "ES": "Spain", "IT": "Italy", "SE": "Sweden", "MX": "Mexico", "AR": "Argentina",
    "ZA": "South Africa", "BE": "Belgium", "FI": "Finland", "NO": "Norway", "LU": "Luxembourg", "BM": "Bermuda",
    "UY": "Uruguay", "KY": "Cayman Islands", "CL": "Chile", "CO": "Colombia", "PE": "Peru", "ID": "Indonesia",
    "PH": "Philippines", "TH": "Thailand", "TR": "Turkey", "GR": "Greece", "PT": "Portugal", "AT": "Austria",
    "NZ": "New Zealand", "KZ": "Kazakhstan", "JE": "Jersey", "MC": "Monaco", "PA": "Panama", "AE": "UAE",
}

# Index funds shown as trend-only tiles above the board. They have no company
# financials, so they are never scored or ranked against the stocks.
ETFS = {
    "SPY": ("S&P 500", "The 500 largest US companies. The index ES futures track."),
    "QQQ": ("Nasdaq-100", "The 100 largest Nasdaq companies, tech heavy. The index NQ futures track."),
    "DIA": ("Dow 30", "30 blue-chip US companies. The index YM futures track."),
    "IWM": ("Russell 2000", "2,000 smaller US companies. The index RTY futures track."),
    # the rest of the world, one fund per market
    "EFA": ("Developed markets", "Large companies in Europe, Japan and Australia. Everything developed outside the US and Canada."),
    "EEM": ("Emerging markets", "Large companies in China, India, Taiwan, Korea, Brazil and other emerging economies."),
    "EWU": ("United Kingdom", "Large London-listed companies."),
    "EWG": ("Germany", "Large German companies."),
    "EWJ": ("Japan", "Large Tokyo-listed companies."),
    "FXI": ("China", "50 of the largest Chinese companies listed in Hong Kong."),
    "INDA": ("India", "Large Indian companies."),
    "EWZ": ("Brazil", "Large Brazilian companies."),
}
US_FUNDS = ("SPY", "QQQ", "DIA", "IWM")

UA = {"User-Agent": "Korvus Compass (BlackCrownVxJ LLC) BlackCrownVxJ.LLC@outlook.com"}

# Plain-English names for the forms a long-term holder cares about. Anything
# else (Form 4s, S-8s, prospectus supplements) is noise on this board.
FORM_NAMES = {
    "10-K": "Annual report", "10-K/A": "Annual report (amended)",
    "10-Q": "Quarterly report", "10-Q/A": "Quarterly report (amended)",
    "8-K": "Material event", "20-F": "Annual report (foreign issuer)",
    "40-F": "Annual report (Canadian issuer)", "6-K": "Foreign issuer report",
    "DEF 14A": "Proxy statement", "S-1": "Registration (new shares)",
    "SC 13D": "Activist stake", "SC 13D/A": "Activist stake (update)",
    "SCHEDULE 13D": "Activist stake", "SCHEDULE 13D/A": "Activist stake (update)",
}
PERIODIC = {"10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A", "40-F/A"}

WEIGHTS = {"trend": 20, "growth": 20, "quality": 20, "value": 10,
           "balance": 10, "holder": 5, "analysts": 10, "catalysts": 5}
PART_LABELS = {"trend": "Price trend", "growth": "Growth", "quality": "Profitability",
               "value": "Valuation", "balance": "Balance sheet", "holder": "Shareholder returns",
               "analysts": "Analyst consensus", "catalysts": "Recent Korvus reads"}

DISCLAIMER = ("Korvus Compass is research, not investment advice. Scores summarize public data "
              "(SEC filings, price history, analyst consensus) with a fixed, published formula. "
              "They are not predictions or recommendations to buy or sell. Past performance does "
              "not guarantee future results. Do your own research.")


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def et_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return utcnow() - dt.timedelta(hours=5)


def iso(d: Optional[dt.datetime] = None) -> str:
    return (d or utcnow()).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def age_hours(s: str) -> float:
    d = parse_iso(s)
    return 1e9 if not d else (utcnow() - d).total_seconds() / 3600.0


def norm_symbol(s: str) -> str:
    """BRK.B / BRK B -> BRK-B (the form SEC and FMP both use)."""
    s = (s or "").strip().upper().replace(".", "-").replace(" ", "-")
    return re.sub(r"[^A-Z0-9\-]", "", s)


def scale(x, lo, hi) -> Optional[float]:
    """Linear 0..1 between lo and hi (hi may be below lo for 'lower is better')."""
    if x is None or isinstance(x, bool):
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or hi == lo:
        return None
    t = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, t))


def _avg(vals):
    v = [x for x in vals if x is not None]
    return (sum(v) / len(v)) if v else None


def _r(x, n=4):
    try:
        return None if x is None else round(float(x), n)
    except (TypeError, ValueError):
        return None


def money(x, cur: str = "USD") -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    a = abs(x)
    sym = "$" if cur == "USD" else ""
    tail = "" if cur == "USD" else f" {cur}"
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}{sym}{a / div:.1f}{suf}{tail}"
    return f"{sign}{sym}{a:.0f}{tail}"


def pct(x, digits=0, signed=True) -> str:
    if x is None:
        return "n/a"
    s = f"{x * 100:+.{digits}f}%" if signed else f"{x * 100:.{digits}f}%"
    return s


# ----------------------------------------------------------------------------
# DATABASE (separate on purpose: korvus.db is the terminal's)
# ----------------------------------------------------------------------------
JSON_COLS = ("chart", "chart5", "spark", "fund", "filings", "analysts", "metrics", "parts", "brief")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stocks (
            symbol TEXT PRIMARY KEY, name TEXT, cik INTEGER,
            sector TEXT, industry TEXT, in_sp INTEGER DEFAULT 0, in_ndx INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            price REAL, price_date TEXT, chg_1d REAL, ret_1m REAL, ret_3m REAL, ret_6m REAL,
            ret_1y REAL, ma50 REAL, ma200 REAL, ma50_slope REAL, hi52 REAL, lo52 REAL,
            pos52 REAL, drawdown REAL, vol_1y REAL, trend TEXT, trend_tone TEXT,
            chart TEXT, spark TEXT, price_utc TEXT,
            fund TEXT, fund_utc TEXT, fund_accn TEXT,
            filings TEXT, subs_utc TEXT,
            analysts TEXT, metrics TEXT, analyst_utc TEXT,
            score REAL, tier TEXT, read TEXT, coverage REAL, parts TEXT, score_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, ts_utc TEXT NOT NULL, kind TEXT, tone TEXT,
            title TEXT, detail TEXT, url TEXT
        );
        CREATE INDEX IF NOT EXISTS ev_ts ON events(ts_utc);
        CREATE INDEX IF NOT EXISTS ev_sym ON events(symbol, ts_utc);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS seen_filings (
            accession TEXT NOT NULL, symbol TEXT NOT NULL, ts_utc TEXT,
            PRIMARY KEY (accession, symbol)
        );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stocks)").fetchall()}
    for col, ddl in (("country", "TEXT"), ("in_global", "INTEGER DEFAULT 0"), ("kind", "TEXT DEFAULT 'stock'"), ("note", "TEXT"), ("chart5", "TEXT"), ("ret_5y", "REAL"),
                     ("live_price", "REAL"), ("live_chg", "REAL"), ("live_utc", "TEXT"),
                     ("brief", "TEXT"), ("brief_sig", "TEXT"), ("brief_utc", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE stocks ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()


def first_seen(accession: str, symbol: str, conn) -> bool:
    """True the first time this (filing, stock) pair is seen. The feed hook and
    the submissions sweep both call it, so a filing is announced exactly once."""
    if not accession:
        return False
    cur = conn.execute("INSERT OR IGNORE INTO seen_filings(accession, symbol, ts_utc) VALUES(?,?,?)",
                       (accession, symbol, iso()))
    return cur.rowcount == 1


def meta_get(k: str, default: str = "") -> str:
    conn = db()
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    conn.close()
    return r["v"] if r else default


def meta_set(k: str, v):
    conn = db()
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (k, v if isinstance(v, str) else json.dumps(v)))
    conn.commit()
    conn.close()


def row_to_dict(r) -> dict:
    d = dict(r)
    for c in JSON_COLS:
        if c in d and isinstance(d[c], str) and d[c]:
            try:
                d[c] = json.loads(d[c])
            except Exception:
                d[c] = None
    return d


def load_stock(symbol: str, conn=None) -> Optional[dict]:
    own = conn is None
    conn = conn or db()
    r = conn.execute("SELECT * FROM stocks WHERE symbol=?", (symbol,)).fetchone()
    if own:
        conn.close()
    return row_to_dict(r) if r else None


def save_fields(symbol: str, fields: dict, conn=None):
    if not fields:
        return
    own = conn is None
    conn = conn or db()
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(json.dumps(v, separators=(",", ":")) if k in JSON_COLS and v is not None else v)
    vals.append(symbol)
    conn.execute(f"UPDATE stocks SET {', '.join(cols)} WHERE symbol=?", vals)
    if own:
        conn.commit()
        conn.close()


def add_event(symbol: str, kind: str, tone: str, title: str, detail: str = "", url: str = "",
              conn=None, ts: str = ""):
    own = conn is None
    conn = conn or db()
    conn.execute("INSERT INTO events(symbol, ts_utc, kind, tone, title, detail, url) VALUES(?,?,?,?,?,?,?)",
                 (symbol, ts or iso(), kind, tone, title, detail, url))
    if own:
        conn.commit()
        conn.close()


def status_set(job: str, **kw):
    """Crawler heartbeat, shown on the page so members can see it working."""
    try:
        cur = json.loads(meta_get("status", "{}") or "{}")
    except Exception:
        cur = {}
    j = cur.get(job, {})
    j.update(kw)
    cur[job] = j
    meta_set("status", cur)


# ----------------------------------------------------------------------------
# UNIVERSE: S&P 500 + Nasdaq-100 + the largest US-listed foreign companies
# ----------------------------------------------------------------------------
class _TableGrab(HTMLParser):
    """Pulls the rows of <table id="constituents"> out of a Wikipedia page
    with the standard library only (no bs4 on the box)."""

    def __init__(self):
        super().__init__()
        self.depth = 0          # >0 while inside the target table
        self.rows, self.row, self.cell = [], None, None
        self.in_th = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif a.get("id") == "constituents":
                self.depth = 1
            return
        if not self.depth or self.depth > 1:
            return
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            self.in_th = tag == "th"
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table" and self.depth:
            self.depth -= 1
            return
        if not self.depth or self.depth > 1:
            return
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            txt = re.sub(r"\[\d+\]", "", "".join(self.cell))
            self.row.append((re.sub(r"\s+", " ", txt).strip(), self.in_th))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.depth == 1 and self.cell is not None:
            self.cell.append(data)


def parse_wiki_constituents(html: str) -> list:
    """Wikipedia constituents table -> [{symbol,name,sector,industry,cik}].
    Columns are found by header text, so a reordered table still parses."""
    p = _TableGrab()
    try:
        p.feed(html or "")
    except Exception:
        return []
    if not p.rows:
        return []
    header, body = None, []
    for row in p.rows:
        if header is None and all(is_th for _, is_th in row):
            header = [t.lower() for t, _ in row]
            continue
        body.append([t for t, _ in row])
    if not header:
        return []

    def col(*names):
        for i, h in enumerate(header):
            if any(h == n or h.startswith(n) for n in names):
                return i
        return None

    i_sym = col("symbol", "ticker")
    i_name = col("security", "company")
    i_sec = col("gics sector", "icb industry", "sector", "industry")
    i_ind = col("gics sub-industry", "icb subsector", "sub-industry", "subsector")
    i_cik = col("cik")
    if i_sym is None:
        return []
    out = []
    for r in body:
        if len(r) <= i_sym:
            continue
        sym = norm_symbol(r[i_sym])
        if not sym or len(sym) > 8:
            continue
        cik = None
        if i_cik is not None and len(r) > i_cik:
            try:
                cik = int(re.sub(r"\D", "", r[i_cik]) or 0) or None
            except ValueError:
                cik = None
        g = lambda i: (r[i] if i is not None and len(r) > i else "")
        out.append({"symbol": sym, "name": g(i_name), "sector": g(i_sec),
                    "industry": g(i_ind), "cik": cik})
    return out


def _fmp_constituents(kind: str) -> list:
    if not FMP_KEY:
        return []
    urls = [f"https://financialmodelingprep.com/stable/{kind}-constituent",
            f"https://financialmodelingprep.com/api/v3/{kind}_constituent"]
    for u in urls:
        try:
            r = requests.get(u, params={"apikey": FMP_KEY}, timeout=30)
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, list) and len(data) > 50:
                out = []
                for x in data:
                    sym = norm_symbol(x.get("symbol", ""))
                    if not sym:
                        continue
                    cik = x.get("cik")
                    try:
                        cik = int(str(cik).lstrip("0") or 0) or None
                    except (TypeError, ValueError):
                        cik = None
                    out.append({"symbol": sym, "name": x.get("name") or "",
                                "sector": x.get("sector") or "", "industry": x.get("subSector") or "",
                                "cik": cik})
                return out
        except Exception as e:
            print(f"  [compass] FMP {kind} constituents error: {e}")
    return []


def _wiki_constituents(page: str) -> list:
    try:
        r = requests.get(f"https://en.wikipedia.org/wiki/{page}", headers=UA, timeout=30)
        if r.status_code == 200:
            return parse_wiki_constituents(r.text)
    except Exception as e:
        print(f"  [compass] wikipedia {page} error: {e}")
    return []


def parse_global(rows: list, limit: int = None) -> list:
    """FMP screener rows -> the largest non-US companies listed on a US exchange
    (ADRs and direct listings: TSM, ASML, Novo Nordisk, Toyota, Shell...). They
    file 20-F / 6-K / 40-F with the SEC, so the same crawler can read them."""
    limit = GLOBAL_MAX if limit is None else limit
    seen_names, out = set(), []
    for r in sorted(rows or [], key=lambda r: -float(r.get("marketCap") or 0)):
        sym = norm_symbol(r.get("symbol", ""))
        co = (r.get("country") or "").strip().upper()
        if not sym or not co or co == "US" or r.get("isEtf") or r.get("isFund"):
            continue
        if float(r.get("marketCap") or 0) < GLOBAL_MIN_MCAP or len(sym) > 6:
            continue
        name = (r.get("companyName") or r.get("name") or "").strip()
        key = re.sub(r"[^a-z0-9]", "", name.lower())[:14]
        if key and key in seen_names:        # a second share class of the same company
            continue
        seen_names.add(key)
        out.append({"symbol": sym, "name": name, "sector": r.get("sector") or "", "industry": r.get("industry") or "",
                    "cik": None, "country": co})
        if len(out) >= limit:
            break
    return out


def _fmp_global() -> list:
    if not FMP_KEY or GLOBAL_MAX <= 0:
        return []
    params = {"marketCapMoreThan": int(GLOBAL_MIN_MCAP), "isEtf": "false", "isFund": "false",
              "isActivelyTrading": "true", "exchange": "NASDAQ,NYSE", "limit": 3000, "apikey": FMP_KEY}
    for url in ("https://financialmodelingprep.com/stable/company-screener",
                "https://financialmodelingprep.com/api/v3/stock-screener"):
        try:
            r = requests.get(url, params=params, timeout=40)
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, list) and data:
                return parse_global(data)
        except Exception as e:
            print(f"  [compass] global screener error: {e}")
    return []


def merge_universe(sp: list, ndx: list, sec_map: dict, extra: list = (), glob: list = ()) -> list:
    """One row per ticker, flagged with which list it sits in. CIKs come from
    the source when present, else from the SEC ticker map."""
    by = {}
    for src, flag in ((sp, "in_sp"), (ndx, "in_ndx")):
        for x in src:
            s = x["symbol"]
            cur = by.setdefault(s, {"symbol": s, "name": "", "sector": "", "industry": "",
                                    "cik": None, "in_sp": 0, "in_ndx": 0, "in_global": 0, "country": "US"})
            cur[flag] = 1
            for k in ("name", "sector", "industry"):
                # S&P's GICS labels win over the Nasdaq ICB ones (one taxonomy on screen)
                if x.get(k) and (not cur[k] or flag == "in_sp"):
                    cur[k] = x[k]
            if x.get("cik") and not cur["cik"]:
                cur["cik"] = int(x["cik"])
    for x in glob:
        s = x["symbol"]
        cur = by.get(s)
        if cur:                       # already an index member (Linde, Accenture...): just record the country
            cur["country"] = x.get("country") or cur["country"]
            continue
        by[s] = {"symbol": s, "name": x.get("name") or "", "sector": x.get("sector") or "",
                 "industry": x.get("industry") or "", "cik": None, "in_sp": 0, "in_ndx": 0,
                 "in_global": 1, "country": x.get("country") or ""}
    for s in extra:
        s = norm_symbol(s)
        if s and s not in by:
            by[s] = {"symbol": s, "name": "", "sector": "", "industry": "", "cik": None,
                     "in_sp": 0, "in_ndx": 0, "in_global": 0, "country": ""}
    for s, cur in by.items():
        if not cur["cik"]:
            c = sec_map.get(s) or sec_map.get(s.replace("-", "."))
            cur["cik"] = int(c) if c else None
    return sorted(by.values(), key=lambda r: r["symbol"])


def _sec_ticker_map() -> dict:
    try:
        import korvus_edgar as edgar
        edgar.load_watchlist()
        with open(edgar.TICKER_MAP_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_universe() -> tuple:
    """Returns (rows, source). Tries the paid feed, then Wikipedia, then disk."""
    sp, src_sp = _fmp_constituents("sp500"), "fmp"
    if len(sp) < 400:
        sp, src_sp = _wiki_constituents("List_of_S%26P_500_companies"), "wikipedia"
    ndx, src_ndx = _fmp_constituents("nasdaq"), "fmp"
    if len(ndx) < 80:
        ndx, src_ndx = _wiki_constituents("List_of_NASDAQ-100_companies"), "wikipedia"
    if len(sp) >= 400 and len(ndx) >= 80:
        glob = _fmp_global()
        if len(glob) < 20 and GLOBAL_MAX > 0 and os.path.exists(UNIVERSE_CACHE):
            try:      # the screener had a bad day: keep last week's international list
                with open(UNIVERSE_CACHE) as f:
                    glob = [r for r in json.load(f)["rows"] if r.get("in_global")]
            except Exception:
                glob = []
        rows = merge_universe(sp, ndx, _sec_ticker_map(), EXTRA, glob)
        try:
            with open(UNIVERSE_CACHE + ".tmp", "w") as f:
                json.dump({"asof": iso(), "rows": rows}, f)
            os.replace(UNIVERSE_CACHE + ".tmp", UNIVERSE_CACHE)
        except Exception:
            pass
        return rows, f"sp500:{src_sp} ndx:{src_ndx} global:{sum(1 for r in rows if r.get('in_global'))}"
    if os.path.exists(UNIVERSE_CACHE):
        try:
            with open(UNIVERSE_CACHE) as f:
                return json.load(f)["rows"], "disk cache"
        except Exception:
            pass
    try:
        import korvus_edgar as edgar
        rows = [{"symbol": s, "name": "", "sector": "", "industry": "", "cik": c,
                 "in_sp": 1, "in_ndx": 0} for s, c in edgar.BASE_WATCHLIST.items()]
        return rows, "fallback list"
    except Exception:
        return [], "none"


def refresh_universe(verbose: bool = True) -> int:
    rows, source = fetch_universe()
    if not rows:
        status_set("universe", error="no source answered", at=iso())
        return 0
    conn = db()
    have = {r["symbol"] for r in conn.execute("SELECT symbol FROM stocks").fetchall()}
    new_syms = {r["symbol"] for r in rows}
    seeded = bool(have - set(ETFS))
    fresh = [r["symbol"] for r in rows if r["symbol"] not in have]
    announce = seeded and len(fresh) <= 10          # a whole new list arriving is not 150 separate updates
    for r in rows:
        co, gl = r.get("country") or "", int(r.get("in_global") or 0)
        if r["symbol"] in have:
            conn.execute("""UPDATE stocks SET name=COALESCE(NULLIF(?,''),name), cik=COALESCE(?,cik),
                            sector=COALESCE(NULLIF(?,''),sector), industry=COALESCE(NULLIF(?,''),industry),
                            in_sp=?, in_ndx=?, in_global=?, country=COALESCE(NULLIF(?,''),country), active=1
                            WHERE symbol=?""",
                         (r["name"], r["cik"], r["sector"], r["industry"], r["in_sp"], r["in_ndx"], gl, co, r["symbol"]))
        else:
            conn.execute("""INSERT INTO stocks(symbol,name,cik,sector,industry,in_sp,in_ndx,in_global,country,active)
                            VALUES(?,?,?,?,?,?,?,?,?,1)""",
                         (r["symbol"], r["name"], r["cik"], r["sector"], r["industry"], r["in_sp"], r["in_ndx"], gl, co))
            if announce:
                add_event(r["symbol"], "index", "neut", f"{r['symbol']} joined the Compass board",
                          "Added to the international list." if gl else "Added to the S&P 500 or Nasdaq-100.", conn=conn)
    gone = have - new_syms - set(ETFS)
    for s in gone:
        conn.execute("UPDATE stocks SET active=0 WHERE symbol=?", (s,))
    for s, (name, note) in ETFS.items():
        conn.execute("""INSERT INTO stocks(symbol,name,sector,industry,kind,note,active)
                        VALUES(?,?,?,?,'etf',?,1) ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name, kind='etf', note=excluded.note, active=1""",
                     (s, name, "Index fund", "ETF", note))
    conn.commit()
    conn.close()
    meta_set("universe_asof", iso())
    status_set("universe", at=iso(), count=len(rows), source=source, removed=len(gone))
    if verbose:
        print(f"  [compass] universe: {len(rows)} names from {source}"
              + (f", {len(gone)} left the indexes" if gone else ""))
    return len(rows)


def active_symbols(conn=None, kind: str = "stock") -> list:
    """kind: 'stock' (the ranked board), 'etf', or '' for everything."""
    own = conn is None
    conn = conn or db()
    q = "SELECT symbol FROM stocks WHERE active=1" + (" AND COALESCE(kind,'stock')=?" if kind else "")
    rows = conn.execute(q + " ORDER BY symbol", (kind,) if kind else ()).fetchall()
    if own:
        conn.close()
    return [r["symbol"] for r in rows]


# ----------------------------------------------------------------------------
# PRICES + TREND (pure math on a list of daily closes)
# ----------------------------------------------------------------------------
def _sma(vals, n, end=None):
    end = len(vals) if end is None else end
    if end < n:
        return None
    w = vals[end - n:end]
    return sum(w) / n


def history_stats(series: list) -> dict:
    """series = [(YYYY-MM-DD, close), ...] oldest first. Returns trend stats,
    a 1-year daily chart with both averages, and a weekly sparkline."""
    series = [(d, float(c)) for d, c in series if c is not None and float(c) > 0]
    if len(series) < 2:
        return {}
    dates = [d for d, _ in series]
    c = [x for _, x in series]
    p = c[-1]

    def ret(n):
        return (p / c[-1 - n] - 1) if len(c) > n else None

    yr = c[-252:]
    hi, lo = max(yr), min(yr)
    ma50 = _sma(c, 50)
    ma200 = _sma(c, 200)
    ma50_prev = _sma(c, 50, len(c) - 20)
    slope = (ma50 / ma50_prev - 1) if (ma50 and ma50_prev) else None
    rets = [math.log(c[i] / c[i - 1]) for i in range(max(1, len(c) - 252), len(c))]
    vol = None
    if len(rets) > 20:
        m = sum(rets) / len(rets)
        vol = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252)
    start = max(0, len(c) - 252)
    chart = {"d": dates[start:], "c": [round(x, 2) for x in c[start:]],
             "m50": [(_r(_sma(c, 50, i + 1), 2)) for i in range(start, len(c))],
             "m200": [(_r(_sma(c, 200, i + 1), 2)) for i in range(start, len(c))]}
    # 5-year view: one point a week, with the same two averages
    s5 = max(0, len(c) - 1260)
    idx = list(range(len(c) - 1, s5 - 1, -5))[::-1]
    chart5 = {"d": [dates[i] for i in idx], "c": [round(c[i], 2) for i in idx],
              "m50": [_r(_sma(c, 50, i + 1), 2) for i in idx],
              "m200": [_r(_sma(c, 200, i + 1), 2) for i in idx]}
    spark = [round(x, 2) for x in c[start:][::5]]
    if spark and spark[-1] != round(p, 2):
        spark.append(round(p, 2))
    out = {
        "price": round(p, 4), "price_date": dates[-1], "chg_1d": _r(ret(1)),
        "ret_1m": _r(ret(21)), "ret_3m": _r(ret(63)), "ret_6m": _r(ret(126)),
        "ret_1y": _r(ret(252) if len(c) > 252 else (p / c[0] - 1 if len(c) > 200 else None)),
        "ret_5y": _r(ret(1260) if len(c) > 1260 else (p / c[0] - 1 if len(c) > 1000 else None)),
        "ma50": _r(ma50, 4), "ma200": _r(ma200, 4), "ma50_slope": _r(slope),
        "hi52": round(hi, 4), "lo52": round(lo, 4),
        "pos52": _r((p - lo) / (hi - lo) if hi > lo else 0.5),
        "drawdown": _r(p / hi - 1), "vol_1y": _r(vol),
        "chart": chart, "chart5": (chart5 if len(idx) > 60 else None), "spark": spark,
    }
    out["trend"], out["trend_tone"] = trend_label(p, ma50, ma200, slope)
    return out


def trend_label(p, ma50, ma200, slope) -> tuple:
    """The long-term trend in words a buy-and-hold investor uses."""
    if not p or not ma50:
        return ("Not enough history", "neut")
    if not ma200:
        return (("Short-term uptrend", "bull") if p > ma50 else ("Short-term downtrend", "bear"))
    above200, golden = p > ma200, ma50 > ma200
    if above200 and golden:
        if p >= ma50 and (slope or 0) > 0:
            return ("Strong uptrend", "bull")
        if p >= ma50:
            return ("Uptrend", "bull")
        return ("Pullback in uptrend", "neut")
    if above200 and not golden:
        return ("Turning up", "neut")
    if golden:
        return ("Losing steam", "neut")
    if p >= ma50:
        return ("Downtrend, bouncing", "bear")
    return ("Downtrend", "bear")


def _fmp_get(url: str, params: dict):
    params = dict(params, apikey=FMP_KEY)
    r = requests.get(url, params=params, timeout=30)
    body = r.text[:300]
    if r.status_code == 429 or "Limit Reach" in body:
        raise RuntimeError("FMP limit reached")
    if r.status_code in (401, 402, 403):
        raise PermissionError(f"FMP HTTP {r.status_code}: {body[:120]}")
    if r.status_code != 200:
        return None
    return r.json()


def fetch_history(symbol: str, days: int = 2000) -> list:
    """Daily closes, oldest first. FMP stable endpoint, then the legacy one."""
    if not FMP_KEY:
        return []
    frm = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    tries = [
        ("https://financialmodelingprep.com/stable/historical-price-eod/light",
         {"symbol": symbol, "from": frm}),
        (f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol}",
         {"serietype": "line", "from": frm}),
    ]
    last_perm = None
    for url, params in tries:
        try:
            data = _fmp_get(url, params)
        except PermissionError as e:
            last_perm = e
            continue
        if not data:
            continue
        rows = data.get("historical") if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            continue
        out = []
        for x in rows:
            d = (x.get("date") or "")[:10]
            c = x.get("close", x.get("price"))
            if d and c is not None:
                out.append((d, float(c)))
        out.sort()
        if out:
            return out
    if last_perm:
        raise last_perm
    return []


# ----------------------------------------------------------------------------
# SEC FUNDAMENTALS (companyfacts XBRL, pure parsing below)
# ----------------------------------------------------------------------------
TAGS = {
    "revenue": {"us-gaap": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                            "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
                            "RevenuesNetOfInterestExpense", "SalesRevenueGoodsNet"],
                "ifrs-full": ["Revenue", "RevenueFromContractsWithCustomers"]},
    "net_income": {"us-gaap": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
                   "ifrs-full": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"]},
    "op_income": {"us-gaap": ["OperatingIncomeLoss"], "ifrs-full": ["ProfitLossFromOperatingActivities"]},
    "gross_profit": {"us-gaap": ["GrossProfit"], "ifrs-full": ["GrossProfit"]},
    "eps": {"us-gaap": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
            "ifrs-full": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"]},
    "ocf": {"us-gaap": ["NetCashProvidedByUsedInOperatingActivities",
                        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
            "ifrs-full": ["CashFlowsFromUsedInOperatingActivities"]},
    "capex": {"us-gaap": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                          "PaymentsForCapitalImprovements"],
              "ifrs-full": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                            "PurchaseOfPropertyPlantAndEquipment"]},
    "dividends": {"us-gaap": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
                  "ifrs-full": ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid"]},
    "buybacks": {"us-gaap": ["PaymentsForRepurchaseOfCommonStock"],
                 "ifrs-full": ["PaymentsToAcquireOrRedeemEntitysShares"]},
    "shares": {"us-gaap": ["WeightedAverageNumberOfDilutedSharesOutstanding",
                           "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"],
               "ifrs-full": ["AdjustedWeightedAverageShares", "WeightedAverageShares"]},
    # balance-sheet (point in time)
    "cash": {"us-gaap": ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
             "ifrs-full": ["CashAndCashEquivalents"]},
    "debt": {"us-gaap": ["LongTermDebt", "LongTermDebtNoncurrent", "DebtInstrumentCarryingAmount"],
             "ifrs-full": ["Borrowings", "NoncurrentPortionOfNoncurrentBorrowings"]},
    "debt_current": {"us-gaap": ["LongTermDebtCurrent", "DebtCurrent"],
                     "ifrs-full": ["CurrentPortionOfNoncurrentBorrowings"]},
    "equity": {"us-gaap": ["StockholdersEquity",
                           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
               "ifrs-full": ["EquityAttributableToOwnersOfParent", "Equity"]},
}
INSTANT = {"cash", "debt", "debt_current", "equity"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT"}


def _days(a: str, b: str) -> int:
    try:
        return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
    except Exception:
        return -1


def _unit_entries(concept: dict, per_share: bool):
    """Pick the unit list: USD first, else the most-used currency."""
    units = (concept or {}).get("units") or {}
    if not units:
        return None, []
    keys = list(units.keys())
    if per_share:
        keys = [k for k in keys if "/shares" in k] or keys
        pick = "USD/shares" if "USD/shares" in keys else max(keys, key=lambda k: len(units[k]))
        return pick.split("/")[0], units[pick]
    if "shares" in units and "USD" not in units and len(keys) == 1:
        return "shares", units["shares"]
    keys = [k for k in keys if "/" not in k and k != "pure"] or keys
    pick = "USD" if "USD" in keys else max(keys, key=lambda k: len(units[k]))
    return pick, units[pick]


def _latest_by(entries, key):
    """Dedupe on key(e), keeping the most recently filed value (restatements win)."""
    best = {}
    for e in entries:
        k = key(e)
        if k is None:
            continue
        if k not in best or (e.get("filed") or "") > (best[k].get("filed") or ""):
            best[k] = e
    return best


def annual_series(entries: list) -> list:
    """[(end, val)] fiscal-year values from annual reports, oldest first."""
    fy = [e for e in entries if e.get("start") and e.get("end")
          and 330 <= _days(e["start"], e["end"]) <= 400]
    by = _latest_by(fy, lambda e: e["end"])
    return sorted((k, float(v["val"])) for k, v in by.items())


def quarter_series(entries: list) -> list:
    """[(end, val)] 3-month values, oldest first. Q4 is rarely filed on its own,
    so it is derived as fiscal year minus the three quarters inside it."""
    q = [e for e in entries if e.get("start") and e.get("end") and 80 <= _days(e["start"], e["end"]) <= 100]
    qby = {k: (v["start"], float(v["val"])) for k, v in _latest_by(q, lambda e: e["end"]).items()}
    fy = [e for e in entries if e.get("start") and e.get("end") and 330 <= _days(e["start"], e["end"]) <= 400]
    for end, e in _latest_by(fy, lambda e: e["end"]).items():
        if end in qby:
            continue
        inside = [(k, v) for k, v in qby.items() if v[0] >= e["start"] and k < end]
        if len(inside) == 3:
            qby[end] = (max(k for k, _ in inside), float(e["val"]) - sum(v[1] for _, v in inside))
    return sorted((k, v[1]) for k, v in qby.items())


def instant_series(entries: list) -> list:
    pts = [e for e in entries if e.get("end") and not e.get("start")]
    by = _latest_by(pts, lambda e: e["end"])
    return sorted((k, float(v["val"])) for k, v in by.items())


def _pick_concept(facts: dict, metric: str):
    """Among candidate tags, use the one with the freshest data.
    Returns (last_end, currency, entries, taxonomy, tag) or None."""
    best = None
    for tax in ("us-gaap", "ifrs-full"):
        for tag in TAGS[metric].get(tax, []):
            c = (facts.get(tax) or {}).get(tag)
            if not c:
                continue
            cur, ents = _unit_entries(c, per_share=(metric == "eps"))
            if not ents:
                continue
            last = max((e.get("end") or "") for e in ents)
            if best is None or last > best[0]:
                best = (last, cur, ents, tax, tag)
    return best


def _yoy_quarter(series: list):
    """Latest quarter vs the same quarter a year earlier."""
    if len(series) < 2:
        return None, None, None
    end, val = series[-1]
    for k, v in reversed(series[:-1]):
        gap = _days(k, end)
        if 340 <= gap <= 390:
            return end, val, ((val / v - 1) if v and v > 0 else None)
    return end, val, None


def _ttm(series: list):
    if len(series) < 4:
        return None
    last4 = series[-4:]
    if _days(last4[0][0], last4[-1][0]) > 300:
        return None
    return sum(v for _, v in last4)


def _growth(a, b):
    return (a / b - 1) if (a is not None and b and b > 0) else None


def _cagr(series: list, years: int):
    if len(series) <= years:
        return None
    a, b = series[-1][1], series[-1 - years][1]
    if a is None or not b or b <= 0 or a <= 0:
        return None
    return (a / b) ** (1.0 / years) - 1


def parse_companyfacts(j: dict) -> dict:
    """SEC companyfacts JSON -> the fundamentals a long-term holder reads."""
    facts = (j or {}).get("facts") or {}
    if not facts:
        return {}
    out = {"currency": "USD", "taxonomy": None}
    ann, qtr, inst = {}, {}, {}
    used = {}
    for m in TAGS:
        got = _pick_concept(facts, m)
        if not got:
            continue
        _, cur, ents, tax, tag = got
        used[m] = tag
        if m == "revenue":
            out["currency"], out["taxonomy"] = (cur if cur != "shares" else "USD"), tax
        if m in INSTANT:
            inst[m] = instant_series(ents)
        else:
            ann[m] = annual_series(ents)
            qtr[m] = quarter_series(ents)
    if not ann.get("revenue") and not ann.get("net_income"):
        return {}
    cur = out["currency"]

    def last(series_map, m):
        s = series_map.get(m) or []
        return s[-1][1] if s else None

    rev_a = ann.get("revenue") or []
    ni_a = ann.get("net_income") or []
    eps_a = ann.get("eps") or []
    ocf_a, cap_a = ann.get("ocf") or [], ann.get("capex") or []

    out["fy_end"] = rev_a[-1][0] if rev_a else (ni_a[-1][0] if ni_a else None)
    out["revenue"] = last(ann, "revenue")
    out["revenue_yoy"] = _growth(rev_a[-1][1], rev_a[-2][1]) if len(rev_a) > 1 else None
    out["revenue_cagr3"] = _cagr(rev_a, 3)
    out["net_income"] = last(ann, "net_income")
    out["op_income"] = last(ann, "op_income")
    out["gross_profit"] = last(ann, "gross_profit")
    rv = out["revenue"]
    out["net_margin"] = (out["net_income"] / rv) if (rv and out["net_income"] is not None) else None
    out["op_margin"] = (out["op_income"] / rv) if (rv and out["op_income"] is not None) else None
    out["gross_margin"] = (out["gross_profit"] / rv) if (rv and out["gross_profit"] is not None) else None
    out["eps"] = last(ann, "eps")
    out["eps_yoy"] = (_growth(eps_a[-1][1], eps_a[-2][1]) if len(eps_a) > 1 else None)

    fcf_series = []
    cap_by = dict(cap_a)
    for end, v in ocf_a:
        fcf_series.append((end, v - abs(cap_by.get(end, 0.0))))
    out["ocf"] = last(ann, "ocf")
    out["capex"] = abs(last(ann, "capex")) if last(ann, "capex") is not None else None
    out["fcf"] = fcf_series[-1][1] if fcf_series else None
    out["fcf_margin"] = (out["fcf"] / rv) if (rv and out["fcf"] is not None) else None
    out["dividends_paid"] = abs(last(ann, "dividends")) if last(ann, "dividends") is not None else None
    out["buybacks"] = abs(last(ann, "buybacks")) if last(ann, "buybacks") is not None else None

    sh_a = ann.get("shares") or []
    out["shares_diluted"] = sh_a[-1][1] if sh_a else None
    out["shares_change_3y"] = _growth(sh_a[-1][1], sh_a[-4][1]) if len(sh_a) >= 4 else (
        _growth(sh_a[-1][1], sh_a[0][1]) if len(sh_a) >= 2 else None)

    # latest quarter and trailing twelve months
    qe, qv, qy = _yoy_quarter(qtr.get("revenue") or [])
    out["q_end"], out["q_revenue"], out["q_revenue_yoy"] = qe, qv, qy
    _, nv, ny = _yoy_quarter(qtr.get("net_income") or [])
    out["q_net_income"], out["q_net_income_yoy"] = nv, ny
    _, ev, ey = _yoy_quarter(qtr.get("eps") or [])
    out["q_eps"], out["q_eps_yoy"] = ev, ey
    out["revenue_ttm"] = _ttm(qtr.get("revenue") or [])
    out["net_income_ttm"] = _ttm(qtr.get("net_income") or [])
    out["eps_ttm"] = _ttm(qtr.get("eps") or [])

    cash_s, eq_s = inst.get("cash") or [], inst.get("equity") or []
    debt_s, dc_s = inst.get("debt") or [], inst.get("debt_current") or []
    out["cash"] = cash_s[-1][1] if cash_s else None
    out["bs_date"] = cash_s[-1][0] if cash_s else (eq_s[-1][0] if eq_s else None)
    debt = debt_s[-1][1] if debt_s else None
    if (debt is not None and dc_s and debt_s and dc_s[-1][0] == debt_s[-1][0]
            and used.get("debt") in ("LongTermDebtNoncurrent", "NoncurrentPortionOfNoncurrentBorrowings")):
        # LongTermDebt already includes the current slice; the noncurrent tags don't
        debt += dc_s[-1][1]
    out["debt"] = debt
    out["equity"] = eq_s[-1][1] if eq_s else None
    out["net_cash"] = (out["cash"] - (debt or 0)) if out["cash"] is not None else None
    eq = out["equity"]
    out["debt_to_equity"] = (debt / eq) if (debt is not None and eq and eq > 0) else None
    out["roe"] = (out["net_income"] / eq) if (out["net_income"] is not None and eq and eq > 0) else None

    # five-year history for the bar charts
    def tail(s, n=5):
        return [[k, _r(v, 2)] for k, v in s[-n:]]
    out["history"] = {"revenue": tail(rev_a), "net_income": tail(ni_a), "fcf": tail(fcf_series),
                      "eps": tail(eps_a)}
    for k in list(out.keys()):
        if isinstance(out[k], float):
            out[k] = _r(out[k], 4 if abs(out[k]) < 1000 else 0)
    out["currency"] = cur
    return out


def parse_submissions(j: dict, limit: int = 15) -> list:
    """SEC submissions JSON -> the most recent notable filings."""
    rec = ((j or {}).get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    out = []
    cik = (j or {}).get("cik")
    for i, form in enumerate(forms):
        if form not in FORM_NAMES:
            continue
        accn = (rec.get("accessionNumber") or [""])[i] if i < len(rec.get("accessionNumber") or []) else ""
        doc = (rec.get("primaryDocument") or [""])[i] if i < len(rec.get("primaryDocument") or []) else ""
        filed = (rec.get("filingDate") or [""])[i] if i < len(rec.get("filingDate") or []) else ""
        items = (rec.get("items") or [""])[i] if i < len(rec.get("items") or []) else ""
        url = ""
        if cik and accn:
            base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}"
            url = f"{base}/{doc}" if doc else f"{base}/{accn}-index.htm"
        out.append({"form": form, "label": FORM_NAMES[form], "filed": filed, "accn": accn,
                    "items": items, "url": url})
        if len(out) >= limit:
            break
    return out


def _sec_json(url: str) -> Optional[dict]:
    import korvus_edgar as edgar      # shared polite session + rate limit
    raw = edgar._get(url, timeout=60)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def fetch_companyfacts(cik: int) -> Optional[dict]:
    return _sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")


def fetch_submissions(cik: int) -> Optional[dict]:
    return _sec_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")


# ----------------------------------------------------------------------------
# ANALYSTS + KEY METRICS (Finnhub)
# ----------------------------------------------------------------------------
_fh_last = [0.0]
_fh_lock = threading.Lock()

METRIC_KEYS = ["peTTM", "peBasicExclExtraTTM", "psTTM", "pbAnnual", "marketCapitalization", "beta",
               "dividendYieldIndicatedAnnual", "currentDividendYieldTTM", "payoutRatioTTM", "roeTTM",
               "netProfitMarginTTM", "operatingMarginTTM", "grossMarginTTM", "revenueGrowthTTMYoy",
               "epsGrowthTTMYoy", "revenueGrowth5Y", "epsGrowth5Y", "dividendGrowthRate5Y",
               "totalDebt/totalEquityQuarterly", "currentRatioQuarterly", "epsTTM",
               "52WeekHigh", "52WeekLow", "52WeekPriceReturnDaily"]


def _finnhub(path: str, params: dict):
    if not FINNHUB_KEY:
        return None
    with _fh_lock:
        wait = FINNHUB_GAP - (time.time() - _fh_last[0])
        if wait > 0:
            time.sleep(wait)
        _fh_last[0] = time.time()
    try:
        r = requests.get(f"https://finnhub.io/api/v1/{path}", params=dict(params, token=FINNHUB_KEY), timeout=20)
        if r.status_code == 429:
            time.sleep(30)
            return None
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def analyst_summary(recs: list) -> dict:
    """Finnhub monthly recommendation trend -> one consensus line."""
    if not isinstance(recs, list) or not recs:
        return {}
    recs = sorted(recs, key=lambda x: x.get("period") or "", reverse=True)
    cur = recs[0]
    keys = ("strongBuy", "buy", "hold", "sell", "strongSell")

    def score(x):
        n = sum(int(x.get(k) or 0) for k in keys)
        if not n:
            return None, 0
        s = (5 * (x.get("strongBuy") or 0) + 4 * (x.get("buy") or 0) + 3 * (x.get("hold") or 0)
             + 2 * (x.get("sell") or 0) + 1 * (x.get("strongSell") or 0)) / n
        return s, n

    s, n = score(cur)
    if s is None:
        return {}
    prev = recs[3] if len(recs) > 3 else (recs[-1] if len(recs) > 1 else None)
    ps = score(prev)[0] if prev else None
    if s >= 4.3:
        label = "Strong buy"
    elif s >= 3.7:
        label = "Buy"
    elif s >= 2.8:
        label = "Hold"
    elif s >= 2.2:
        label = "Sell"
    else:
        label = "Strong sell"
    return {"period": cur.get("period"), "analysts": n, "score": round(s, 2), "label": label,
            "buy_pct": round(((cur.get("strongBuy") or 0) + (cur.get("buy") or 0)) / n, 3),
            "counts": {k: int(cur.get(k) or 0) for k in keys},
            "trend": (None if ps is None else round(s - ps, 2))}


def fetch_analysts(symbol: str) -> tuple:
    recs = _finnhub("stock/recommendation", {"symbol": symbol})
    met = _finnhub("stock/metric", {"symbol": symbol, "metric": "all"})
    metrics = {}
    if isinstance(met, dict) and isinstance(met.get("metric"), dict):
        m = met["metric"]
        metrics = {k: m.get(k) for k in METRIC_KEYS if m.get(k) is not None}
    return analyst_summary(recs or []), metrics


# ----------------------------------------------------------------------------
# KORVUS READS (what the terminal and Korvus Intel already said)
# ----------------------------------------------------------------------------
def korvus_reads(symbol: str, days: int = 45, limit: int = 12) -> list:
    since = iso(utcnow() - dt.timedelta(days=days))
    out = []
    if os.path.exists(KORVUS_DB):
        try:
            c = sqlite3.connect(f"file:{KORVUS_DB}?mode=ro", uri=True, timeout=10)
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT created_at, source_name, url, headline, title, summary, direction, impact
                   FROM items WHERE source='filing' AND processed=1 AND created_at>=?
                   AND (source_name LIKE ? ) ORDER BY created_at DESC LIMIT ?""",
                (since, f"% · {symbol}", limit)).fetchall()
            c.close()
            for r in rows:
                out.append({"at": r["created_at"], "src": "Korvus SEC read", "form": (r["source_name"] or "")
                            .replace("SEC", "").split("·")[0].strip(), "title": r["title"] or r["headline"],
                            "summary": r["summary"] or "", "dir": r["direction"] or "neut",
                            "impact": r["impact"] or "low", "url": r["url"] or ""})
        except Exception:
            pass
    if os.path.exists(INTEL_DB):
        try:
            c = sqlite3.connect(f"file:{INTEL_DB}?mode=ro", uri=True, timeout=10)
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT seen_utc, form, url, headline, summary, direction, impact FROM filings
                   WHERE symbol=? AND scored=1 AND seen_utc>=? ORDER BY seen_utc DESC LIMIT ?""",
                (symbol, since, limit)).fetchall()
            c.close()
            have = {x["url"] for x in out}
            for r in rows:
                if r["url"] in have:
                    continue
                out.append({"at": r["seen_utc"], "src": "Korvus Intel read", "form": r["form"] or "",
                            "title": r["headline"] or "", "summary": r["summary"] or "",
                            "dir": r["direction"] or "neut", "impact": r["impact"] or "low", "url": r["url"] or ""})
        except Exception:
            pass
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out[:limit]


def catalyst_tone(reads: list) -> Optional[float]:
    """Net tone of recent reads in [-1, 1], weighted by impact. None if quiet."""
    if not reads:
        return None
    w = {"high": 3, "med": 2, "low": 1}
    tot = net = 0.0
    for r in reads:
        k = w.get(r.get("impact"), 1)
        tot += k
        net += k * {"bull": 1, "bear": -1}.get(r.get("dir"), 0)
    return (net / tot) if tot else None


# ----------------------------------------------------------------------------
# THE COMPASS SCORE (transparent, every part explained)
# ----------------------------------------------------------------------------
def _num_or_none(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def valuation(s: dict) -> dict:
    """P/E, FCF yield, dividend yield, market cap. Finnhub's figures first
    (they handle share classes and ADR ratios); our own math as the backup,
    only for US-dollar filers so a foreign-currency EPS never meets a USD price."""
    f, m = s.get("fund") or {}, s.get("metrics") or {}
    price = _num_or_none(s.get("price"))
    mcap = _num_or_none(m.get("marketCapitalization"))
    mcap = mcap * 1e6 if mcap else None
    usd = (f.get("currency") == "USD")
    if not mcap and price and usd and f.get("shares_diluted"):
        mcap = price * f["shares_diluted"]
    pe = _num_or_none(m.get("peTTM")) or _num_or_none(m.get("peBasicExclExtraTTM"))
    if pe is None and price and usd:
        eps = f.get("eps_ttm") or f.get("eps")
        if eps:
            pe = price / eps if eps > 0 else -1.0
    fcf_yield = (f["fcf"] / mcap) if (usd and mcap and f.get("fcf") is not None) else None
    dy = _num_or_none(m.get("dividendYieldIndicatedAnnual")) or _num_or_none(m.get("currentDividendYieldTTM"))
    dy = dy / 100.0 if dy is not None else ((f["dividends_paid"] / mcap) if (usd and mcap and f.get("dividends_paid")) else None)
    holder_yield = None
    if usd and mcap and (f.get("dividends_paid") or f.get("buybacks")):
        holder_yield = ((f.get("dividends_paid") or 0) + (f.get("buybacks") or 0)) / mcap
    return {"mcap": _r(mcap, 0), "pe": _r(pe, 2), "fcf_yield": _r(fcf_yield), "div_yield": _r(dy),
            "holder_yield": _r(holder_yield)}


def score_stock(s: dict) -> dict:
    """Returns {score, tier, read, coverage, parts:[...], val:{...}}."""
    f, m, a = s.get("fund") or {}, s.get("metrics") or {}, s.get("analysts") or {}
    val = valuation(s)
    fin = (s.get("sector") or "").lower().startswith("financ")
    parts = {}

    # Price trend
    if s.get("price") and (s.get("ma50") or s.get("pos52") is not None):
        p, ma50, ma200 = s.get("price"), s.get("ma50"), s.get("ma200")
        bits, notes = [], []
        if ma200:
            bits.append((0.30, 1.0 if p > ma200 else 0.0))
            notes.append("Price is above its 200-day average" if p > ma200 else "Price is below its 200-day average")
            if ma50:
                bits.append((0.20, 1.0 if ma50 > ma200 else 0.0))
                notes.append("50-day average is above the 200-day" if ma50 > ma200
                             else "50-day average is below the 200-day")
        if s.get("ma50_slope") is not None:
            bits.append((0.10, 1.0 if s["ma50_slope"] > 0 else 0.0))
        if s.get("pos52") is not None:
            bits.append((0.20, s["pos52"]))
            notes.append(f"Sits {round(s['pos52'] * 100)}% of the way up its 52-week range")
        r1 = s.get("ret_1y")
        if r1 is not None:
            bits.append((0.20, scale(r1, -0.30, 0.40)))
            notes.append(f"{pct(r1)} over the past year")
        if bits:
            wsum = sum(w for w, _ in bits)
            parts["trend"] = (sum(w * v for w, v in bits) / wsum, notes)

    # Growth
    g_notes, g = [], []
    if f.get("revenue_cagr3") is not None:
        g.append(scale(f["revenue_cagr3"], -0.05, 0.25))
        g_notes.append(f"Revenue {pct(f['revenue_cagr3'])} a year over 3 years")
    ry = f.get("q_revenue_yoy") if f.get("q_revenue_yoy") is not None else f.get("revenue_yoy")
    if ry is None and m.get("revenueGrowthTTMYoy") is not None:
        ry = m["revenueGrowthTTMYoy"] / 100.0
    if ry is not None:
        g.append(scale(ry, -0.10, 0.30))
        g_notes.append(f"Latest revenue {pct(ry)} year over year")
    ey = f.get("eps_yoy")
    if ey is None and m.get("epsGrowthTTMYoy") is not None:
        ey = m["epsGrowthTTMYoy"] / 100.0
    if ey is not None and (f.get("eps") or 0) > 0:
        g.append(scale(ey, -0.30, 0.40))
        g_notes.append(f"Earnings per share {pct(ey)} year over year")
    if _avg(g) is not None:
        parts["growth"] = (_avg(g), g_notes)

    # Profitability
    q, q_notes = [], []
    nm = f.get("net_margin")
    if nm is None and m.get("netProfitMarginTTM") is not None:
        nm = m["netProfitMarginTTM"] / 100.0
    if nm is not None:
        q.append(scale(nm, -0.05, 0.25))
        q_notes.append(f"Keeps {pct(nm, 0, signed=False)} of revenue as profit")
    if f.get("fcf_margin") is not None and not fin:
        q.append(scale(f["fcf_margin"], -0.05, 0.25))
        q_notes.append(f"Free cash flow margin {pct(f['fcf_margin'], 0, signed=False)}")
    roe = f.get("roe")
    if roe is None and m.get("roeTTM") is not None:
        roe = m["roeTTM"] / 100.0
    if roe is not None:
        q.append(scale(roe, 0.0, 0.30))
        q_notes.append(f"Return on equity {pct(roe, 0, signed=False)}")
    if f.get("fcf") is not None and not fin:
        q.append(1.0 if f["fcf"] > 0 else 0.0)
        q_notes.append("Generates free cash" if f["fcf"] > 0 else "Burning cash")
    if _avg(q) is not None:
        parts["quality"] = (_avg(q), q_notes)

    # Valuation
    v, v_notes = [], []
    if val["fcf_yield"] is not None and not fin:
        v.append(scale(val["fcf_yield"], 0.0, 0.07))
        v_notes.append(f"Free cash flow yield {pct(val['fcf_yield'], 1, signed=False)}")
    if val["pe"] is not None:
        v.append(0.0 if val["pe"] <= 0 else scale(val["pe"], 45, 12))
        v_notes.append("Not profitable on a trailing basis" if val["pe"] <= 0
                       else f"Trades at {val['pe']:.0f}x earnings")
    if _avg(v) is not None:
        parts["value"] = (_avg(v), v_notes)

    # Balance sheet (skipped for banks / insurers, where debt is the product)
    if not fin:
        if f.get("net_cash") is not None and f["net_cash"] > 0:
            parts["balance"] = (1.0, [f"Net cash of {money(f['net_cash'], f.get('currency', 'USD'))}"])
        elif f.get("debt_to_equity") is not None:
            de = f["debt_to_equity"]
            parts["balance"] = (scale(de, 2.0, 0.3), [f"Debt is {de:.1f}x shareholder equity"])
        elif m.get("totalDebt/totalEquityQuarterly") is not None:
            de = m["totalDebt/totalEquityQuarterly"]
            parts["balance"] = (scale(de, 2.0, 0.3), [f"Debt is {de:.1f}x shareholder equity"])

    # Shareholder returns
    h, h_notes = [], []
    sc = f.get("shares_change_3y")
    if sc is not None:
        h.append(scale(-sc, -0.05, 0.05))
        h_notes.append("Share count shrinking (buybacks)" if sc < -0.005 else
                       ("Share count growing (dilution)" if sc > 0.005 else "Share count flat"))
    if f.get("dividends_paid") or f.get("buybacks") or val["div_yield"]:
        h.append(1.0)
        if val["div_yield"]:
            h_notes.append(f"Dividend yield {pct(val['div_yield'], 1, signed=False)}")
    elif f:
        h.append(0.0)
    if _avg(h) is not None:
        parts["holder"] = (_avg(h), h_notes)

    # Analysts
    if a.get("score"):
        parts["analysts"] = (scale(a["score"], 2.5, 4.6),
                             [f"{a['analysts']} analysts, consensus {a['label']}"
                              + (f", {round(a['buy_pct'] * 100)}% rate it a buy" if a.get("buy_pct") is not None else "")])

    # Recent Korvus reads
    reads = s.get("_reads")
    tone = catalyst_tone(reads or [])
    if tone is not None:
        parts["catalysts"] = ((tone + 1) / 2, [f"{len(reads)} filing read(s) in 45 days, net "
                                               + ("bullish" if tone > 0.15 else "bearish" if tone < -0.15 else "mixed")])

    present = {k: v for k, v in parts.items() if v[0] is not None}
    wsum = sum(WEIGHTS[k] for k in present)
    coverage = wsum / 100.0
    score = (sum(WEIGHTS[k] * v[0] for k, v in present.items()) / wsum * 100.0) if wsum else None
    out_parts = [{"key": k, "label": PART_LABELS[k], "weight": WEIGHTS[k],
                  "value": (round(present[k][0] * 100) if k in present else None),
                  "notes": (present[k][1] if k in present else ["No data yet"])} for k in WEIGHTS]
    if score is None or coverage < 0.40:
        tier, read = "Building", "neut"
    else:
        tier = ("Top rated" if score >= 75 else "Strong" if score >= 62 else "Solid" if score >= 50
                else "Mixed" if score >= 38 else "Weak")
        ttone = s.get("trend_tone")
        if score >= 62 and ttone != "bear":
            read = "bull"
        elif score < 38 or (ttone == "bear" and score < 50):
            read = "bear"
        else:
            read = "neut"
    return {"score": (round(score, 1) if score is not None else None), "tier": tier, "read": read,
            "coverage": round(coverage, 2), "parts": out_parts, "val": val}


TIER_ORDER = ["Weak", "Mixed", "Solid", "Strong", "Top rated"]


def diff_events(old: dict, new: dict) -> list:
    """What changed for this stock that a holder would want to hear about.
    Returns [(kind, tone, title, detail)]. Quiet on first fill (no 'old')."""
    ev = []
    if not old:
        return ev
    sym = new.get("symbol") or old.get("symbol")
    ot, nt = old.get("trend"), new.get("trend")
    if ot and nt and ot != nt and ot != "Not enough history":
        ev.append(("trend", new.get("trend_tone") or "neut", f"{sym} trend: {ot} → {nt}",
                   f"Price {new.get('price')} vs 50-day {_r(new.get('ma50'), 2)} and 200-day {_r(new.get('ma200'), 2)}."))
    o50, o200, n50, n200 = old.get("ma50"), old.get("ma200"), new.get("ma50"), new.get("ma200")
    if all(x is not None for x in (o50, o200, n50, n200)):
        if o50 <= o200 and n50 > n200:
            ev.append(("cross", "bull", f"{sym} golden cross",
                       "The 50-day average moved above the 200-day, a classic long-term uptrend signal."))
        elif o50 >= o200 and n50 < n200:
            ev.append(("cross", "bear", f"{sym} death cross",
                       "The 50-day average fell below the 200-day, a classic long-term downtrend signal."))
    op, np_, ohi, olo = old.get("price"), new.get("price"), old.get("hi52"), old.get("lo52")
    if op and np_ and ohi and np_ > ohi and op <= ohi:
        ev.append(("high", "bull", f"{sym} new 52-week high", f"Closed at {np_}."))
    if op and np_ and olo and np_ < olo and op >= olo:
        ev.append(("low", "bear", f"{sym} new 52-week low", f"Closed at {np_}."))
    otr, ntr = old.get("tier"), new.get("tier")
    if otr in TIER_ORDER and ntr in TIER_ORDER and otr != ntr:
        up = TIER_ORDER.index(ntr) > TIER_ORDER.index(otr)
        ev.append(("tier", "bull" if up else "bear", f"{sym} Compass tier: {otr} → {ntr}",
                   f"Score {old.get('score')} → {new.get('score')}."))
    oa, na = (old.get("analysts") or {}).get("label"), (new.get("analysts") or {}).get("label")
    if oa and na and oa != na:
        order = ["Strong sell", "Sell", "Hold", "Buy", "Strong buy"]
        up = order.index(na) > order.index(oa) if (na in order and oa in order) else True
        ev.append(("analysts", "bull" if up else "bear", f"{sym} analyst consensus: {oa} → {na}",
                   f"{(new.get('analysts') or {}).get('analysts')} analysts covering."))
    return ev


def fund_event(sym: str, f: dict, filing: dict) -> tuple:
    cur = f.get("currency", "USD")
    if f.get("q_revenue") is not None and f.get("q_end") and f["q_end"] >= (f.get("fy_end") or ""):
        g = f.get("q_revenue_yoy")
        title = f"{sym} new quarter: revenue {money(f['q_revenue'], cur)}" + (f" ({pct(g)} YoY)" if g is not None else "")
        tone = "bull" if (g or 0) > 0.03 else "bear" if (g or 0) < -0.03 else "neut"
    else:
        g = f.get("revenue_yoy")
        title = f"{sym} new fiscal year: revenue {money(f.get('revenue'), cur)}" + (f" ({pct(g)} YoY)" if g is not None else "")
        tone = "bull" if (g or 0) > 0.03 else "bear" if (g or 0) < -0.03 else "neut"
    bits = []
    if f.get("net_margin") is not None:
        bits.append(f"net margin {pct(f['net_margin'], 0, signed=False)}")
    if f.get("fcf") is not None:
        bits.append(f"free cash flow {money(f['fcf'], cur)}")
    detail = (f"From the {filing.get('form', 'filing')} filed {filing.get('filed', '')}. " + ", ".join(bits)).strip()
    return ("fundamentals", tone, title, detail)


def rescore(symbols: Optional[list] = None, emit: bool = True) -> int:
    conn = db()
    syms = symbols or active_symbols(conn, kind="")
    n = 0
    for sym in syms:
        s = load_stock(sym, conn)
        if not s:
            continue
        if s.get("kind") == "etf":      # index funds: trend only, never ranked
            save_fields(sym, {"score": None, "tier": "Index fund", "read": s.get("trend_tone") or "neut",
                              "coverage": None, "parts": [], "score_utc": iso()}, conn)
            continue
        s["_reads"] = korvus_reads(sym)
        res = score_stock(s)
        new = dict(s, score=res["score"], tier=res["tier"], read=res["read"])
        if emit and s.get("score_utc"):
            for kind, tone, title, detail in diff_events(
                    {"symbol": sym, "tier": s.get("tier"), "score": s.get("score")},
                    {"symbol": sym, "tier": new["tier"], "score": new["score"]}):
                add_event(sym, kind, tone, title, detail, conn=conn)
        save_fields(sym, {"score": res["score"], "tier": res["tier"], "read": res["read"],
                          "coverage": res["coverage"], "parts": res["parts"], "score_utc": iso()}, conn)
        n += 1
    conn.commit()
    conn.close()
    return n


# ----------------------------------------------------------------------------
# JOBS (each walks the stalest names first and stops on a provider limit)
# ----------------------------------------------------------------------------
_stop = threading.Event()


def _last_session_date() -> str:
    """The most recent US trading date whose close should be in by now."""
    n = et_now()
    d = n.date()
    if n.hour < 17:          # closes land after ~4:15 PM ET
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def job_prices(limit: Optional[int] = None, verbose: bool = True) -> int:
    if not FMP_KEY:
        status_set("prices", error="FMP_KEY missing", at=iso())
        return 0
    want = _last_session_date()
    conn = db()
    rows = conn.execute("""SELECT symbol FROM stocks WHERE active=1 AND
                           (price_date IS NULL OR price_date < ?) AND
                           (price_utc IS NULL OR price_utc < ?)
                           ORDER BY price_utc IS NOT NULL, price_utc""",
                        (want, iso(utcnow() - dt.timedelta(hours=3)))).fetchall()
    conn.close()
    syms = [r["symbol"] for r in rows][:limit] if limit else [r["symbol"] for r in rows]
    done = fails = 0
    for sym in syms:
        if _stop.is_set():
            break
        try:
            hist = fetch_history(sym)
        except RuntimeError as e:
            status_set("prices", error=str(e), at=iso(), done=done)
            print(f"  [compass] prices paused: {e}")
            break
        except PermissionError as e:
            status_set("prices", error=str(e)[:160], at=iso(), done=done)
            print(f"  [compass] prices blocked: {e}")
            break
        except Exception as e:
            hist = []
            print(f"  [compass] {sym} history error: {e}")
        time.sleep(FMP_GAP)
        conn = db()
        if not hist:
            fails += 1
            save_fields(sym, {"price_utc": iso()}, conn)
            conn.commit()
            conn.close()
            continue
        stats = history_stats(hist)
        old = load_stock(sym, conn)
        fields = {k: stats.get(k) for k in ("price", "price_date", "chg_1d", "ret_1m", "ret_3m", "ret_6m",
                                            "ret_1y", "ret_5y", "ma50", "ma200", "ma50_slope", "hi52", "lo52",
                                            "pos52", "drawdown", "vol_1y", "trend", "trend_tone", "chart",
                                            "chart5", "spark")}
        fields["price_utc"] = iso()
        if old and old.get("price") and old.get("kind") != "etf":
            for kind, tone, title, detail in diff_events(old, dict(old, **fields, symbol=sym)):
                if kind in ("trend", "cross", "high", "low"):
                    add_event(sym, kind, tone, title, detail, conn=conn)
        save_fields(sym, fields, conn)
        conn.commit()
        conn.close()
        done += 1
    status_set("prices", at=iso(), done=done, failed=fails, session=want, pending=max(0, len(syms) - done - fails))
    if verbose and syms:
        print(f"  [compass] prices: {done} updated, {fails} empty (session {want})")
    return done


_filings_lock = threading.Lock()


def job_filings(limit: Optional[int] = None, verbose: bool = True, symbols: Optional[list] = None) -> int:
    """Submissions sweep: new filings become updates; a new periodic report
    (or 35-day-old data) triggers a fresh companyfacts read. `symbols` runs it
    right now for those names (the live feed hook uses that)."""
    with _filings_lock:
        return _job_filings(limit, verbose, symbols)


def _job_filings(limit, verbose, symbols) -> int:
    conn = db()
    if symbols:
        marks = ",".join("?" * len(symbols))
        rows = conn.execute(f"SELECT symbol, cik FROM stocks WHERE active=1 AND cik IS NOT NULL "
                            f"AND symbol IN ({marks}) ORDER BY symbol", list(symbols)).fetchall()
    else:
        rows = conn.execute("""SELECT symbol, cik FROM stocks WHERE active=1 AND cik IS NOT NULL AND
                               (subs_utc IS NULL OR subs_utc < ?) ORDER BY subs_utc IS NOT NULL, subs_utc""",
                            (iso(utcnow() - dt.timedelta(hours=6)),)).fetchall()
    conn.close()
    rows = rows[:limit] if limit else rows
    seen_cik, n_new, n_fund = {}, 0, 0
    for r in rows:
        if _stop.is_set():
            break
        sym, cik = r["symbol"], int(r["cik"])
        conn = db()
        old = load_stock(sym, conn)
        if cik in seen_cik:          # share classes: GOOG rides GOOGL's read
            src = load_stock(seen_cik[cik], conn) or {}
            save_fields(sym, {"filings": src.get("filings"), "fund": src.get("fund"),
                              "fund_utc": src.get("fund_utc"), "fund_accn": src.get("fund_accn"),
                              "subs_utc": iso()}, conn)
            conn.commit()
            conn.close()
            continue
        conn.close()
        subs = fetch_submissions(cik)
        if not subs:
            continue
        filings = parse_submissions(subs)
        old_accns = {x.get("accn") for x in (old.get("filings") or [])} if old else set()
        conn = db()
        first_fill = not (old and old.get("filings"))
        for fl in filings:
            # first_seen() also records the filing, so the live feed hook and
            # this sweep never announce the same one twice
            fresh = first_seen(fl["accn"], sym, conn)
            if fresh and not first_fill and fl["accn"] not in old_accns:
                add_event(sym, "filing", "neut", f"{sym} filed a {fl['form']}: {fl['label']}",
                          f"Filed {fl['filed']}." + (f" Items {fl['items']}." if fl.get("items") else ""),
                          fl["url"], conn=conn)
                n_new += 1
        save_fields(sym, {"filings": filings, "subs_utc": iso()}, conn)
        conn.commit()
        conn.close()

        latest_periodic = next((x for x in filings if x["form"] in PERIODIC), None)
        need = (not old or not old.get("fund") or age_hours(old.get("fund_utc") or "") > 35 * 24
                or (latest_periodic and latest_periodic["accn"] != (old or {}).get("fund_accn")))
        if need:
            facts = fetch_companyfacts(cik)
            f = parse_companyfacts(facts) if facts else {}
            conn = db()
            if f:
                accn = latest_periodic["accn"] if latest_periodic else ""
                if (old and old.get("fund") and latest_periodic and accn != old.get("fund_accn")
                        and old.get("fund_accn")):
                    kind, tone, title, detail = fund_event(sym, f, latest_periodic)
                    add_event(sym, kind, tone, title, detail, latest_periodic["url"], conn=conn)
                save_fields(sym, {"fund": f, "fund_utc": iso(), "fund_accn": accn}, conn)
                n_fund += 1
            else:
                save_fields(sym, {"fund_utc": iso()}, conn)
            conn.commit()
            conn.close()
        seen_cik[cik] = sym
    if not symbols:
        status_set("filings", at=iso(), checked=len(rows), new_filings=n_new, fundamentals=n_fund)
    if verbose and rows:
        print(f"  [compass] filings: {len(rows)} checked, {n_new} new, {n_fund} fundamentals refreshed")
    return len(rows)


def job_analysts(limit: Optional[int] = None, verbose: bool = True) -> int:
    if not FINNHUB_KEY:
        status_set("analysts", error="FINNHUB_KEY missing", at=iso())
        return 0
    conn = db()
    rows = conn.execute("""SELECT symbol FROM stocks WHERE active=1 AND COALESCE(kind,'stock')='stock' AND
                           (analyst_utc IS NULL OR analyst_utc < ?) ORDER BY analyst_utc IS NOT NULL, analyst_utc""",
                        (iso(utcnow() - dt.timedelta(days=7)),)).fetchall()
    conn.close()
    syms = [r["symbol"] for r in rows]
    syms = syms[:limit] if limit else syms[:120]       # a slice per tick keeps Finnhub calm
    done = 0
    for sym in syms:
        if _stop.is_set():
            break
        a, m = fetch_analysts(sym)
        conn = db()
        old = load_stock(sym, conn)
        if old and old.get("analysts") and a:
            for kind, tone, title, detail in diff_events(old, dict(old, analysts=a, symbol=sym)):
                if kind == "analysts":
                    add_event(sym, kind, tone, title, detail, conn=conn)
        save_fields(sym, {"analysts": a or (old or {}).get("analysts"), "metrics": m or (old or {}).get("metrics"),
                          "analyst_utc": iso()}, conn)
        conn.commit()
        conn.close()
        done += 1
    status_set("analysts", at=iso(), done=done, pending=max(0, len(rows) - done))
    if verbose and syms:
        print(f"  [compass] analysts: {done} refreshed")
    return done


def prune_events(days: int = 120):
    conn = db()
    conn.execute("DELETE FROM events WHERE ts_utc < ?", (iso(utcnow() - dt.timedelta(days=days)),))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# LIVE LAYERS: the EDGAR feed hook (always on) and live prices (COMPASS_LIVE)
# ----------------------------------------------------------------------------
def market_open_now() -> bool:
    n = et_now()
    mins = n.hour * 60 + n.minute
    return n.weekday() < 5 and 9 * 60 + 30 <= mins <= 16 * 60 + 5


def parse_quotes(data) -> dict:
    """FMP batch-quote rows -> {SYM: (price, day change as a fraction)}."""
    out = {}
    for x in (data if isinstance(data, list) else []):
        sym = norm_symbol(x.get("symbol", ""))
        price = x.get("price")
        if not sym or price is None:
            continue
        chg = x.get("changePercentage", x.get("changesPercentage"))
        try:
            out[sym] = (float(price), (float(chg) / 100.0 if chg is not None else None))
        except (TypeError, ValueError):
            continue
    return out


def fetch_quotes(symbols: list) -> dict:
    """Current quotes for many symbols in a handful of calls (100 per call)."""
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = ",".join(symbols[i:i + 100])
        data = None
        for url, params in (("https://financialmodelingprep.com/stable/batch-quote", {"symbols": chunk}),
                            (f"https://financialmodelingprep.com/api/v3/quote/{chunk}", {})):
            try:
                data = _fmp_get(url, params)
            except PermissionError:
                data = None
            if data:
                break
        out.update(parse_quotes(data))
        time.sleep(0.15)
    return out


def job_live(verbose: bool = False) -> int:
    """Display-only live prices. Scores and trend keep using daily closes."""
    syms = active_symbols(kind="")
    try:
        q = fetch_quotes(syms)
    except RuntimeError as e:
        status_set("live", error=str(e), at=iso())
        return 0
    if not q:
        status_set("live", error="no quotes returned", at=iso())
        return 0
    now = iso()
    conn = db()
    for sym, (price, chg) in q.items():
        conn.execute("UPDATE stocks SET live_price=?, live_chg=?, live_utc=? WHERE symbol=?", (price, chg, now, sym))
    conn.commit()
    conn.close()
    status_set("live", at=now, quotes=len(q), error=None)
    return len(q)


def match_feed(entries: list, by_cik: dict) -> list:
    """EDGAR feed entries -> [(symbol, entry)] for companies on the board."""
    out = []
    for e in entries:
        for sym in by_cik.get(int(e.get("cik") or 0), []):
            out.append((sym, e))
    return out


def job_feed(verbose: bool = True, fetch=None) -> int:
    """Poll EDGAR's latest-filings feed. A filing from a board company becomes
    an update right away and that company's numbers are re-read."""
    import korvus_edgar as edgar
    conn = db()
    by_cik = {}
    for r in conn.execute("SELECT symbol, cik FROM stocks WHERE active=1 AND cik IS NOT NULL").fetchall():
        by_cik.setdefault(int(r["cik"]), []).append(r["symbol"])
    conn.close()
    fetch = fetch or (lambda form: edgar.parse_feed(edgar._get(edgar.FEED_URL.format(form=form)) or ""))
    hits = []
    for form in FEED_FORMS:
        try:
            hits += match_feed([e for e in fetch(form) if e.get("form") in FORM_NAMES], by_cik)
        except Exception as e:
            print(f"  [compass] feed {form} error: {e}")
    seeded = meta_get("feed_seeded") == "1"
    conn = db()
    fresh = []
    for sym, e in hits:
        if first_seen(e.get("accession", ""), sym, conn) and seeded:
            items = ", ".join(e.get("items") or [])
            add_event(sym, "filing", "neut", f"{sym} filed a {e['form']}: {FORM_NAMES[e['form']]}",
                      "Just accepted by the SEC." + (f" Items {items}." if items else ""), e.get("url", ""), conn=conn)
            fresh.append(sym)
    conn.commit()
    conn.close()
    if not seeded:
        meta_set("feed_seeded", "1")      # first poll only learns what is already out
    status_set("feed", at=iso(), matched=len(hits), new=len(fresh), error=None)
    if fresh:
        syms = sorted(set(fresh))
        if verbose:
            print(f"  [compass] live filing(s): {', '.join(syms)}")
        job_filings(symbols=syms, verbose=False)
        rescore(syms)
        try:
            import korvus_compass_agent as agent
            agent.job_briefs(symbols=syms, verbose=False)
        except Exception as e:
            print(f"  [compass] brief refresh error: {e}")
    return len(fresh)


def tick(verbose: bool = True, backfill: bool = False):
    if age_hours(meta_get("universe_asof")) > 24 * 7 or not active_symbols():
        refresh_universe(verbose)
    job_prices(verbose=verbose)
    job_filings(verbose=verbose)
    n = rescore()
    status_set("score", at=iso(), scored=n)
    import korvus_compass_agent as agent
    agent.fill_rule_briefs()                       # every page has words from the first pass
    job_analysts(verbose=verbose, limit=(100000 if backfill else None))
    n = rescore()
    status_set("score", at=iso(), scored=n)
    agent.job_briefs(limit=(100000 if backfill else None), verbose=verbose)
    prune_events()


def _loop(name: str, seconds_fn, fn):
    def run():
        while not _stop.is_set():
            try:
                fn()
            except Exception as e:
                print(f"  [compass:{name}] error: {e}")
                try:
                    status_set(name, error=f"{e.__class__.__name__}: {str(e)[:160]}", at=iso())
                except Exception:
                    pass
            _stop.wait(seconds_fn())
    t = threading.Thread(target=run, name=f"compass-{name}", daemon=True)
    t.start()
    return t


def claim_sec_budget():
    """This process's slice of the SEC's 10 requests a second."""
    import korvus_edgar as edgar
    edgar.MIN_GAP = 1.0 / SEC_RPS


def run_forever():
    import korvus_edgar as edgar
    db_init()
    claim_sec_budget()
    meta_set("started_utc", iso())
    status_set("system", at=iso(), error=None, live=LIVE, sec_rps=SEC_RPS)
    print(f"  [compass] crawler started: SEC budget {SEC_RPS:g}/s, live prices {'ON' if LIVE else 'off'}, "
          f"filing feed every 60s, main pass every {TICK_SECONDS}s")
    _loop("heartbeat", lambda: 15, lambda: meta_set("heartbeat", iso()))
    _loop("feed", lambda: 60 if edgar._edgar_open_now() else 300, job_feed)
    if LIVE:
        first = [True]

        def live_job():
            if market_open_now() or first[0]:      # one pull at start so tiles are never blank
                first[0] = False
                job_live()
        _loop("live", lambda: LIVE_SECONDS if market_open_now() else 300, live_job)
    first = True
    while not _stop.is_set():
        try:
            # A mostly empty board gets one full fill, start to finish, so every
            # stock page is ready as soon as possible; after that, small passes.
            empty = first and (admin_status().get("counts") or {}).get("with_analysts", 0) < 0.5 * max(1, len(active_symbols()))
            first = False
            tick(verbose=True, backfill=empty)
        except Exception as e:
            print(f"  [compass] tick error: {e}")
            try:
                status_set("system", error=str(e)[:200], at=iso())
            except Exception:
                pass
        _stop.wait(TICK_SECONDS)


# ----------------------------------------------------------------------------
# READ API (the web server calls these; read-only, never touches a provider)
# ----------------------------------------------------------------------------
def _ro():
    if not os.path.exists(DB_PATH):
        return None
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _status(c) -> dict:
    try:
        r = c.execute("SELECT v FROM meta WHERE k='status'").fetchone()
        st = json.loads(r["v"]) if r else {}
    except Exception:
        st = {}
    return st


def _live_fields(s: dict) -> dict:
    """Live price for display, only while the switch is on and the quote is fresh."""
    if not LIVE or s.get("live_price") is None or age_hours(s.get("live_utc") or "") > 0.25:
        return {}
    return {"lp": s["live_price"], "lc": s.get("live_chg"), "lt": s.get("live_utc")}


def api_list() -> dict:
    c = _ro()
    if not c:
        return {"stocks": [], "etfs": [], "status": {}, "building": True, "disclaimer": DISCLAIMER}
    rows = c.execute("SELECT * FROM stocks WHERE active=1").fetchall()
    out, etfs = [], []
    for r in rows:
        s = row_to_dict(r)
        if s.get("kind") == "etf":
            etfs.append(dict({"s": s["symbol"], "n": s["name"], "note": s.get("note") or "",
                              "g": ("us" if s["symbol"] in US_FUNDS else "world"), "p": s["price"],
                              "d1": s["chg_1d"], "r1y": s["ret_1y"], "r5y": s.get("ret_5y"), "pos": s["pos52"],
                              "dd": s["drawdown"], "tr": s["trend"], "tt": s["trend_tone"],
                              "sp_": s.get("spark") or [], "pd": s["price_date"]}, **_live_fields(s)))
            continue
        f, a = s.get("fund") or {}, s.get("analysts") or {}
        val = valuation(s)
        parts = {p["key"]: p["value"] for p in (s.get("parts") or [])}
        out.append(dict({
            "s": s["symbol"], "n": s["name"], "sec": s["sector"], "ind": s["industry"],
            "sp": s["in_sp"], "nd": s["in_ndx"], "gl": s.get("in_global") or 0,
            "co": s.get("country") or "", "cn": COUNTRY_NAMES.get(s.get("country") or "", s.get("country") or ""),
            "p": s["price"], "d1": s["chg_1d"], "r3": s["ret_3m"], "r1y": s["ret_1y"],
            "dd": s["drawdown"], "pos": s["pos52"], "tr": s["trend"], "tt": s["trend_tone"],
            "sp_": s.get("spark") or [],
            "sc": s["score"], "ti": s["tier"], "rd": s["read"], "cov": s["coverage"],
            "pt": parts,
            "rg": f.get("revenue_cagr3"), "ry": (f.get("q_revenue_yoy") if f.get("q_revenue_yoy") is not None
                                                  else f.get("revenue_yoy")),
            "nm": f.get("net_margin"), "fm": f.get("fcf_margin"),
            "pe": val["pe"], "fy": val["fcf_yield"], "dy": val["div_yield"], "mc": val["mcap"],
            "an": a.get("label"), "as": a.get("score"), "ac": a.get("analysts"),
            "hl": (s.get("brief") or {}).get("headline"),
            "pd": s["price_date"],
        }, **_live_fields(s)))
    st = _status(c)
    asof = c.execute("SELECT MAX(price_date) m FROM stocks").fetchone()["m"]
    c.close()
    order = list(ETFS)
    etfs.sort(key=lambda x: order.index(x["s"]) if x["s"] in order else 99)
    return {"stocks": out, "etfs": etfs, "status": st, "asof": asof, "weights": WEIGHTS, "labels": PART_LABELS,
            "live": bool(LIVE and any("lp" in x for x in out)),
            "building": not any(x["sc"] is not None for x in out), "disclaimer": DISCLAIMER}


def api_stock(symbol: str) -> Optional[dict]:
    symbol = norm_symbol(symbol)
    c = _ro()
    if not c:
        return None
    r = c.execute("SELECT * FROM stocks WHERE symbol=?", (symbol,)).fetchone()
    if not r:
        c.close()
        return None
    s = row_to_dict(r)
    ev = [dict(x) for x in c.execute(
        "SELECT symbol, ts_utc, kind, tone, title, detail, url FROM events WHERE symbol=? ORDER BY ts_utc DESC LIMIT 30",
        (symbol,)).fetchall()]
    c.close()
    s["val"] = valuation(s)
    s["reads"] = korvus_reads(symbol) if s.get("kind") != "etf" else []
    s["events"] = ev
    s["live"] = _live_fields(s)
    s["country_name"] = COUNTRY_NAMES.get(s.get("country") or "", s.get("country") or "")
    s["disclaimer"] = DISCLAIMER
    s["weights"] = WEIGHTS
    return s


def api_events(limit: int = 80, symbol: str = "") -> list:
    c = _ro()
    if not c:
        return []
    if symbol:
        rows = c.execute("SELECT * FROM events WHERE symbol=? ORDER BY ts_utc DESC, id DESC LIMIT ?",
                         (norm_symbol(symbol), limit)).fetchall()
    else:
        rows = c.execute("SELECT * FROM events ORDER BY ts_utc DESC, id DESC LIMIT ?", (limit,)).fetchall()
    out = [dict(x) for x in rows]
    c.close()
    return out


def admin_status() -> dict:
    """Everything the BlackCrown admin shows about this crawler. Read-only."""
    c = _ro()
    if not c:
        return {"running": False, "error": "no compass database yet"}
    g = lambda q, a=(): c.execute(q, a).fetchone()[0]
    hb = c.execute("SELECT v FROM meta WHERE k='heartbeat'").fetchone()
    started = c.execute("SELECT v FROM meta WHERE k='started_utc'").fetchone()
    age = age_hours(hb["v"]) * 3600 if hb else None
    stock = "active=1 AND COALESCE(kind,'stock')='stock'"
    since = iso(utcnow() - dt.timedelta(hours=24))
    by = {}
    for r in c.execute(f"SELECT brief FROM stocks WHERE {stock} AND brief IS NOT NULL").fetchall():
        try:
            k = (json.loads(r["brief"]) or {}).get("by") or "rules"
        except Exception:
            k = "rules"
        by[k] = by.get(k, 0) + 1
    out = {
        "running": bool(age is not None and age < 90),
        "heartbeat_age_s": (round(age, 1) if age is not None else None),
        "started_utc": started["v"] if started else None,
        "live_prices": LIVE, "sec_rps": SEC_RPS,
        "status": _status(c),
        "counts": {
            "stocks": g(f"SELECT COUNT(*) FROM stocks WHERE {stock}"),
            "funds": g("SELECT COUNT(*) FROM stocks WHERE active=1 AND kind='etf'"),
            "international": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND COALESCE(country,'US') NOT IN ('US','')"),
            "countries": g(f"SELECT COUNT(DISTINCT country) FROM stocks WHERE {stock} AND COALESCE(country,'')!=''"),
            "priced": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND price IS NOT NULL"),
            "with_financials": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND fund IS NOT NULL"),
            "with_analysts": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND analysts IS NOT NULL AND analysts NOT IN ('{{}}','null')"),
            "scored": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND score IS NOT NULL AND tier!='Building'"),
            "bullish": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND read='bull' AND tier!='Building'"),
            "bearish": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND read='bear' AND tier!='Building'"),
            "top_rated": g(f"SELECT COUNT(*) FROM stocks WHERE {stock} AND tier='Top rated'"),
            "briefs": sum(by.values()), "briefs_by": by,
            "updates_24h": g("SELECT COUNT(*) FROM events WHERE ts_utc>=?", (since,)),
            "filings_24h": g("SELECT COUNT(*) FROM events WHERE kind='filing' AND ts_utc>=?", (since,)),
        },
        "price_asof": g("SELECT MAX(price_date) FROM stocks"),
        "events": [dict(x) for x in c.execute(
            "SELECT symbol, ts_utc, kind, tone, title, detail FROM events ORDER BY ts_utc DESC, id DESC LIMIT 25")],
        "top": [dict(x) for x in c.execute(
            f"SELECT symbol, name, score, tier, read, trend FROM stocks WHERE {stock} AND score IS NOT NULL "
            "ORDER BY score DESC LIMIT 8")],
    }
    c.close()
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def probe(symbol: str):
    symbol = norm_symbol(symbol)
    print(f"Probing every Compass source for {symbol}\n")
    print("FMP_KEY", "set" if FMP_KEY else "MISSING", "| FINNHUB_KEY", "set" if FINNHUB_KEY else "MISSING")
    try:
        sp = _fmp_constituents("sp500")
        print(f"  universe  FMP S&P list: {len(sp)} names" + ("" if sp else " (will use Wikipedia)"))
        if not sp:
            w = _wiki_constituents("List_of_S%26P_500_companies")
            print(f"  universe  Wikipedia S&P list: {len(w)} names")
    except Exception as e:
        print(f"  universe  error: {e}")
    try:
        h = fetch_history(symbol)
        st = history_stats(h)
        print(f"  prices    {len(h)} daily closes, last {st.get('price_date')} @ {st.get('price')}, "
              f"trend: {st.get('trend')}")
    except Exception as e:
        print(f"  prices    FAILED: {e}")
    cik = _sec_ticker_map().get(symbol)
    print(f"  SEC CIK   {cik}")
    if cik:
        subs = fetch_submissions(int(cik))
        fl = parse_submissions(subs) if subs else []
        print(f"  filings   {len(fl)} notable, latest: " + (f"{fl[0]['form']} {fl[0]['filed']}" if fl else "none"))
        facts = fetch_companyfacts(int(cik))
        f = parse_companyfacts(facts) if facts else {}
        print(f"  facts     revenue {money(f.get('revenue'), f.get('currency', 'USD'))} "
              f"(3y {pct(f.get('revenue_cagr3'))}), net margin {pct(f.get('net_margin'), 0, False)}, "
              f"FCF {money(f.get('fcf'))}, latest Q {f.get('q_end')} {pct(f.get('q_revenue_yoy'))} YoY")
    a, m = fetch_analysts(symbol)
    print(f"  analysts  {a.get('label')} ({a.get('analysts')} analysts) | metrics keys: {len(m)}")
    try:
        q = fetch_quotes([symbol, "SPY", "QQQ"])
        print(f"  live      batch quotes: {q}  (shown on the page only when COMPASS_LIVE=1)")
    except Exception as e:
        print(f"  live      FAILED: {e}")
    try:
        import korvus_compass_agent as agent
        print(f"  agent     {agent.describe()}")
    except Exception as e:
        print(f"  agent     error: {e}")


def print_board(n: int = 20):
    d = api_list()
    rows = sorted([x for x in d["stocks"] if x["sc"] is not None], key=lambda x: -x["sc"])[:n]
    print(f"Compass board (prices as of {d.get('asof')}), top {len(rows)}:")
    for i, x in enumerate(rows, 1):
        print(f"{i:>3}. {x['s']:<6} {x['sc']:>5.1f} {x['ti']:<10} {x['rd']:<4} {x['tr'] or '':<22} "
              f"1y {pct(x['r1y']):>6}  {x['n'][:34]}")


def main():
    ap = argparse.ArgumentParser(description="Korvus Compass: long-term investing board crawler")
    ap.add_argument("--probe", metavar="SYM")
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--stock", metavar="SYM")
    ap.add_argument("--top", type=int, nargs="?", const=20)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="fill the whole board now (prices, SEC, analysts, briefs)")
    a = ap.parse_args()
    db_init()
    claim_sec_budget()
    if a.backfill:
        t0 = time.time()
        print("Filling the whole Compass board. Prices take a few minutes, SEC data about five,")
        print("analyst data about thirty-five, then the analyst agent writes every brief.")
        tick(verbose=True, backfill=True)
        if LIVE:
            job_live()
        d = admin_status()["counts"]
        print(f"Done in {int((time.time() - t0) / 60)} min: {d['scored']} of {d['stocks']} stocks scored, "
              f"{d['briefs']} briefs ({d['briefs_by']}).")
    elif a.probe:
        probe(a.probe)
    elif a.universe:
        refresh_universe()
    elif a.stock:
        sym = norm_symbol(a.stock)
        if not load_stock(sym):
            refresh_universe()
        conn = db()
        conn.execute("UPDATE stocks SET price_utc=NULL, price_date=NULL, subs_utc=NULL, analyst_utc=NULL WHERE symbol=?",
                     (sym,))
        conn.commit()
        conn.close()
        hist = fetch_history(sym)
        if hist:
            save_fields(sym, dict(history_stats(hist), price_utc=iso()))
        s = load_stock(sym) or {}
        if s.get("cik"):
            subs = fetch_submissions(int(s["cik"]))
            fl = parse_submissions(subs) if subs else []
            facts = fetch_companyfacts(int(s["cik"]))
            f = parse_companyfacts(facts) if facts else {}
            save_fields(sym, {"filings": fl, "subs_utc": iso(), "fund": f or None, "fund_utc": iso(),
                              "fund_accn": next((x["accn"] for x in fl if x["form"] in PERIODIC), "")})
        an, m = fetch_analysts(sym)
        save_fields(sym, {"analysts": an, "metrics": m, "analyst_utc": iso()})
        rescore([sym], emit=False)
        s = api_stock(sym) or {}
        print(json.dumps({k: s.get(k) for k in ("symbol", "name", "price", "trend", "score", "tier", "read",
                                                 "coverage", "parts", "val")}, indent=2, default=str))
    elif a.once:
        if not active_symbols():
            refresh_universe()
        job_prices(limit=a.limit)
        job_filings(limit=a.limit)
        job_analysts(limit=a.limit)
        print(f"  [compass] scored {rescore()} names")
    elif a.top:
        print_board(a.top)
    elif a.loop:
        run_forever()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
==============================================================================
 KORVUS ENGINE  ·  Phase 1   (by BlackCrownVxJ.LLC)
==============================================================================
 WHAT THIS DOES
   1. Pulls market news from Alpha Vantage (ticker-tagged, with sentiment)
   2. Pulls social chatter from Reddit (finance subreddits)
   3. (X / Twitter is stubbed - flip it on later when you add a key)
   4. Sends each NEW item to Claude (Haiku) for a summary + impact score +
      direction (bull/bear/neutral) + affected instruments + confidence
   5. Saves everything to a local SQLite database (korvus.db)

 This is the 24/7 "brain." In Phase 2 the dashboard reads korvus.db.
 In Phase 4 this same script moves to your server on a schedule.

 HOW TO RUN  (see README.md for the full walkthrough)
   1. pip install -r requirements.txt
   2. copy .env.example to .env and fill in your keys
   3. python korvus_engine.py            # one pass
      python korvus_engine.py --loop      # runs every POLL_MINUTES forever
==============================================================================
"""

import os
import re
import sys
import json
import time
import hashlib
import sqlite3
import korvus_auth as auth   # for high-impact email alerts
import argparse
import datetime as dt
from typing import Optional

# --- third-party libs (installed via requirements.txt) ----------------------
import requests
from dotenv import load_dotenv
import anthropic

# ----------------------------------------------------------------------------
# CONFIG  - loaded from your .env file so secrets never live in the code
# ----------------------------------------------------------------------------
load_dotenv()

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
BENZINGA_KEY        = os.getenv("BENZINGA_KEY", "")
ALPHAVANTAGE_KEY    = os.getenv("ALPHAVANTAGE_KEY", "")
REDDIT_CLIENT_ID    = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET= os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT   = os.getenv("REDDIT_USER_AGENT", "korvus-engine/0.1 by BlackCrownVxJ")

# Which news provider to use: "benzinga" (free Basic), "benzinga_premium", or "alphavantage"
NEWS_PROVIDER = os.getenv("NEWS_PROVIDER", "benzinga")

# Which Claude model does the scoring. Set CLAUDE_MODEL in .env to switch anytime.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")

# How often the --loop mode runs (minutes)
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "5"))

# The instruments Korvus tracks - Claude maps each news item onto THESE.
# Comprehensive cross-market universe so ANY story gets tagged with the right
# market(s) it affects, not just index futures. Grouped for readability.
WATCHED_INSTRUMENTS = [
    # --- US equity index futures (+ cash index / ETF equivalents) ---
    "MNQ", "NQ", "QQQ",        # Nasdaq-100
    "MES", "ES", "SPY",        # S&P 500
    "MYM", "YM", "DIA",        # Dow
    "M2K", "RTY", "IWM",       # Russell 2000
    "NKD",                     # Nikkei 225 future
    "VX", "VIX",               # volatility

    # --- Energy ---
    "CL",   # WTI crude oil
    "BZ",   # Brent crude
    "NG",   # natural gas
    "RB",   # gasoline
    "HO",   # heating oil

    # --- Metals ---
    "GC",   # gold
    "SI",   # silver
    "PL",   # platinum
    "PA",   # palladium
    "HG",   # copper

    # --- Agriculture ---
    "ZC",   # corn
    "ZW",   # wheat
    "ZS",   # soybeans
    "KC",   # coffee
    "SB",   # sugar
    "CT",   # cotton
    "LE",   # live cattle

    # --- Rates / bonds ---
    "ZT",   # 2Y
    "ZF",   # 5Y
    "ZN",   # 10Y T-note
    "ZB",   # 30Y T-bond
    "GE",   # eurodollar / SOFR (short rates)

    # --- FX futures / currency ---
    "DXY",  # US dollar index
    "6E",   # euro
    "6J",   # yen
    "6B",   # british pound
    "6C",   # canadian dollar
    "6A",   # aussie dollar
    "6S",   # swiss franc
    "6M",   # mexican peso

    # --- Crypto ---
    "BTC",  # bitcoin
    "ETH",  # ethereum

    # --- Megacap / index-moving equities ---
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
    "AVGO", "AMD", "NFLX", "JPM", "XOM",
]

# Alpha Vantage news "topics"/tickers to track. Index futures move on big tech,
# the broad market, and macro - so we pull those tickers' news.
AV_TICKERS = ["QQQ", "SPY", "NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA"]

# Reddit subreddits to scan for chatter
SUBREDDITS = ["wallstreetbets", "stocks", "options", "futures", "Daytrading"]

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "korvus.db")


# ----------------------------------------------------------------------------
# DATABASE  - one table, "items". Dead simple and easy to read from the UI.
# ----------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id           TEXT PRIMARY KEY,   -- hash of source+headline (dedupe)
            created_at   TEXT NOT NULL,      -- when WE ingested it (UTC ISO)
            published_at TEXT,               -- when the source published it
            source       TEXT NOT NULL,      -- 'wire' | 'reddit' | 'x'
            source_name  TEXT,               -- e.g. 'Reuters', 'r/options'
            url          TEXT,
            headline     TEXT NOT NULL,
            raw_text     TEXT,               -- original blurb we fed to Claude
            summary      TEXT,               -- Claude's plain-English summary
            impact_desc  TEXT,               -- Claude's richer analysis (detail view)
            impact       TEXT,               -- 'high' | 'med' | 'low'
            direction    TEXT,               -- 'bull' | 'bear' | 'neut'
            instruments  TEXT,               -- JSON list e.g. ["MNQ","MES"]
            confidence   INTEGER,            -- 0-100
            noise        INTEGER DEFAULT 0,  -- 1 = pure non-market junk, hidden from feed
            category     TEXT DEFAULT 'general', -- 'general' | 'forex' (drives the news tab split)
            processed    INTEGER DEFAULT 0   -- 1 once Claude has scored it
        )
    """)
    # Migration: add impact_desc to pre-existing DBs that don't have it yet.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "impact_desc" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN impact_desc TEXT")
        print("  [db] migrated: added impact_desc column")
    if "noise" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN noise INTEGER DEFAULT 0")
        print("  [db] migrated: added noise column")
    if "category" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN category TEXT DEFAULT 'general'")
        print("  [db] migrated: added category column")
    conn.commit()
    conn.close()


def make_id(source: str, headline: str) -> str:
    return hashlib.sha256(f"{source}|{headline}".encode("utf-8")).hexdigest()[:16]


def item_exists(conn, item_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,))
    return cur.fetchone() is not None


def insert_raw_item(conn, item: dict):
    """Insert a freshly-pulled item that hasn't been scored by Claude yet."""
    conn.execute("""
        INSERT OR IGNORE INTO items
        (id, created_at, published_at, source, source_name, url, headline, raw_text, category, processed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        item["id"],
        dt.datetime.now(dt.timezone.utc).isoformat(),
        item.get("published_at"),
        item["source"],
        item.get("source_name"),
        item.get("url"),
        item["headline"],
        item.get("raw_text", ""),
        item.get("category", "general"),
    ))
    conn.commit()


# ----------------------------------------------------------------------------
# SOURCE 1 - NEWS PROVIDER LAYER  (swappable)
# Set NEWS_PROVIDER in .env to one of: "benzinga", "benzinga_premium", "alphavantage"
# Same return shape for all three, so the rest of the engine never changes.
# Upgrading from free Benzinga Basic to premium = change ONE line in .env.
# ----------------------------------------------------------------------------
def fetch_news() -> list[dict]:
    provider = NEWS_PROVIDER.lower().strip()
    if provider == "benzinga":
        return fetch_benzinga(premium=False)
    if provider == "benzinga_premium":
        return fetch_benzinga(premium=True)
    if provider == "alphavantage":
        return fetch_alphavantage_news()
    print(f"  [wire] unknown NEWS_PROVIDER '{NEWS_PROVIDER}' - skipping news")
    return []


# --- Benzinga (Basic free tier now; premium later by flipping NEWS_PROVIDER) -
# Free Basic tier: headline + teaser + link. Get a token at benzinga.com (or
# via AWS Marketplace "Benzinga Basic Financial News API"). Premium uses the
# same endpoint with fuller content once you have a paid token.
def fetch_benzinga(premium: bool = False) -> list[dict]:
    if not BENZINGA_KEY:
        print("  [wire] no BENZINGA_KEY set - skipping")
        return []

    # Benzinga news REST endpoint. tickers= narrows to what you trade.
    url = "https://api.benzinga.com/api/v2/news"
    params = {
        "token": BENZINGA_KEY,
        "tickers": ",".join(AV_TICKERS),
        "pageSize": 50,
        "displayOutput": "full" if premium else "abstract",
        "sort": "created:desc",
    }
    headers = {"Accept": "application/json"}
    out = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        if r.status_code == 401:
            print("  [wire] Benzinga 401 - check BENZINGA_KEY / that your plan covers the news API")
            return []
        data = r.json()
        # Benzinga returns a list of article objects
        articles = data if isinstance(data, list) else data.get("news", data.get("data", []))
        for art in articles:
            headline = (art.get("title") or "").strip()
            if not headline:
                continue
            # 'teaser' on basic; 'body' (HTML) on premium - strip tags for Claude
            body = art.get("teaser") or art.get("body") or ""
            body = re.sub(r"<[^>]+>", " ", body)  # remove any HTML
            out.append({
                "id": make_id("wire", headline),
                "source": "wire",
                "source_name": "Benzinga",
                "url": art.get("url"),
                "published_at": art.get("created"),
                "headline": headline,
                "raw_text": body.strip()[:1200],
            })
    except Exception as e:
        print(f"  [wire] Benzinga error: {e}")
    print(f"  [wire] Benzinga pulled {len(out)} articles ({'premium' if premium else 'basic'})")
    return out


# --- Alpha Vantage (kept as a free fallback / alternative) ------------------
# Docs: https://www.alphavantage.co/documentation/  (NEWS_SENTIMENT)
def fetch_alphavantage_news() -> list[dict]:
    if not ALPHAVANTAGE_KEY:
        print("  [wire] no ALPHAVANTAGE_KEY set - skipping")
        return []

    tickers = ",".join(AV_TICKERS)
    url = (
        "https://www.alphavantage.co/query"
        f"?function=NEWS_SENTIMENT&tickers={tickers}"
        f"&sort=LATEST&limit=50&apikey={ALPHAVANTAGE_KEY}"
    )
    out = []
    try:
        r = requests.get(url, timeout=20)
        data = r.json()
        if "feed" not in data:
            print(f"  [wire] unexpected response: {list(data.keys())} "
                  f"(often a rate-limit note on the free tier)")
            return []
        for art in data["feed"]:
            headline = art.get("title", "").strip()
            if not headline:
                continue
            out.append({
                "id": make_id("wire", headline),
                "source": "wire",
                "source_name": art.get("source", "Newswire"),
                "url": art.get("url"),
                "published_at": art.get("time_published"),
                "headline": headline,
                "raw_text": art.get("summary", "")[:1200],
            })
    except Exception as e:
        print(f"  [wire] error: {e}")
    print(f"  [wire] Alpha Vantage pulled {len(out)} articles")
    return out


# ----------------------------------------------------------------------------
# SOURCE 2 - REDDIT  (free API via OAuth client-credentials)
# Create an app at https://www.reddit.com/prefs/apps  (type: 'script')
# ----------------------------------------------------------------------------
_reddit_token_cache = {"token": None, "expires": 0}


def reddit_token() -> Optional[str]:
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return None
    now = time.time()
    if _reddit_token_cache["token"] and now < _reddit_token_cache["expires"]:
        return _reddit_token_cache["token"]
    try:
        auth = requests.auth.HTTPBasicAuth(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=auth,
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": REDDIT_USER_AGENT},
            timeout=20,
        )
        tok = resp.json().get("access_token")
        _reddit_token_cache["token"] = tok
        _reddit_token_cache["expires"] = now + 3000  # ~50 min
        return tok
    except Exception as e:
        print(f"  [reddit] auth error: {e}")
        return None


def fetch_reddit() -> list[dict]:
    tok = reddit_token()
    if not tok:
        print("  [reddit] no Reddit credentials set - skipping")
        return []

    headers = {"Authorization": f"bearer {tok}", "User-Agent": REDDIT_USER_AGENT}
    out = []
    for sub in SUBREDDITS:
        try:
            r = requests.get(
                f"https://oauth.reddit.com/r/{sub}/hot",
                headers=headers, params={"limit": 15}, timeout=20,
            )
            for child in r.json().get("data", {}).get("children", []):
                p = child.get("data", {})
                # skip stickied mod posts and very low-score noise
                if p.get("stickied") or p.get("score", 0) < 25:
                    continue
                headline = p.get("title", "").strip()
                if not headline:
                    continue
                out.append({
                    "id": make_id("reddit", headline),
                    "source": "reddit",
                    "source_name": f"r/{sub}",
                    "url": "https://reddit.com" + p.get("permalink", ""),
                    "published_at": dt.datetime.fromtimestamp(
                        p.get("created_utc", time.time()), dt.timezone.utc).isoformat(),
                    "headline": headline,
                    "raw_text": (p.get("selftext", "") or "")[:1000],
                })
        except Exception as e:
            print(f"  [reddit] error on r/{sub}: {e}")
    print(f"  [reddit] pulled {len(out)} posts")
    return out


# ----------------------------------------------------------------------------
# SOURCE 3 - X / TWITTER  (stub - turn on later)
# X's API is pay-per-read now; wire it in once you've got a key + budget.
# Keep the same return shape and the rest of the engine just works.
# ----------------------------------------------------------------------------
def fetch_x() -> list[dict]:
    # TODO: implement with your X API bearer token when ready.
    return []


# ----------------------------------------------------------------------------
# SOURCE 4 - FREE RSS NEWS  (no API key needed)
# Supplements Benzinga's free tier, which only serves a static recent window.
# These public financial feeds publish frequently, so they fill the gaps and
# keep the timeline advancing. Parsed with the stdlib (no extra dependency).
# Toggle/extend via RSS_FEEDS below.
# ----------------------------------------------------------------------------
# Each feed: (display_name, url, category). category is "forex" or "general"
# and drives the News-panel tab split on the dashboard.
RSS_FEEDS = [
    ("CNBC Markets",  "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "general"),
    ("CNBC Top",      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362", "general"),
    ("MarketWatch",   "https://feeds.content.dowjones.io/public/rss/mw_topstories", "general"),
    ("MW RealTime",   "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "general"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", "general"),
    ("Investing.com", "https://www.investing.com/rss/news.rss", "general"),
    ("SeekingAlpha",  "https://seekingalpha.com/market_currents.xml", "general"),
    ("InvestingLive", "https://www.investinglive.com/feed", "general"),
    # --- Forex / FX-focused feeds (engine logs+skips any that don't return 200) ---
    ("FXStreet",      "https://www.fxstreet.com/rss/news", "forex"),
    ("ForexLive",     "https://www.forexlive.com/feed", "forex"),
    ("DailyForex",    "https://www.dailyforex.com/rss/forexnews.xml", "forex"),
    ("Investing FX",  "https://www.investing.com/rss/news_1.rss", "forex"),
]

def fetch_rss() -> list[dict]:
    import xml.etree.ElementTree as ET
    out = []
    for name, url, category in RSS_FEEDS:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "korvus-engine/0.2"})
            if r.status_code != 200:
                print(f"  [rss] {name} returned {r.status_code} - skipping")
                continue
            root = ET.fromstring(r.content)
            # RSS items live at channel/item; handle namespaces loosely
            items = root.findall(".//item")
            count = 0
            for it in items[:25]:                       # cap per feed
                title = (it.findtext("title") or "").strip()
                if not title:
                    continue
                desc = (it.findtext("description") or "").strip()
                desc = re.sub(r"<[^>]+>", " ", desc)    # strip any HTML
                link = (it.findtext("link") or "").strip()
                pub  = (it.findtext("pubDate") or "").strip()
                out.append({
                    "id": make_id("wire", title),       # same hash space → dedupes vs Benzinga dupes
                    "source": "wire",
                    "source_name": name,
                    "category": category,               # 'general' | 'forex'
                    "url": link,
                    "published_at": pub,
                    "headline": title,
                    "raw_text": desc[:1200],
                })
                count += 1
            print(f"  [rss] {name} pulled {count} articles")
        except Exception as e:
            print(f"  [rss] {name} error: {e}")
    return out


# ----------------------------------------------------------------------------
# THE BRAIN - Claude scores one item: summary + impact + direction + conf
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the senior markets analyst behind Korvus, a market-intelligence \
terminal for an index-futures day trader (mainly MNQ and MES, ICT/Smart-Money \
style). Reason like a professional trading-desk strategist: rigorous, calibrated, sober, \
evidence-based, and index-futures first. For each news or social item you receive, \
assess its likely short-term impact on US index futures and return STRICT JSON only, \
no prose, no markdown.

Return exactly this shape:
{
  "noise": true | false,        // true ONLY for pure non-market junk (see rules)
  "summary": "<=2 sentences, plain English, the quick take shown on the feed card>",
  "impact_desc": "<3-5 sentences of desk-grade analysis for the detail view, reasoning about impact on INDEX FUTURES specifically. Work the real framework: (1) classify the event, broad macro versus single-name or sector, and whether the names involved are large enough to move the index by weight or breadth; (2) name the transmission mechanism, the concrete channel by which it reaches the tagged instruments (rates, risk sentiment, index weight, credit spreads, the dollar, positioning); (3) anchor to the base rate for this kind of event (for example, single-name M&A in mid-cap industrials is typically low-impact to broad index futures); (4) give the calibrated read plus the specific thing that would confirm or invalidate it and what to watch next. Concrete and specific to THIS story, sober, and honest about uncertainty. This is analysis and a read, never personalized advice or a trade instruction.>",
  "impact": "high" | "med" | "low",
  "direction": "bull" | "bear" | "neut",
  "instruments": ["MNQ", ...],   // subset of the watched list, [] if none
  "confidence": 0-100            // your confidence in this read
}

Guidance:

NOISE FILTER (set "noise": true ONLY for pure non-market junk that should never
appear in a trading feed). Mark noise=true for:
  - personal-finance listicles ("10 best free checking accounts", "best credit
    cards", "should I buy a boat", mortgage-rate roundups, retirement advice)
  - product/service reviews (credit card reviews, app reviews)
  - celebrity / human-interest / lifestyle with zero market relevance
  - pure how-to / explainer content with no market event
Everything with ANY genuine market relevance gets noise=false - even if low impact.
When in doubt, noise=false.

IMPACT - use these CONCRETE criteria and be CONSISTENT (the same underlying
event must get the same impact regardless of how the headline is worded):

  "high" = a genuine MACRO market-mover likely to move index futures NOW:
    - Fed/FOMC decisions, rate guidance, Powell remarks
    - CPI / PCE / jobs (NFP) / major economic data surprises
    - Broad tariffs / trade war affecting MANY countries or whole sectors
    - War, military strikes, major geopolitical shocks, oil supply shocks
    - Central-bank intervention (BOJ yen intervention, ECB emergency action)
    - Mega-cap earnings shocks or guidance from NVDA/AAPL/MSFT/etc. that move the index
    - Systemic/credit events (bank failures, sovereign stress)
    -> If a story is broad geopolitics, war/oil, Fed, or sweeping tariffs, it is
       almost always "high", NOT "med". Do not under-rate these.

  "med" = matters and is directional but is sector/single-name or second-order:
    - one large-cap's earnings/news (not enough alone to move the whole index)
    - a single commodity move, one country's data, sector rotation
    - meaningful but not market-wide

  "low" = minor / single small-cap / routine filings (Form 144, 6-K, S-1),
    micro-cap news, slow-moving or already-priced-in items.

DIRECTION - neutral is the HONEST default. Only assign "bull" or "bear" when the
story has a CLEAR directional read for the tagged instruments. Do NOT force a
direction to seem decisive - an informational item with no clear lean is "neut",
and that is correct. BUT: if a story is clearly risk-on or risk-off (e.g. "oil
jumps on Mideast missiles", "stocks rally on cooler CPI"), do NOT lazily call it
neutral - give it the real direction it implies.

OTHER:
- Social/rumor with no confirmation = usually "low" and lower confidence.
- Be calibrated and sober. Give analysis and a directional read, never personalized
  advice or a trade instruction. Explain mechanisms and what to watch, never "buy",
  "sell", or "go long". Calibrate confidence honestly; a genuinely low-conviction read
  is fine and correct.
- FORMAT: write the text fields (summary, impact_desc) in plain professional prose.
  Do NOT use em-dashes anywhere; use commas or periods instead. No markdown and no
  bullet characters inside the fields.
- summary = the fast headline take. impact_desc = the deeper "why it matters / how
  it transmits / what to watch" analysis. Both grounded in THIS story only.
- Tag ONLY the instruments this story is a PRIMARY, direct driver for. Be strict:
  AT MOST 3, usually 1-2, ordered most-direct first. If a story wouldn't plausibly
  move an instrument on its own, do NOT tag it. A single-stock story tags that stock
  (e.g. Broadcom earnings -> AVGO); add a broad index proxy (ONE of NQ/QQQ or ES/SPY)
  ONLY when the move is genuinely big or broad enough to shift the whole index, not by
  reflex. Macro mapping: crude -> CL; gold -> GC; Treasuries/yields -> ZN or ZB;
  EUR/ECB -> 6E; yen/BOJ -> 6J. Do NOT pad the list with loosely-related names, and do
  NOT stack near-duplicates of the same exposure (pick ONE of NQ/MNQ/QQQ, ONE of
  ES/MES/SPY) - choose the single best representative. Prefer fewer, high-conviction
  tags over a long "maybe" list. [] if nothing is directly moved.
- Only use instruments from this watched list: {INSTRUMENTS}.
"""


def _extract_json(text):
    """Pull a JSON object out of a model reply, tolerant of a preface, code
    fences, or trailing prose. Returns a dict, or None if nothing parses."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?", "", t).strip()
        t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    if start < 0:
        return None
    depth = 0                       # walk to the matching close brace, ignore anything after
    for i in range(start, len(t)):
        ch = t[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


def score_with_claude(client, item: dict, _retry: bool = True) -> Optional[dict]:
    sys_prompt = SYSTEM_PROMPT.replace("{INSTRUMENTS}", ", ".join(WATCHED_INSTRUMENTS))
    user_blob = (
        f"SOURCE: {item['source']} ({item.get('source_name','')})\n"
        f"HEADLINE: {item['headline']}\n"
        f"BODY: {item.get('raw_text','')}"
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,                 # headroom so a fuller analysis is never truncated mid-JSON
            system=sys_prompt,
            messages=[
                {"role": "user",
                 "content": user_blob + "\n\nRespond with ONLY the JSON object, starting with { and ending with }. No preamble, no markdown, no code fences."},
            ],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        data = _extract_json(text)           # tolerant of any stray preface or trailing prose
        if data is None:
            if _retry:                        # one retry before giving up on this item
                return score_with_claude(client, item, _retry=False)
            print(f"    [claude] no JSON in response for: {item['headline'][:50]}")
            return None
        # normalize / clamp
        data["impact"] = data.get("impact", "low") if data.get("impact") in ("high","med","low") else "low"
        data["direction"] = data.get("direction") if data.get("direction") in ("bull","bear","neut") else "neut"
        try:
            data["confidence"] = max(0, min(100, int(data.get("confidence", 0))))
        except (ValueError, TypeError):
            data["confidence"] = 0
        if not isinstance(data.get("instruments"), list):
            data["instruments"] = []
        # keep it tight & realistic - at most the 3 most-direct (prompt orders them)
        data["instruments"] = data["instruments"][:3]
        _EM = chr(0x2014)
        def _plain(t):
            t = (t or "").strip().replace(" " + _EM + " ", ", ").replace(_EM, ", ")
            return t.replace(" ,", ",").replace("  ", " ")
        data["summary"] = _plain(data.get("summary"))
        data["impact_desc"] = _plain(data.get("impact_desc"))
        data["noise"] = bool(data.get("noise", False))
        return data
    except Exception as e:
        print(f"    [claude] error: {e}")
        return None


def process_unscored(conn, client, limit: int = 40):
    rows = conn.execute(
        "SELECT * FROM items WHERE processed = 0 ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        print("  [brain] nothing new to score")
        return
    print(f"  [brain] scoring {len(rows)} item(s) with {CLAUDE_MODEL}...")
    for row in rows:
        item = dict(row)
        result = score_with_claude(client, item)
        if result is None:
            continue
        conn.execute("""
            UPDATE items
            SET summary=?, impact_desc=?, impact=?, direction=?, instruments=?, confidence=?, noise=?, processed=1
            WHERE id=?
        """, (
            result["summary"],
            result.get("impact_desc", ""),
            result["impact"],
            result["direction"],
            json.dumps(result["instruments"]),
            result["confidence"],
            1 if result.get("noise") else 0,
            item["id"],
        ))
        conn.commit()
        # Fire a one-time email alert to opted-in Pro members on genuine
        # high-impact events. Wrapped so a mail hiccup never breaks scoring.
        if result["impact"] == "high" and not result.get("noise"):
            try:
                auth.send_high_impact_alert({
                    "headline":    item.get("headline", ""),
                    "summary":     result.get("summary", ""),
                    "impact_desc": result.get("impact_desc", ""),
                    "direction":   result.get("direction", ""),
                    "instruments": result.get("instruments", []),
                    "url":         item.get("url", ""),
                })
            except Exception as e:
                print(f"    [alerts] notify failed: {e}")
        if result.get("noise"):
            print(f"    · [noise - hidden]                     {item['headline'][:60]}")
        else:
            print(f"    ✓ [{result['impact']:>4}|{result['direction']:>4}|{result['confidence']:>3}%] "
                  f"{item['headline'][:64]}")
        time.sleep(0.4)  # gentle pacing


# ----------------------------------------------------------------------------
# ONE FULL PASS
# ----------------------------------------------------------------------------
def run_once():
    print(f"\n=== KORVUS pass @ {dt.datetime.now().strftime('%I:%M:%S %p')} ===")
    if not ANTHROPIC_API_KEY:
        print("  !! ANTHROPIC_API_KEY is missing - add it to .env. Aborting pass.")
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    conn = db_connect()

    # 1) gather from all sources
    pulled = []
    pulled += fetch_news()      # Benzinga Basic / premium / Alpha Vantage (per .env)
    pulled += fetch_rss()       # free financial RSS feeds (no key) - fills the gaps
    pulled += fetch_reddit()
    pulled += fetch_x()

    # 2) store only NEW ones
    new_count = 0
    for item in pulled:
        if not item_exists(conn, item["id"]):
            insert_raw_item(conn, item)
            new_count += 1
    print(f"  [store] {new_count} new item(s) saved (of {len(pulled)} pulled)")

    # 3) let Claude score anything unscored
    process_unscored(conn, client)

    # 4) quick tally
    total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    print(f"  [db] korvus.db now holds {total} item(s)")
    conn.close()

    # Forex to Discord no longer runs here. It has its own fast heartbeat
    # (korvus_forex_discord.run_forever), started as a daemon thread in main(),
    # so releases post within a minute instead of waiting on this 5-min loop.


def main():
    ap = argparse.ArgumentParser(description="Korvus engine (Phase 1)")
    ap.add_argument("--loop", action="store_true",
                    help=f"run forever, every {POLL_MINUTES} min")
    args = ap.parse_args()

    db_init()
    if args.loop:
        # Forex posts on its own fast heartbeat, decoupled from the 5-min news
        # loop, so releases land within a minute of the calendar registering them.
        try:
            import threading
            from korvus_forex_discord import run_forever
            threading.Thread(target=run_forever, daemon=True).start()
        except Exception as e:
            print(f"  [forex-discord] could not start fast poll: {e}")
        print(f"Korvus engine in LOOP mode - every {POLL_MINUTES} min. Ctrl+C to stop.")
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                print("\nStopped.")
                sys.exit(0)
            except Exception as e:
                print(f"  !! pass error: {e}")
            time.sleep(POLL_MINUTES * 60)
    else:
        run_once()
        print("\nDone. Run with --loop to keep it going 24/7.")


if __name__ == "__main__":
    main()

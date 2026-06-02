#!/usr/bin/env python3
"""
==============================================================================
 KORVUS ENGINE  ·  Phase 1   (by BlackCrownVxJ.LLC)
==============================================================================
 WHAT THIS DOES
   1. Pulls market news from Alpha Vantage (ticker-tagged, with sentiment)
   2. Pulls social chatter from Reddit (finance subreddits)
   3. (X / Twitter is stubbed — flip it on later when you add a key)
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
import argparse
import datetime as dt
from typing import Optional

# --- third-party libs (installed via requirements.txt) ----------------------
import requests
from dotenv import load_dotenv
import anthropic

# ----------------------------------------------------------------------------
# CONFIG  — loaded from your .env file so secrets never live in the code
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

# Which Claude model does the summarizing. Haiku = fast + cheap, ideal here.
CLAUDE_MODEL = "claude-haiku-4-5"

# How often the --loop mode runs (minutes)
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "5"))

# The instruments Korvus tracks — Claude maps each news item onto THESE.
# Expanded to cover every market shown on the dashboard so stories get tagged
# with the RIGHT instrument (a crude-oil headline -> CL, gold -> GC, bonds ->
# ZN, EUR -> 6E), not just the index futures. Grouped for readability.
WATCHED_INSTRUMENTS = [
    # US index futures (+ their cash/ETF equivalents)
    "MNQ", "MES", "MYM", "M2K", "NQ", "ES", "YM", "RTY", "QQQ", "SPY", "DIA", "IWM",
    # Commodities
    "CL",   # crude oil
    "GC",   # gold
    "SI",   # silver
    "NG",   # natural gas
    "HG",   # copper
    # Rates / bonds
    "ZN",   # 10Y T-note
    "ZB",   # 30Y T-bond
    "ZF",   # 5Y
    # FX futures
    "6E",   # euro
    "6J",   # yen
    "6B",   # pound
    "DXY",  # dollar index
    # Volatility
    "VX",   # VIX futures
    # Megacap equities that move the indices
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
    # Crypto (only if relevant)
    "BTC", "ETH",
]

# Alpha Vantage news "topics"/tickers to track. Index futures move on big tech,
# the broad market, and macro — so we pull those tickers' news.
AV_TICKERS = ["QQQ", "SPY", "NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA"]

# Reddit subreddits to scan for chatter
SUBREDDITS = ["wallstreetbets", "stocks", "options", "futures", "Daytrading"]

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "korvus.db")


# ----------------------------------------------------------------------------
# DATABASE  — one table, "items". Dead simple and easy to read from the UI.
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
            impact       TEXT,               -- 'high' | 'med' | 'low'
            direction    TEXT,               -- 'bull' | 'bear' | 'neut'
            instruments  TEXT,               -- JSON list e.g. ["MNQ","MES"]
            confidence   INTEGER,            -- 0-100
            processed    INTEGER DEFAULT 0   -- 1 once Claude has scored it
        )
    """)
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
        (id, created_at, published_at, source, source_name, url, headline, raw_text, processed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        item["id"],
        dt.datetime.now(dt.timezone.utc).isoformat(),
        item.get("published_at"),
        item["source"],
        item.get("source_name"),
        item.get("url"),
        item["headline"],
        item.get("raw_text", ""),
    ))
    conn.commit()


# ----------------------------------------------------------------------------
# SOURCE 1 — NEWS PROVIDER LAYER  (swappable)
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
    print(f"  [wire] unknown NEWS_PROVIDER '{NEWS_PROVIDER}' — skipping news")
    return []


# --- Benzinga (Basic free tier now; premium later by flipping NEWS_PROVIDER) -
# Free Basic tier: headline + teaser + link. Get a token at benzinga.com (or
# via AWS Marketplace "Benzinga Basic Financial News API"). Premium uses the
# same endpoint with fuller content once you have a paid token.
def fetch_benzinga(premium: bool = False) -> list[dict]:
    if not BENZINGA_KEY:
        print("  [wire] no BENZINGA_KEY set — skipping")
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
            print("  [wire] Benzinga 401 — check BENZINGA_KEY / that your plan covers the news API")
            return []
        data = r.json()
        # Benzinga returns a list of article objects
        articles = data if isinstance(data, list) else data.get("news", data.get("data", []))
        for art in articles:
            headline = (art.get("title") or "").strip()
            if not headline:
                continue
            # 'teaser' on basic; 'body' (HTML) on premium — strip tags for Claude
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
        print("  [wire] no ALPHAVANTAGE_KEY set — skipping")
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
# SOURCE 2 — REDDIT  (free API via OAuth client-credentials)
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
        print("  [reddit] no Reddit credentials set — skipping")
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
# SOURCE 3 — X / TWITTER  (stub — turn on later)
# X's API is pay-per-read now; wire it in once you've got a key + budget.
# Keep the same return shape and the rest of the engine just works.
# ----------------------------------------------------------------------------
def fetch_x() -> list[dict]:
    # TODO: implement with your X API bearer token when ready.
    return []


# ----------------------------------------------------------------------------
# SOURCE 4 — FREE RSS NEWS  (no API key needed)
# Supplements Benzinga's free tier, which only serves a static recent window.
# These public financial feeds publish frequently, so they fill the gaps and
# keep the timeline advancing. Parsed with the stdlib (no extra dependency).
# Toggle/extend via RSS_FEEDS below.
# ----------------------------------------------------------------------------
RSS_FEEDS = [
    ("CNBC Markets",  "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Top",      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362"),
    ("MarketWatch",   "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MW RealTime",   "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Investing.com", "https://www.investing.com/rss/news.rss"),
    ("SeekingAlpha",  "https://seekingalpha.com/market_currents.xml"),
    ("InvestingLive", "https://www.investinglive.com/feed"),
]

def fetch_rss() -> list[dict]:
    import xml.etree.ElementTree as ET
    out = []
    for name, url in RSS_FEEDS:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "korvus-engine/0.2"})
            if r.status_code != 200:
                print(f"  [rss] {name} returned {r.status_code} — skipping")
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
# THE BRAIN — Claude scores one item: summary + impact + direction + conf
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the analyst engine for Korvus, a market-intelligence \
terminal for an index-futures day trader (mainly MNQ and MES, ICT/Smart-Money \
style). For each news or social item you receive, assess its likely short-term \
impact on US index futures and return STRICT JSON only — no prose, no markdown.

Return exactly this shape:
{
  "summary": "<=2 sentences, plain English, what it means for an index trader>",
  "impact": "high" | "med" | "low",
  "direction": "bull" | "bear" | "neut",
  "instruments": ["MNQ", ...],   // subset of the watched list, [] if none
  "confidence": 0-100            // your confidence in this read
}

Guidance:
- "high" impact = likely to move markets now (Fed, CPI, megacap shock, geopolitics, OPEC, major data).
- Social/rumor with no confirmation = usually "low" and lower confidence.
- Be calibrated and sober. Do NOT give trading advice or tell the user to buy/sell.
- Tag the instruments MOST DIRECTLY affected by THIS story, across all markets —
  not just index futures. A crude-oil story -> CL; gold -> GC; a Treasury/yield
  story -> ZN/ZB; a EUR/ECB story -> 6E; a single megacap -> that ticker (+ NQ/QQQ
  if it's big enough to move the index). Use [] if nothing on the list fits.
- Only use instruments from this watched list: {INSTRUMENTS}.
"""


def score_with_claude(client, item: dict) -> Optional[dict]:
    sys_prompt = SYSTEM_PROMPT.replace("{INSTRUMENTS}", ", ".join(WATCHED_INSTRUMENTS))
    user_blob = (
        f"SOURCE: {item['source']} ({item.get('source_name','')})\n"
        f"HEADLINE: {item['headline']}\n"
        f"BODY: {item.get('raw_text','')}"
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_blob}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        # Be forgiving if the model wraps JSON in stray text/backticks
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            print(f"    [claude] no JSON in response for: {item['headline'][:50]}")
            return None
        data = json.loads(match.group(0))
        # normalize / clamp
        data["impact"] = data.get("impact", "low") if data.get("impact") in ("high","med","low") else "low"
        data["direction"] = data.get("direction") if data.get("direction") in ("bull","bear","neut") else "neut"
        data["confidence"] = max(0, min(100, int(data.get("confidence", 0))))
        if not isinstance(data.get("instruments"), list):
            data["instruments"] = []
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
            SET summary=?, impact=?, direction=?, instruments=?, confidence=?, processed=1
            WHERE id=?
        """, (
            result["summary"],
            result["impact"],
            result["direction"],
            json.dumps(result["instruments"]),
            result["confidence"],
            item["id"],
        ))
        conn.commit()
        print(f"    ✓ [{result['impact']:>4}|{result['direction']:>4}|{result['confidence']:>3}%] "
              f"{item['headline'][:64]}")
        time.sleep(0.4)  # gentle pacing


# ----------------------------------------------------------------------------
# ONE FULL PASS
# ----------------------------------------------------------------------------
def run_once():
    print(f"\n=== KORVUS pass @ {dt.datetime.now().strftime('%I:%M:%S %p')} ===")
    if not ANTHROPIC_API_KEY:
        print("  !! ANTHROPIC_API_KEY is missing — add it to .env. Aborting pass.")
        return

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    conn = db_connect()

    # 1) gather from all sources
    pulled = []
    pulled += fetch_news()      # Benzinga Basic / premium / Alpha Vantage (per .env)
    pulled += fetch_rss()       # free financial RSS feeds (no key) — fills the gaps
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


def main():
    ap = argparse.ArgumentParser(description="Korvus engine (Phase 1)")
    ap.add_argument("--loop", action="store_true",
                    help=f"run forever, every {POLL_MINUTES} min")
    args = ap.parse_args()

    db_init()
    if args.loop:
        print(f"Korvus engine in LOOP mode — every {POLL_MINUTES} min. Ctrl+C to stop.")
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

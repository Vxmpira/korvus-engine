"""Visit, click and signup-step counters for the BlackCrown admin's Board.

The admin's agents kept stalling on numbers nobody collected: human versus
crawler visits per page, which hero button gets clicked, and where signups
drop off. This module counts all three on the Korvus box itself (server side,
so ad blockers and JS-less crawlers cannot skew it) and serves the totals to
the admin through /api/feed/traffic. Counts only, never IPs or identities.
"""
import os
import re
import sqlite3
import threading
import datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("KORVUS_TRAFFIC_DB", os.path.join(HERE, "korvus_traffic.db"))
ET = ZoneInfo("America/New_York")

# crawlers, link unfurlers, monitors and scripts. A real browser UA matches none
BOT_RE = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|preview|headless|lighthouse|pagespeed|python|curl|wget|"
    r"httpclient|okhttp|go-http|java/|node-fetch|undici|axios|monitor|uptime|pingdom|ahrefs|"
    r"semrush|mj12|dotbot|petal|bytespider|gptbot|claudebot|claude-web|perplexity|amazonbot|"
    r"applebot|facebookexternalhit|meta-externalagent|embedly|whatsapp|telegram|discord|"
    r"twitterbot|linkedinbot|slackbot|scrapy|feedfetcher|validator|archive\.org|^node$|^$",
    re.I,
)

_lock = threading.Lock()
_ready = False


def is_bot(ua):
    return bool(BOT_RE.search(ua or ""))


def _conn():
    global _ready
    c = sqlite3.connect(DB, timeout=5)
    if not _ready:
        c.execute(
            "CREATE TABLE IF NOT EXISTS hits (day TEXT, kind TEXT, key TEXT, n INTEGER, "
            "PRIMARY KEY (day, kind, key))"
        )
        c.commit()
        _ready = True
    return c


def today(offset=0):
    return (dt.datetime.now(ET) - dt.timedelta(days=offset)).strftime("%Y-%m-%d")


def record(kind, key):
    """Add one to (today ET, kind, key). Never raises: counting must never
    break a page load or a signup."""
    try:
        key = re.sub(r"[^a-z0-9_/.-]", "", str(key).lower())[:48] or "unknown"
        with _lock:
            c = _conn()
            c.execute(
                "INSERT INTO hits (day, kind, key, n) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(day, kind, key) DO UPDATE SET n = n + 1",
                (today(), kind, key),
            )
            c.commit()
            c.close()
    except Exception:
        pass


def referrer_bucket(ref, own_host="korvus.industries"):
    host = ""
    m = re.match(r"https?://([^/]+)", ref or "", re.I)
    if m:
        host = m.group(1).lower()
    if not host:
        return "direct"
    if own_host in host:
        return ""  # an internal click, not a source
    for name, pat in (
        ("x", r"(^|\.)x\.com$|t\.co$|twitter"),
        ("google", r"google\."),
        ("bing", r"bing\.|duckduckgo|yahoo|brave"),
        ("tiktok", r"tiktok"),
        ("youtube", r"youtube|youtu\.be"),
        ("discord", r"discord"),
        ("reddit", r"reddit"),
        ("brand_site", r"blackcrownvxj\.com"),
        ("crown", r"blackcrown-intelligence"),
        ("tradingview", r"tradingview"),
        ("ai", r"chatgpt|openai|perplexity|claude\.ai|gemini|copilot"),
    ):
        if re.search(pat, host):
            return name
    return "other"


def summary(days=14):
    """Totals for the last 7 days, the 7 before them, and a per-day series.
    kinds: view_h / view_b (page key), click (button key), step (signup
    funnel step), src (where human visitors came from)."""
    days = max(1, min(60, int(days or 14)))
    since = today(days - 1)
    rows = []
    try:
        with _lock:
            c = _conn()
            rows = c.execute(
                "SELECT day, kind, key, n FROM hits WHERE day >= ? ORDER BY day", (since,)
            ).fetchall()
            first = c.execute("SELECT MIN(day) FROM hits").fetchone()[0]
            c.close()
    except Exception as e:
        return {"error": str(e)[:200]}
    w7, p7, series = {}, {}, {}
    cut7, cut14 = today(6), today(13)
    for day, kind, key, n in rows:
        series.setdefault(day, {}).setdefault(kind, {})[key] = n
        bucket = w7 if day >= cut7 else (p7 if day >= cut14 else None)
        if bucket is not None:
            bucket.setdefault(kind, {})
            bucket[kind][key] = bucket[kind].get(key, 0) + n
    return {
        "counting_since": first,
        "today": today(),
        "last7": w7,
        "prior7": p7,
        "series": series,
        "notes": "view_js pages a real browser rendered (its script ran; the true people count), "
        "view_h hits whose user agent looked human (includes scripts posing as browsers), view_b crawler or script hits, click button keys "
        "(data-k), step signup funnel, src first-touch source of human views. Server side, ET days.",
    }

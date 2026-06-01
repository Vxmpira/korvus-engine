#!/usr/bin/env python3
"""
Korvus — RSS feed tester.
Run this ON THE SERVER to see which free financial RSS feeds are reachable
from your AWS IP and return parseable items. Keep the ones that say OK.

    python rss_test.py
"""
import requests
import xml.etree.ElementTree as ET

# Candidate free financial RSS feeds (no API key). We'll keep whichever work.
CANDIDATES = [
    ("CNBC Markets",   "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Top",       "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362"),
    ("MarketWatch",    "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MW RealTime",    "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("Yahoo Finance",  "https://finance.yahoo.com/news/rssindex"),
    ("NASDAQ Mkts",    "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
    ("Investing.com",  "https://www.investing.com/rss/news.rss"),
    ("Investing 25",   "https://www.investing.com/rss/news_25.rss"),
    ("SeekingAlpha",   "https://seekingalpha.com/market_currents.xml"),
    ("InvestingLive",  "https://www.investinglive.com/feed"),
]

UA = "Mozilla/5.0 (compatible; korvus-engine/0.2; +https://korvus.industries)"

print("Testing RSS feeds from this server...\n")
working = []
for name, url in CANDIDATES:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": UA})
        if r.status_code != 200:
            print(f"  [{r.status_code}] {name}  — blocked/unavailable")
            continue
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        if items:
            sample = (items[0].findtext("title") or "")[:55]
            print(f"  [OK]  {name}  — {len(items)} items  e.g. \"{sample}\"")
            working.append((name, url))
        else:
            print(f"  [--]  {name}  — 200 but no <item>s found")
    except Exception as e:
        print(f"  [ERR] {name}  — {type(e).__name__}: {e}")

print("\n" + "="*60)
if working:
    print("KEEP THESE (paste into korvus_engine.py RSS_FEEDS):\n")
    for name, url in working:
        print(f'    ("{name}", "{url}"),')
else:
    print("None worked from this server. We'll try a different approach.")
print("="*60)

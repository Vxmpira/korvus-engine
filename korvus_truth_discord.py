#!/usr/bin/env python3
"""
==============================================================================
 KORVUS PRESIDENTIAL WIRE -> DISCORD        (BlackCrownVxJ LLC)
==============================================================================
 Posts market-moving @realDonaldTrump Truth Social posts to #daily-news,
 quietly (no pings), each with Korvus's own read attached.

 It routes every new post through the SAME brain that scores your news
 (korvus_engine.score_with_claude, Haiku, your system prompt), so relevance is
 judged by meaning, not keywords: only posts the model marks market-relevant go
 out, and each card carries the impact, direction, confidence, instruments, and
 the desk-grade "Korvus read", exactly like the terminal.

 SOURCE: the free public RSS archive at trumpstruth.org. No API key for the
 feed, no cost. It is a third-party relay, so best-effort, not an official API.

 HOW IT RUNS: the engine's fast heartbeat (korvus_forex_discord.run_forever)
 calls post_new_truths() every ~60s, so a post lands within about a minute.

 CONFIG (.env)
   TRUTH_DISCORD_WEBHOOK   channel webhook (default: FOREX_DISCORD_WEBHOOK)
   TRUTH_FEED_URL          RSS source (default: trumpstruth.org/feed)
   TRUTH_FILTER            "smart"  -> Claude-scored, market-movers only (default)
                           "keyword"-> fast keyword filter, no scoring, no read
                           "all"    -> post everything, no filter
   TRUTH_LOGO_URL          crown icon (default: FOREX_LOGO_URL)
   TRUTH_STATE_FILE        dedup state (default: next to this file)
   ANTHROPIC_API_KEY       reused from your engine for scoring

 CLI:
   python korvus_truth_discord.py --dry-run   score + show what would post
   python korvus_truth_discord.py --test       send one test embed
   python korvus_truth_discord.py --replay     score + post the latest mover now
   python korvus_truth_discord.py --seed        mark the feed seen, post nothing
==============================================================================
"""
import os
import re
import json
import argparse
import datetime as dt
import xml.etree.ElementTree as ET

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

WEBHOOK    = (os.getenv("TRUTH_DISCORD_WEBHOOK") or os.getenv("FOREX_DISCORD_WEBHOOK") or "").strip()
FEED_URL   = os.getenv("TRUTH_FEED_URL", "https://www.trumpstruth.org/feed").strip()
LOGO_URL   = (os.getenv("TRUTH_LOGO_URL") or os.getenv("FOREX_LOGO_URL") or "").strip()
FILTER     = os.getenv("TRUTH_FILTER", "smart").lower().strip()
STATE_FILE = os.getenv("TRUTH_STATE_FILE",
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "truth_posted.json"))

PINK   = 16723592     # neutral / no clear lean
GREEN  = 3066993      # bullish
RED    = 15158332     # bearish
AUTHOR = "ECLIPSE-X \u00B7 PRESIDENTIAL WIRE"
FOOTER = "BlackCrownVxJ LLC \u00B7 Presidential Wire"

_IMPACT_DOT = {"high": "\U0001F534", "med": "\U0001F7E0", "low": "\u26AA"}
_DIR = {"bull": ("\U0001F4C8 Bullish", GREEN),
        "bear": ("\U0001F4C9 Bearish", RED),
        "neut": ("\u2796 Neutral", PINK)}

# Fallback filter, only used when scoring is unavailable or TRUTH_FILTER=keyword.
_MARKET = re.compile(
    r"\b(tariff|tariffs|trade|china|chinese|fed|federal reserve|powell|rate|rates|interest|"
    r"inflation|cpi|ppi|jobs|payroll|unemployment|gdp|econom|oil|energy|gas|opec|stock|stocks|"
    r"market|markets|dow|nasdaq|s&p|dollar|currency|euro|yuan|deal|sanction|sanctions|tax|taxes|"
    r"chip|chips|semiconductor|nvidia|tesla|apple|boeing|bitcoin|crypto|treasury|treasuries|debt|"
    r"spending|deficit|bank|banks|recession|steel|aluminum|auto|autos|import|imports|export|exports|"
    r"russia|iran|border|drug|drugs|pharma)\b", re.I)


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("posted", [])
    s.setdefault("initialized", False)
    return s


def _save_state(s):
    s["posted"] = s["posted"][-800:]
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception as e:
        print(f"  [truth] could not save state: {e}")


_A_TAG = re.compile(r'<a\s[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def _anchor_repl(m):
    """Truth Social (Mastodon fork) chops a link's visible text into
    invisible/ellipsis spans, so stripping tags with spaces shreds the URL.
    Anchors that display a URL collapse to their href, the one canonical
    intact form, and Discord auto-links it. Hashtags and @mentions keep
    their visible text instead of turning into ugly profile/tag URLs."""
    href = (m.group(1) or "").replace("&amp;", "&")
    inner = re.sub(r"<[^>]+>", "", m.group(2) or "")
    flat = re.sub(r"\s+", "", inner).replace("&amp;", "&").rstrip(".\u2026")
    if flat.startswith(("#", "@")):
        return " " + inner + " "
    if flat.lower().startswith(("http", "www.")) or (flat and flat.lower() in href.lower()):
        return " " + href + " "
    return " " + inner + " "


def _clean(html):
    t = _A_TAG.sub(_anchor_repl, html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'").replace("&#039;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", t).strip()


def _fetch_items():
    r = requests.get(FEED_URL, timeout=15,
                     headers={"User-Agent": "korvus-engine/0.2 (+https://korvus.industries)"})
    if r.status_code != 200:
        print(f"  [truth] feed returned {r.status_code}")
        return []
    root = ET.fromstring(r.content)
    items = []
    for it in root.findall(".//item")[:40]:
        link  = (it.findtext("link") or "").strip()
        guid  = (it.findtext("guid") or link or "").strip()
        title = _clean(it.findtext("title") or "")
        desc  = _clean(it.findtext("description") or "")
        pub   = (it.findtext("pubDate") or "").strip()
        text  = desc or title
        if not guid:
            guid = link or text[:60]
        items.append({"guid": guid, "link": link, "text": text, "pub": pub})
    return items


# ---------------------------------------------------------------------------
# scoring: reuse the engine's brain so the read matches the terminal exactly
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        import anthropic
        _CLIENT = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    return _CLIENT


def _score(text):
    """Score one post with the engine's own scorer. Returns the result dict, or
    None if scoring is unavailable (missing key, error) so the caller can fall
    back to the keyword filter."""
    try:
        from korvus_engine import score_with_claude
        item = {
            "source": "truth",
            "source_name": "Truth Social (@realDonaldTrump)",
            "headline": text[:280],
            "raw_text": text[:1200],
        }
        return score_with_claude(_client(), item)
    except Exception as e:
        print(f"  [truth] scoring unavailable ({e}), using keyword fallback")
        return None


def _relevant(text):
    return bool(_MARKET.search(text or ""))


def _build_embed(item, score=None):
    text = item["text"]
    if len(text) > 900:
        cut = text[:897]
        # never slice a word or URL in half at the cut, back up to whitespace
        sp = cut.rfind(" ")
        if sp > 600:
            cut = cut[:sp]
        text = cut.rstrip() + "..."
    lines = [text]
    color = PINK
    if score:
        impact = score.get("impact", "low")
        direction = score.get("direction", "neut")
        conf = score.get("confidence", 0)
        dot = _IMPACT_DOT.get(impact, "\u26AA")
        dlabel, color = _DIR.get(direction, ("\u2796 Neutral", PINK))
        lines += ["", f"{dot} {impact.upper()} IMPACT  \u00B7  {dlabel}  \u00B7  {conf}% conf"]
        read = (score.get("impact_desc") or score.get("summary") or "").strip()
        if read:
            lines += ["", "**Korvus read**", read]
        instr = score.get("instruments") or []
        if instr:
            lines += ["", "Instruments in focus: " + ", ".join(instr)]
    if item.get("link"):
        lines += ["", f"[View on Truth Social \u203A]({item['link']})"]
    embed = {
        "author": {"name": AUTHOR},
        "title": "\U0001F1FA\U0001F1F8 President Trump \u00B7 Truth Social",
        "description": "\n".join(lines),
        "color": color,
        "footer": {"text": FOOTER},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if LOGO_URL:
        embed["author"]["icon_url"] = LOGO_URL
        embed["thumbnail"] = {"url": LOGO_URL}
    return embed


def _send(embed):
    payload = {"content": "", "embeds": [embed], "allowed_mentions": {"parse": []}}
    try:
        r = requests.post(WEBHOOK, json=payload, timeout=20)
    except Exception as e:
        print(f"  [truth] webhook error: {e}")
        return False
    if r.status_code not in (200, 204):
        print(f"  [truth] webhook {r.status_code}: {r.text[:160]}")
        return False
    return True


def _decide(text):
    """Return (should_post, score_or_None). score is attached to the embed when present."""
    if FILTER == "all":
        return True, None
    if FILTER == "keyword":
        return _relevant(text), None
    # smart (default): score with the brain; skip noise; fall back to keyword if scoring is down
    score = _score(text)
    if score is None:
        return _relevant(text), None
    return (not score.get("noise", False)), score


def post_new_truths(dry_run=False):
    if not WEBHOOK and not dry_run:
        print("  [truth] no TRUTH_DISCORD_WEBHOOK / FOREX_DISCORD_WEBHOOK set, skipping")
        return
    try:
        items = _fetch_items()
    except Exception as e:
        print(f"  [truth] feed error: {e}")
        return
    if not items:
        return

    state = _load_state()
    seen = set(state["posted"])

    # First run: mark everything already in the feed as seen and post nothing, so
    # we never backfill the archive. Only new posts go out (and get scored) from here.
    if not state["initialized"] and not dry_run:
        for it in items:
            seen.add(it["guid"])
        state["posted"] = list(seen)
        state["initialized"] = True
        _save_state(state)
        print(f"  [truth] first run: seeded {len(items)} post(s), posting new ones from here")
        return

    fresh = [it for it in reversed(items) if it["guid"] not in seen and it["text"]]
    posted = 0
    for it in fresh:
        should, score = _decide(it["text"])
        if should:
            if dry_run:
                tag = f"[{score['impact']}/{score['direction']}]" if score else "[keyword]"
                print(f"  [truth][dry] POST {tag}: {it['text'][:80]}")
            elif _send(_build_embed(it, score)):
                posted += 1
        elif dry_run:
            print(f"  [truth][dry] skip: {it['text'][:80]}")
        if not dry_run:
            seen.add(it["guid"])

    if not dry_run:
        state["posted"] = list(seen)
        _save_state(state)
        if posted:
            print(f"  [truth] posted {posted} new mover(s)")


def _test():
    if not WEBHOOK:
        print("  [truth] no webhook set")
        return
    e = _build_embed({"text": "If you can read this in #daily-news, the Korvus Presidential Wire is wired up correctly.",
                      "link": "https://truthsocial.com/@realDonaldTrump"})
    print("  [truth] test sent" if _send(e) else "  [truth] test failed")


def _replay():
    items = _fetch_items()
    for it in items:                 # newest first; find the first that would post
        should, score = _decide(it["text"])
        if should and it["text"]:
            _send(_build_embed(it, score))
            print(f"  [truth] replayed: {it['text'][:80]}")
            return
    print("  [truth] nothing worth posting in the feed right now")


def _seed():
    items = _fetch_items()
    state = _load_state()
    seen = set(state["posted"])
    for it in items:
        seen.add(it["guid"])
    state["posted"] = list(seen)
    state["initialized"] = True
    _save_state(state)
    print(f"  [truth] seeded {len(items)} post(s), posted nothing")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Korvus Presidential Wire -> Discord")
    ap.add_argument("--dry-run", action="store_true", help="score and show what would post, send nothing")
    ap.add_argument("--test", action="store_true", help="send one test embed")
    ap.add_argument("--replay", action="store_true", help="post the latest mover now")
    ap.add_argument("--seed", action="store_true", help="mark the feed seen, post nothing")
    a = ap.parse_args()
    if a.test:
        _test()
    elif a.replay:
        _replay()
    elif a.seed:
        _seed()
    else:
        post_new_truths(dry_run=a.dry_run)

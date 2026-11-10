#!/usr/bin/env python3
"""
==============================================================================
 KORVUS PRIMARY SOURCE  ·  SEC EDGAR filing watcher   (BlackCrownVxJ LLC)
==============================================================================
 WHY THIS EXISTS
   Every news outlet writes its earnings story FROM the company's SEC filing.
   The filing (an 8-K with the press release attached as Exhibit 99.1) hits
   EDGAR first. This module reads that filing directly, so Korvus is no longer
   waiting on a reporter, an editor, an RSS refresh, and then our 5 min poll.

 WHAT IT DOES
   1. Polls EDGAR's live "latest filings" Atom feed (8-K and 6-K) every
      EDGAR_POLL_SECONDS (default 20s) while EDGAR is open (6 AM to 10 PM ET,
      weekdays), and every 5 min otherwise.
   2. Keeps only filings from the index heavyweights in EDGAR_TICKERS (the
      names that move NQ / ES by weight).
   3. Opens the filing, pulls Exhibit 99.1 (the press release) or, when there
      is no exhibit, the 8-K body itself.
   4. Scores it right away with the SAME brain as the rest of the feed
      (korvus_engine.score_with_claude) and saves it to korvus.db as
      source='filing', so it lands on the terminal with the exact SEC
      acceptance time next to the time Korvus had it.
   5. Optional: after EDGAR_DISCORD_DELAY_MIN (default 15), posts the med/high
      ones to Discord with both timestamps, as a public receipt that Operator
      members had it first.

 SEC FAIR ACCESS (do not remove)
   The SEC caps everyone at 10 requests/second and asks automated tools to
   declare a User-Agent with a company name and contact email. We send at most
   2 feed requests per poll plus 2 per matched filing, with a hard floor of
   0.25s between requests, and declare EDGAR_USER_AGENT.

 CONFIG (.env, all optional)
   EDGAR_ENABLED            1 (default) / 0 to switch the whole thing off
   EDGAR_USER_AGENT         "Korvus BlackCrownVxJ LLC <contact email>"
   EDGAR_TICKERS            extra tickers, comma separated (added to the base list)
   EDGAR_FORMS              default "8-K,6-K"
   EDGAR_POLL_SECONDS       default 20 (min 10)
   EDGAR_MAX_AGE_MIN        ignore filings older than this on (re)start, default 30
   EDGAR_DISCORD_WEBHOOK    channel webhook for the delayed public receipt (off if blank)
   EDGAR_DISCORD_DELAY_MIN  default 15
   EDGAR_TERMINAL_URL       link in the Discord card, default https://korvus.industries/terminal

 CLI
   python korvus_edgar.py --once                one poll, score + save matches
   python korvus_edgar.py --loop                run forever (the engine also starts this as a thread)
   python korvus_edgar.py --dry-run             show what matches right now, no AI, no DB
   python korvus_edgar.py --dry-run --all       same, ignoring the watchlist
   python korvus_edgar.py --replay <index-url>  score + save one specific filing (demo / backfill)
   python korvus_edgar.py --replay <url> --no-save   score + print only
==============================================================================
"""
import os
import re
import sys
import json
import time
import html as _html
import sqlite3
import argparse
import threading
import datetime as dt
import xml.etree.ElementTree as ET
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


def _clean_env(key: str, default: str = "") -> str:
    """systemd EnvironmentFile keeps inline comments and quotes; strip both."""
    v = os.getenv(key, default)
    if v is None:
        return default
    v = v.split(" #")[0].strip().strip('"').strip("'").strip()
    return v if v else default


def _int_env(key: str, default: int, floor: int = 0) -> int:
    try:
        return max(floor, int(_clean_env(key, str(default))))
    except ValueError:
        return default


ENABLED        = _clean_env("EDGAR_ENABLED", "1") not in ("0", "false", "no", "off")
USER_AGENT     = _clean_env("EDGAR_USER_AGENT", "Korvus BlackCrownVxJ LLC BlackCrownVxJ.LLC@outlook.com")
FORMS          = [f.strip().upper() for f in _clean_env("EDGAR_FORMS", "8-K,6-K").split(",") if f.strip()]
POLL_SECONDS   = _int_env("EDGAR_POLL_SECONDS", 20, floor=10)
OFF_HOURS_SEC  = 300
MAX_AGE_MIN    = _int_env("EDGAR_MAX_AGE_MIN", 30, floor=1)
DISCORD_HOOK   = _clean_env("EDGAR_DISCORD_WEBHOOK", "")
DISCORD_DELAY  = _int_env("EDGAR_DISCORD_DELAY_MIN", 15, floor=0)
TERMINAL_URL   = _clean_env("EDGAR_TERMINAL_URL", "https://korvus.industries/terminal")
LOGO_URL       = _clean_env("FOREX_LOGO_URL", "")

DB_PATH    = os.path.join(HERE, "korvus.db")
STATE_FILE = os.path.join(HERE, "edgar_state.json")
TICKER_MAP_CACHE = os.path.join(HERE, "edgar_tickers.json")

FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}"
            "&company=&dateb=&owner=include&start=0&count=100&output=atom")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# The names that move NQ / ES by index weight, plus the bellwethers the desk
# watches. CIKs verified against SEC company_tickers.json on 2026-11-09
# (note XOM moved to a new holding-company CIK). The live map refreshes daily
# and wins over these, so a future reorg cannot silently blind the watcher.
BASE_WATCHLIST = {
    "AAPL": 320193,  "MSFT": 789019,  "NVDA": 1045810, "AMZN": 1018724,
    "META": 1326801, "GOOGL": 1652044, "TSLA": 1318605, "AVGO": 1730168,
    "AMD": 2488,     "NFLX": 1065280, "COST": 909832,  "PLTR": 1321655,
    "ORCL": 1341439, "CRM": 1108524,  "ADBE": 796343,  "INTC": 50863,
    "QCOM": 804328,  "MU": 723125,    "ASML": 937966,  "TSM": 1046179,
    "ARM": 1973239,  "JPM": 19617,    "GS": 886982,    "BAC": 70858,
    "XOM": 2115436,  "LLY": 59478,    "UNH": 731766,   "V": 1403161,
    "MA": 1141391,   "WMT": 104169,   "BRK-B": 1067983,
}

# 8-K item codes -> what a trader calls them. Order = how we rank importance.
ITEM_NAMES = {
    "2.02": "earnings release",
    "7.01": "Reg FD disclosure",
    "5.02": "executive / board change",
    "1.01": "material agreement",
    "1.02": "agreement terminated",
    "2.01": "acquisition or disposal completed",
    "2.03": "new debt obligation",
    "2.05": "restructuring / exit costs",
    "2.06": "material impairment",
    "3.01": "listing notice",
    "4.02": "non-reliance on prior financials",
    "5.07": "shareholder vote results",
    "8.01": "other material event",
}
ITEM_RANK = list(ITEM_NAMES.keys())
# items that are never worth a feed card on their own
LOW_VALUE_ITEMS = {"5.07", "5.03", "9.01", "5.08", "3.03"}


# ----------------------------------------------------------------------------
# HTTP: one polite session, declared UA, hard spacing between requests
# ----------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
_http_lock = threading.Lock()
_last_req = [0.0]
# The SEC allows 10 requests a second per user across all machines. Three
# processes share it: the terminal watcher (this default, 2/s), Korvus Intel
# and Korvus Compass (4/s each; they set MIN_GAP for their own process).
MIN_GAP = 1.0 / max(0.5, min(6.0, float(_clean_env("EDGAR_MAX_RPS", "2") or 2)))


def _get(url: str, timeout: int = 15) -> Optional[str]:
    with _http_lock:
        wait = MIN_GAP - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
    try:
        r = _session.get(url, timeout=timeout)
        if r.status_code == 429 or r.status_code == 403:
            print(f"  [edgar] SEC returned {r.status_code}; backing off 60s")
            time.sleep(60)
            return None
        if r.status_code != 200:
            return None
        r.encoding = r.encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"  [edgar] fetch error {url[:80]}: {e}")
        return None


# ----------------------------------------------------------------------------
# WATCHLIST: ticker <-> CIK, refreshed from the SEC daily, cached on disk
# ----------------------------------------------------------------------------
_watch_lock = threading.Lock()
_watch = {"by_cik": {}, "loaded": 0.0}


def _wanted_tickers() -> list:
    extra = [t.strip().upper() for t in _clean_env("EDGAR_TICKERS", "").split(",") if t.strip()]
    out = list(BASE_WATCHLIST.keys())
    for t in extra:
        if t not in out:
            out.append(t)
    return out


def load_watchlist(force: bool = False) -> dict:
    """Return {cik:int -> ticker}. Live SEC map first, then disk cache, then
    the verified base list, so the watcher always has something to work with."""
    with _watch_lock:
        if not force and _watch["by_cik"] and time.time() - _watch["loaded"] < 86400:
            return _watch["by_cik"]
        sec_map = {}
        raw = _get(TICKER_MAP_URL, timeout=30)
        if raw:
            try:
                data = json.loads(raw)
                sec_map = {r["ticker"].upper(): int(r["cik_str"]) for r in data.values()}
                with open(TICKER_MAP_CACHE + ".tmp", "w") as f:
                    json.dump(sec_map, f)
                os.replace(TICKER_MAP_CACHE + ".tmp", TICKER_MAP_CACHE)
            except Exception as e:
                print(f"  [edgar] ticker map parse error: {e}")
                sec_map = {}
        if not sec_map and os.path.exists(TICKER_MAP_CACHE):
            try:
                with open(TICKER_MAP_CACHE) as f:
                    sec_map = json.load(f)
            except Exception:
                sec_map = {}
        by_cik = {}
        for t in _wanted_tickers():
            cik = sec_map.get(t) or BASE_WATCHLIST.get(t)
            if cik:
                # share classes (GOOG/GOOGL) map to one CIK; keep the first name
                by_cik.setdefault(int(cik), t)
            else:
                print(f"  [edgar] no CIK found for {t}, skipping it")
        _watch["by_cik"] = by_cik
        _watch["loaded"] = time.time()
        return by_cik


# ----------------------------------------------------------------------------
# PARSERS (pure functions, covered by tests/test_edgar.py)
# ----------------------------------------------------------------------------
_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feed(xml_text: str) -> list:
    """EDGAR getcurrent Atom -> [{form, company, cik, url, accession, items, updated}]."""
    out = []
    if not xml_text:
        return out
    try:
        # drop the declaration (EDGAR says ISO-8859-1) and parse the decoded str
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text, count=1))
    except ET.ParseError as e:
        print(f"  [edgar] feed parse error: {e}")
        return out
    for e in root.iter(_ATOM + "entry"):
        title = (e.findtext(_ATOM + "title") or "").strip()
        m = re.match(r"^\s*(\S+)\s+-\s+(.*?)\s+\((\d{10})\)\s+\((\w[\w ]*)\)\s*$", title)
        if not m:
            continue
        form, company, cik, role = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        link = e.find(_ATOM + "link")
        url = link.get("href") if link is not None else ""
        summary = _html.unescape(e.findtext(_ATOM + "summary") or "")
        acc = re.search(r"AccNo:\s*</b>\s*([\d-]+)|AccNo:\s*([\d-]+)", summary)
        accession = (acc.group(1) or acc.group(2)) if acc else ""
        if not accession:
            idm = re.search(r"accession-number=([\d-]+)", e.findtext(_ATOM + "id") or "")
            accession = idm.group(1) if idm else ""
        items = re.findall(r"Item\s+(\d+\.\d+)", summary)
        out.append({
            "form": form, "company": company, "cik": cik, "role": role,
            "url": url, "accession": accession, "items": items,
            "updated": (e.findtext(_ATOM + "updated") or "").strip(),
        })
    return out


def parse_index(html_text: str, base: str = "https://www.sec.gov") -> dict:
    """Filing -index.htm -> {accepted: 'YYYY-MM-DD HH:MM:SS' (ET), docs: [{type, desc, url}]}."""
    res = {"accepted": "", "docs": []}
    if not html_text:
        return res
    m = re.search(r"Accepted</div>\s*<div class=\"info\">([^<]+)</div>", html_text)
    if m:
        res["accepted"] = m.group(1).strip()
    t0 = html_text.find('summary="Document Format Files"')
    if t0 < 0:
        return res
    t1 = html_text.find("</table>", t0)
    table = html_text[t0:t1 if t1 > 0 else None]
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            continue
        href = re.search(r'href="([^"]+)"', cells[2])
        if not href:
            continue
        link = href.group(1)
        if link.startswith("/ix?doc="):         # inline XBRL viewer wrapper
            link = link[len("/ix?doc="):]
        if link.startswith("/"):
            link = base + link
        def _txt(c):
            return re.sub(r"\s+", " ", _html.unescape(re.sub(r"(?is)<[^>]+>", " ", c))).strip()
        res["docs"].append({"type": _txt(cells[3]).upper(), "desc": _txt(cells[1]), "url": link})
    return res


def pick_document(docs: list, form: str) -> Optional[dict]:
    """Prefer the press release (EX-99.1, then any EX-99.x), else the form body."""
    htm = [d for d in docs if d["url"].lower().endswith((".htm", ".html", ".txt"))
           and not d["url"].lower().endswith("-index.htm")]
    for want in ("EX-99.1", "EX-99.01", "EX-99"):
        for d in htm:
            if d["type"].startswith(want):
                return d
    for d in htm:
        if d["type"] == form.upper() or d["type"].startswith(form.upper().split("/")[0]):
            return d
    return None


def html_to_text(raw: str, limit: int = 14000) -> str:
    """Filing HTML -> readable text. Keeps table rows as 'a | b | c' lines so the
    model can read the income statement, drops the SGML header and scripts."""
    if not raw:
        return ""
    s = raw
    s = re.sub(r"(?is)^.*?<TEXT>", "", s, count=1) if "<TEXT>" in s[:2000] else s
    s = re.sub(r"(?is)<(script|style|title|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", s)
    s = re.sub(r"(?i)</t[dh]\s*>", " | ", s)
    s = re.sub(r"(?i)<(br|/p|/div|/tr|/li|/h\d)[^>]*>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    lines = []
    for ln in s.split("\n"):
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        ln = re.sub(r"(\|\s*){2,}", "| ", ln).strip(" |")
        if ln and not re.fullmatch(r"[|\s$%()-]*", ln):
            lines.append(ln)
    return "\n".join(lines)[:limit]


_HEADLINE_HINT = re.compile(r"(?i)\b(announces?|reports?|results|declares?|appoints?|names|"
                            r"completes?|agrees?|enters?|provides?|update|outlook|guidance)\b")


def pick_headline(text: str, ticker: str, company: str, items: list, form: str) -> str:
    """The press release's own title if we can find it, else a plain description."""
    for ln in text.split("\n")[:25]:
        low = ln.lower()
        if any(b in low for b in ("foreign private issuer", "pursuant to rule", "securities exchange act",
                                  "commission file number", "indicate by check mark")):
            continue
        if 25 <= len(ln) <= 180 and "|" not in ln and _HEADLINE_HINT.search(ln) \
                and not ln.upper().startswith(("EX-99", "EXHIBIT", "FORM ", "UNITED STATES")):
            return f"{ticker}: {ln.rstrip('.')}"
    what = describe_items(items)
    if what:
        return f"{ticker} files {form}: {what}"
    return f"{ticker}: new {'foreign issuer report' if form.upper().startswith('6-K') else form} filed with the SEC"


def describe_items(items: list) -> str:
    named = [ITEM_NAMES[i] for i in sorted(set(items), key=lambda x: ITEM_RANK.index(x)
                                            if x in ITEM_RANK else 99) if i in ITEM_NAMES]
    return ", ".join(named[:2])


def worth_it(entry: dict) -> bool:
    """Skip filings that only carry vote results / bylaw tweaks and nothing else.
    6-Ks carry no item codes, so they always pass."""
    items = set(entry.get("items") or [])
    if not items:
        return True
    return bool(items - LOW_VALUE_ITEMS)


def to_utc_iso(stamp: str) -> str:
    """Feed 'updated' (has an offset) -> UTC ISO. Blank on failure."""
    try:
        d = dt.datetime.fromisoformat(stamp)
        if d.tzinfo is None:
            return ""
        return d.astimezone(dt.timezone.utc).isoformat()
    except Exception:
        return ""


def accepted_et_to_utc(accepted: str) -> str:
    """Index page 'Accepted' (ET, no offset) -> UTC ISO."""
    try:
        from zoneinfo import ZoneInfo
        d = dt.datetime.strptime(accepted, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
        return d.astimezone(dt.timezone.utc).isoformat()
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# STATE: accessions already handled + pending Discord receipts
# ----------------------------------------------------------------------------
_state_lock = threading.Lock()


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            s.setdefault("seen", [])
            s.setdefault("discord_pending", [])
            return s
    except Exception:
        return {"seen": [], "discord_pending": []}


def _save_state(s: dict):
    s["seen"] = s["seen"][-3000:]
    s["discord_pending"] = s["discord_pending"][-200:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


# ----------------------------------------------------------------------------
# DB + BRAIN
# ----------------------------------------------------------------------------
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


_client = [None]


def _brain():
    if _client[0] is None:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        _client[0] = anthropic.Anthropic(api_key=key)
    return _client[0]


def build_item(entry: dict, ticker: str, idx: dict, doc_text: str) -> dict:
    import korvus_engine as eng
    filed_utc = accepted_et_to_utc(idx.get("accepted", "")) or to_utc_iso(entry.get("updated", ""))
    what = describe_items(entry.get("items") or [])
    headline = pick_headline(doc_text, ticker, entry["company"], entry.get("items") or [], entry["form"])
    preface = (f"PRIMARY SOURCE: SEC Form {entry['form']} filed by {entry['company']} ({ticker}), "
               f"accepted by EDGAR {idx.get('accepted') or entry.get('updated')} ET."
               + (f" Items: {', '.join(entry.get('items') or [])} ({what})." if what else ""))
    return {
        "id": eng.make_id("filing", entry["accession"] or entry["url"]),
        "source": "filing",
        "source_name": f"SEC {entry['form']} · {ticker}",
        "url": entry["url"],
        "published_at": filed_utc,
        "headline": headline,
        "raw_text": (preface + "\n\n" + doc_text[:1400]).strip(),
        "article_text": doc_text,
        "category": "general",
        "ticker": ticker,
    }


def score_and_save(item: dict, save: bool = True) -> Optional[dict]:
    """Insert, score immediately (no waiting on the 5 min news pass), update.
    If scoring fails the row stays processed=0 and the engine's normal pass
    picks it up, so nothing is ever lost."""
    import korvus_engine as eng
    if save:
        eng.db_init()
        conn = _db()
        try:
            if eng.item_exists(conn, item["id"]):
                return None
            eng.insert_raw_item(conn, item)
        finally:
            conn.close()
    client = _brain()
    if client is None:
        print("  [edgar] no ANTHROPIC_API_KEY, saved unscored")
        return None
    result = eng.score_with_claude(client, item)
    if result is None:
        return None
    if not save:
        return result
    conn = _db()
    try:
        eng.save_score(conn, item["id"], result)
    finally:
        conn.close()
    if result["impact"] == "high" and not result.get("noise"):
        try:
            eng.auth.send_high_impact_alert({
                "headline": result.get("title") or item["headline"], "summary": result.get("summary", ""),
                "impact_desc": result.get("impact_desc", ""), "direction": result.get("direction", ""),
                "instruments": result.get("instruments", []), "url": item.get("url", ""),
            })
        except Exception as e:
            print(f"    [edgar] alert email failed: {e}")
    return result


# ----------------------------------------------------------------------------
# ONE POLL
# ----------------------------------------------------------------------------
def _fresh(entry: dict) -> bool:
    u = to_utc_iso(entry.get("updated", ""))
    if not u:
        return True
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(u)
    return age <= dt.timedelta(minutes=MAX_AGE_MIN)


def fetch_matches(all_names: bool = False) -> list:
    watch = load_watchlist()
    matches, seen_acc = [], set()
    for form in FORMS:
        for e in parse_feed(_get(FEED_URL.format(form=form)) or ""):
            if e["accession"] in seen_acc:
                continue
            seen_acc.add(e["accession"])
            ticker = watch.get(e["cik"])
            if ticker or all_names:
                e["ticker"] = ticker or e["company"][:18]
                matches.append(e)
    return matches


def handle_entry(e: dict, save: bool = True, verbose: bool = True) -> Optional[dict]:
    idx = parse_index(_get(e["url"]) or "")
    doc = pick_document(idx["docs"], e["form"])
    text = html_to_text(_get(doc["url"]) or "") if doc else ""
    if not text:
        text = f"{e['company']} filed Form {e['form']}. " + (describe_items(e.get("items") or []) or "")
    item = build_item(e, e["ticker"], idx, text)
    t0 = time.time()
    result = score_and_save(item, save=save)
    if verbose:
        if result:
            print(f"    ✓ [{result['impact']:>4}|{result['direction']:>4}|{result['confidence']:>3}%] "
                  f"{item['headline'][:70]}  ({time.time() - t0:.1f}s to score)")
        else:
            print(f"    · saved / skipped: {item['headline'][:70]}")
    if result and save and DISCORD_HOOK and result["impact"] in ("high", "med") and not result.get("noise"):
        with _state_lock:
            s = _load_state()
            s["discord_pending"].append({
                "post_after": time.time() + DISCORD_DELAY * 60,
                "item_id": item["id"],
                "headline": item["headline"], "summary": result.get("summary", ""),
                "impact": result["impact"], "direction": result["direction"],
                "instruments": result.get("instruments", []), "url": item["url"],
                "filed_utc": item["published_at"],
                "korvus_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ticker": item["ticker"], "form": e["form"],
            })
            _save_state(s)
    return result


def rescore(limit: int = 20) -> int:
    """Re-read recent filing cards from the source document with the current
    prompt (new titles, key numbers, key points) and update them in place."""
    import korvus_engine as eng
    eng.db_init()
    client = _brain()
    if client is None:
        print("  [edgar] no ANTHROPIC_API_KEY")
        return 0
    conn = _db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM items WHERE source='filing' ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    conn.close()
    done = 0
    for r in rows:
        idx = parse_index(_get(r["url"]) or "")
        form = next((d["type"] for d in idx["docs"] if d["type"] in ("8-K", "6-K", "8-K/A", "6-K/A")), "8-K")
        doc = pick_document(idx["docs"], form)
        text = html_to_text(_get(doc["url"]) or "") if doc else ""
        item = {**r, "article_text": text}
        res = eng.score_with_claude(client, item)
        if not res:
            print(f"    x {r['headline'][:70]}")
            continue
        conn = _db()
        try:
            eng.save_score(conn, r["id"], res)
        finally:
            conn.close()
        done += 1
        print(f"    ✓ {res.get('title') or r['headline']}"[:110])
    return done


def poll_once(verbose: bool = True) -> int:
    with _state_lock:
        state = _load_state()
    seen = set(state["seen"])
    handled = 0
    for e in fetch_matches():
        if e["accession"] in seen:
            continue
        with _state_lock:
            s = _load_state()
            s["seen"].append(e["accession"])
            _save_state(s)
        seen.add(e["accession"])
        if not _fresh(e) or not worth_it(e):
            continue
        if verbose:
            print(f"  [edgar] {e['ticker']} {e['form']} {e['updated']} items={e.get('items')}")
        try:
            handle_entry(e, save=True, verbose=verbose)
            handled += 1
        except Exception as ex:
            print(f"  [edgar] handle error {e['ticker']}: {ex}")
    flush_discord()
    return handled


# ----------------------------------------------------------------------------
# DISCORD: delayed public receipt (proof that Operator had it first)
# ----------------------------------------------------------------------------
_COLORS = {"bull": 0x46D17F, "bear": 0xFF2E4D, "neut": 0xFF2D8F}


def _et(iso: str, fmt: str = "%-I:%M:%S %p") -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromisoformat(iso).astimezone(ZoneInfo("America/New_York")).strftime(fmt)
    except Exception:
        return ""


FORM_LABELS = {"8-K": "Current report", "8-K/A": "Amended current report",
               "6-K": "Foreign issuer report", "6-K/A": "Amended foreign issuer report"}
_DIR_WORD = {"bull": "\u25B2 Bullish", "bear": "\u25BC Bearish", "neut": "\u25C6 Neutral"}


def _jl(v):
    try:
        x = json.loads(v or "[]")
        return x if isinstance(x, list) else []
    except (TypeError, ValueError):
        return []


def _short(t: str, n: int) -> str:
    """Trim to n chars on a word boundary (only when needed)."""
    t = " ".join(str(t or "").split())
    if len(t) <= n:
        return t
    cut = t[:n - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "\u2026"


_FILLER = {"authorization", "authorized", "strategic", "total", "cash", "approved", "board", "amount"}


def tidy_labels(facts: list) -> tuple:
    """Shorter, aligned number labels: lift a period prefix shared by most
    rows ("Q3 Revenue", "Q3 EPS" -> heading "Q3"), then drop filler words
    from labels that are still long. Returns (period, [labels])."""
    labels = [" ".join(str(f["label"]).split()) for f in facts]
    first = [l.split(" ", 1)[0] for l in labels]
    period = ""
    for tok in set(first):
        if (sum(1 for x in first if x == tok) >= max(2, (len(labels) + 1) // 2)
                and any(c.isdigit() for c in tok) and len(tok) <= 6):
            period = tok
    out = []
    for l in labels:
        if period and l.startswith(period + " "):
            l = l[len(period) + 1:]
        if len(l) > 16:
            l = " ".join(w for w in l.split() if w.lower() not in _FILLER) or l
        out.append(l[:1].upper() + l[1:])
    return period, out


def build_sec_embed(row: dict) -> dict:
    """The terminal's SEC card as a clean Discord embed. No thumbnail and no
    field grid (both squeeze the text): one status line, the take as a quote,
    key numbers as an aligned table, up to 3 short points, watch next, links.
    Respects Discord's limits (title 256, description 4096, 6000 total)."""
    sn = row.get("source_name") or "SEC filing"                # "SEC 6-K · TSM"
    form = sn.replace("SEC", "").split("·")[0].strip() or "8-K"
    ticker = sn.split("·")[-1].strip() if "·" in sn else ""
    company = (row.get("company") or "").strip()
    who = f"{company} ({ticker})" if company and ticker else (company or ticker)
    filed = _et(row.get("published_at") or row.get("created_at") or "", "%-I:%M %p ET")
    title = (row.get("title") or row.get("headline") or "SEC filing").strip()

    inst = _jl(row.get("instruments"))
    status = (f"**{(row.get('impact') or 'low').upper()} IMPACT**  \u00B7  "
              f"{_DIR_WORD.get(row.get('direction'), _DIR_WORD['neut'])} {row.get('confidence') or 0}%"
              + (f"  \u00B7  {'  '.join('`' + x + '`' for x in inst[:3])}" if inst else ""))
    parts = [status]
    if row.get("summary"):
        parts.append("> " + _short(row["summary"], 330))
    facts = [f for f in _jl(row.get("facts")) if isinstance(f, dict) and f.get("label") and f.get("value")][:6]
    if facts:
        period, labels = tidy_labels(facts)
        labels = [_short(l, 20) for l in labels]
        w = max(len(l) for l in labels)
        lines = []
        for l, f in zip(labels, facts):
            line = f"{l:<{w}}  {_short(f['value'], 18)}"
            if f.get("change"):
                line += f"  {_short(f['change'], 16)}"
            lines.append(line)
        head = "**Key numbers" + (" \u00B7 " + period if period else "") + "**"
        parts.append(head + "\n```\n" + "\n".join(lines) + "\n```")
    pts = [p for p in _jl(row.get("key_points")) if str(p).strip()][:3]
    if pts:
        parts.append("**Why it matters**\n" + "\n".join(f"\u2022 {_short(p, 170)}" for p in pts))
    if row.get("watch"):
        parts.append("**Watch next**\n" + _short(row["watch"], 220))
    parts.append(f"[Read the filing]({row.get('url', '')})  \u00B7  [Open Korvus]({TERMINAL_URL})")
    embed = {
        "author": {"name": f"SEC \u00B7 {form} \u00B7 {FORM_LABELS.get(form, 'Filing')}"
                           + (f" \u00B7 {who}" if who else "")},
        "title": _short(title, 256),
        "url": row.get("url") or None,
        "description": "\n\n".join(parts)[:4000],
        "color": _COLORS.get(row.get("direction"), _COLORS["neut"]),
        "footer": {"text": f"Filed {filed}  \u00B7  Korvus  \u00B7  analysis, not advice"},
    }
    if row.get("published_at"):
        embed["timestamp"] = row["published_at"]
    if not embed["url"]:
        embed.pop("url")
    return embed


def _row_for(item_id: str) -> Optional[dict]:
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        conn.close()
        return dict(r) if r else None
    except Exception:
        return None


def post_embed(embed: dict) -> bool:
    if not DISCORD_HOOK:
        print("  [edgar] EDGAR_DISCORD_WEBHOOK not set")
        return False
    for attempt in (1, 2):
        try:
            r = requests.post(DISCORD_HOOK, json={"embeds": [embed], "allowed_mentions": {"parse": []}}, timeout=20)
            if r.status_code in (200, 204):
                return True
            if r.status_code == 429 and attempt == 1:
                time.sleep(float((r.json() or {}).get("retry_after", 2)) + 0.5)
                continue
            print(f"  [edgar] discord post HTTP {r.status_code}: {r.text[:200]}")
            return False
        except Exception as ex:
            print(f"  [edgar] discord post failed: {ex}")
            return False
    return False


def flush_discord():
    """Post queued filings to the SEC channel once their delay is up, built
    from the card AS IT IS NOW (so a --rescore also improves queued posts)."""
    if not DISCORD_HOOK:
        return
    with _state_lock:
        s = _load_state()
        due = [p for p in s["discord_pending"] if p["post_after"] <= time.time()]
        if not due:
            return
        s["discord_pending"] = [p for p in s["discord_pending"] if p["post_after"] > time.time()]
        _save_state(s)
    for p in due:
        row = _row_for(p["item_id"]) if p.get("item_id") else None
        if row is None:   # queued before this version: build from what was saved
            row = {"source_name": f"SEC {p.get('form', '8-K')} \u00B7 {p.get('ticker', '')}",
                   "headline": p.get("headline"), "summary": p.get("summary"), "impact": p.get("impact"),
                   "direction": p.get("direction"), "instruments": json.dumps(p.get("instruments") or []),
                   "url": p.get("url"), "published_at": p.get("filed_utc")}
        post_embed(build_sec_embed(row))
        time.sleep(1.2)   # stay well under Discord's webhook rate limit


# ----------------------------------------------------------------------------
# LOOP
# ----------------------------------------------------------------------------
def _edgar_open_now() -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5)))
    return now.weekday() < 5 and 6 <= now.hour < 22


def run_forever():
    if not ENABLED:
        print("  [edgar] EDGAR_ENABLED=0, watcher off")
        return
    w = load_watchlist()
    print(f"  [edgar] primary-source watcher on: {len(w)} names, forms {FORMS}, "
          f"every {POLL_SECONDS}s while EDGAR is open")
    while True:
        try:
            poll_once()
        except Exception as e:
            print(f"  [edgar] loop error: {e}")
        time.sleep(POLL_SECONDS if _edgar_open_now() else OFF_HOURS_SEC)


def start_thread() -> Optional[threading.Thread]:
    """Called by korvus_engine --loop so systemd supervises it with the engine."""
    if not ENABLED:
        return None
    t = threading.Thread(target=run_forever, name="korvus-edgar", daemon=True)
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description="Korvus primary-source SEC filing watcher")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="dry-run: ignore the watchlist")
    ap.add_argument("--replay", metavar="INDEX_URL")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--discord-test", type=int, nargs="?", const=1, metavar="N",
                    help="post the N most recent SEC cards to the Discord channel right now (preview)")
    ap.add_argument("--rescore", type=int, metavar="N",
                    help="re-read the N most recent SEC filings on the terminal with the current prompt")
    a = ap.parse_args()

    if a.discord_test:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM items WHERE source='filing' AND processed=1 ORDER BY created_at DESC LIMIT ?",
            (a.discord_test,))]
        conn.close()
        if not rows:
            print("  [edgar] no scored SEC cards yet")
        for r in reversed(rows):
            ok = post_embed(build_sec_embed(r))
            print(f"  [edgar] {'posted' if ok else 'FAILED'}: {(r.get('title') or r['headline'])[:80]}")
            time.sleep(1.2)
        return
    if a.rescore:
        n = rescore(a.rescore)
        print(f"  [edgar] rescored {n} filing(s)")
        return
    if a.dry_run:
        found = fetch_matches(all_names=a.all)
        print(f"  [edgar] reached EDGAR; {len(found)} matching filing(s) in the latest feed"
              + ("" if found or a.all else " (none from the watchlist right now, that is normal; try --all)"))
        for e in found[:40]:
            print(f"  {e['updated'][11:19]}  {e['ticker']:<18} {e['form']:<6} "
                  f"{'skip' if not worth_it(e) else 'TAKE'}  {describe_items(e['items']) or e['items']}")
        return
    if a.replay:
        m = re.search(r"/data/(\d+)/", a.replay)
        cik = int(m.group(1)) if m else 0
        ticker = load_watchlist().get(cik, str(cik))
        acc = re.search(r"(\d{10}-\d{2}-\d{6})-index", a.replay)
        idx = parse_index(_get(a.replay) or "")
        form = next((d["type"] for d in idx["docs"] if d["type"] in ("8-K", "6-K", "8-K/A")), "8-K")
        e = {"form": form, "company": ticker, "cik": cik, "url": a.replay,
             "accession": acc.group(1) if acc else a.replay, "items": [], "updated": "", "ticker": ticker}
        r = handle_entry(e, save=not a.no_save)
        if r:
            print(json.dumps(r, indent=2))
        return
    if a.loop:
        run_forever()
        return
    n = poll_once()
    print(f"  [edgar] handled {n} new filing(s)")


if __name__ == "__main__":
    main()

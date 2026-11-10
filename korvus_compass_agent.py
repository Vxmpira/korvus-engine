"""
==============================================================================
 KORVUS COMPASS ANALYST  (the agent that writes every stock page)
==============================================================================
 The Compass crawler gathers numbers. This agent turns each stock's numbers
 into a short brief a normal investor can read: one headline, a two-sentence
 summary, what is working, what to be careful about, what to watch next, and
 who the stock tends to suit. Briefs are written ahead of time and stored, so
 a member never opens an empty page.

 HOW IT STAYS HONEST
   - It only sees the crawler's data sheet for that stock, and is told to use
     nothing else. No outside facts, no price targets, no predictions.
   - It never tells anyone to buy or sell. A brief that slips into advice is
     thrown away and replaced by the rule-written version.
   - Every brief says who wrote it and when.

 WHO WRITES
   COMPASS_AGENT=haiku  (default) Claude Haiku.
   COMPASS_AGENT=crown  Crown on OpenRouter; Haiku steps in if Crown fails.
   COMPASS_AGENT=off    rule-written briefs only (no model, no cost).
   With no key at all, or when a model call fails, the rule-written brief is
   used, built from the same score notes shown on the page.

 WHEN
   A brief is rewritten only when something material changes: the tier, the
   read, the trend direction, a new quarterly or annual report, the analyst
   consensus, or a new Korvus filing read. At most once a day per stock unless
   a new report lands.
==============================================================================
"""

import os
import re
import json
import time
import hashlib
from typing import Optional

import requests

import korvus_compass as c

AGENT         = c._env("COMPASS_AGENT", "haiku").lower()
PER_TICK      = int(c._num("COMPASS_BRIEFS_PER_TICK", 80))
CLAUDE_MODEL  = "claude-haiku-4-5"
CROWN_BASE    = c._env("CROWN_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
CROWN_KEY     = c._env("CROWN_API_KEY") or c._env("OPENROUTER_API_KEY")
CROWN_MODEL   = c._env("CROWN_MODEL")
CROWN_TIMEOUT = int(c._num("CROWN_TIMEOUT", 120))

PROMPT = """You are the Compass Analyst for Korvus, a research desk for everyday long-term investors \
(people who buy shares on Robinhood or Cash App and hold them for years). You are given ONE stock's \
data sheet, gathered by the Korvus crawler from SEC filings, price history and analyst consensus.

Write a brief in plain English that a beginner can follow. Rules:
- Use ONLY the facts on the data sheet. Do not add outside knowledge, news, products, or history.
- Quote real numbers from the sheet. Never invent or round a number into something it is not.
- No advice: never tell the reader to buy, sell, hold, add or avoid. No price targets. No predictions \
about where the price will go. Describe what the data shows.
- Be balanced. Every stock gets both strengths and risks.
- Short sentences. No jargon without a five-word explanation. No em dashes.

Return ONLY this JSON object:
{"headline": "max 90 characters, the one-line takeaway",
 "summary": "2 or 3 sentences on what kind of company profile this is right now",
 "bull": ["2 or 3 short points on what is working, each with a number"],
 "bear": ["2 or 3 short points on what to be careful about, each with a number where possible"],
 "watch": "one sentence: the single thing in the data most worth watching next",
 "fit": "one sentence: the kind of long-term investor this profile tends to suit, described, not advised"}"""

BANNED = re.compile(r"(?i)\b(you should|we recommend|i recommend|buy now|sell now|strong buy opportunity|"
                    r"must[- ]own|can't lose|cannot lose|guarantee[ds]?|price target of|will (surge|soar|double|"
                    r"triple|skyrocket|crash|moon)|is a buy\b|is a sell\b|time to buy|time to sell)")


def crown_ready() -> bool:
    return bool(CROWN_KEY and CROWN_MODEL)


_client = [None]


def _haiku():
    if _client[0] is None:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        import anthropic
        _client[0] = anthropic.Anthropic(api_key=key)
    return _client[0]


def describe() -> str:
    if AGENT == "off":
        return "rule-written briefs only (COMPASS_AGENT=off)"
    who = []
    if AGENT == "crown":
        who.append(f"Crown ({CROWN_MODEL})" if crown_ready() else "Crown NOT configured")
    who.append("Haiku" if os.getenv("ANTHROPIC_API_KEY") else "Haiku key MISSING")
    return " then ".join(who) + ", rule-written as the safety net"


# ----------------------------------------------------------------------------
# the data sheet (the only thing the model sees)
# ----------------------------------------------------------------------------
def data_sheet(s: dict) -> str:
    f, a, v = s.get("fund") or {}, s.get("analysts") or {}, c.valuation(s)
    cur = f.get("currency", "USD")
    L = [f"STOCK: {s['symbol']} ({s.get('name') or ''})",
         f"SECTOR: {s.get('sector') or 'n/a'} / {s.get('industry') or 'n/a'}",
         f"COMPASS SCORE: {s.get('score')} of 100, tier {s.get('tier')}, read {s.get('read')}",
         "SCORE PARTS (0-100 each):"]
    for p in s.get("parts") or []:
        if p.get("value") is not None:
            L.append(f"  {p['label']} {p['value']} ({p['weight']}% weight): " + "; ".join(p.get("notes") or []))
    L += ["PRICE TREND:",
          f"  trend label: {s.get('trend')}; last close {s.get('price')} on {s.get('price_date')}",
          f"  returns: 1 month {c.pct(s.get('ret_1m'), 1)}, 6 months {c.pct(s.get('ret_6m'), 1)}, "
          f"1 year {c.pct(s.get('ret_1y'), 1)}, 5 years {c.pct(s.get('ret_5y'), 0)}",
          f"  52-week range {s.get('lo52')} to {s.get('hi52')}; {c.pct(s.get('drawdown'), 0)} from the high; "
          f"yearly volatility {c.pct(s.get('vol_1y'), 0, False)}"]
    if f:
        L += [f"COMPANY FINANCIALS (SEC filings, fiscal year ended {f.get('fy_end')}):",
              f"  revenue {c.money(f.get('revenue'), cur)} ({c.pct(f.get('revenue_yoy'))} vs prior year, "
              f"{c.pct(f.get('revenue_cagr3'))} a year over 3 years)",
              f"  latest quarter revenue {c.money(f.get('q_revenue'), cur)} ({c.pct(f.get('q_revenue_yoy'))} vs a year ago)",
              f"  net income {c.money(f.get('net_income'), cur)}; net margin {c.pct(f.get('net_margin'), 0, False)}; "
              f"operating margin {c.pct(f.get('op_margin'), 0, False)}",
              f"  free cash flow {c.money(f.get('fcf'), cur)}; cash {c.money(f.get('cash'), cur)}; "
              f"debt {c.money(f.get('debt'), cur)}; return on equity {c.pct(f.get('roe'), 0, False)}",
              f"  share count change over 3 years {c.pct(f.get('shares_change_3y'), 1)}"]
    L.append(f"VALUATION: market value {c.money(v.get('mcap'))}; P/E {v.get('pe')}; "
             f"free cash flow yield {c.pct(v.get('fcf_yield'), 1, False)}; dividend yield {c.pct(v.get('div_yield'), 1, False)}")
    if a.get("analysts"):
        L.append(f"ANALYSTS: {a['analysts']} covering, consensus {a.get('label')}, "
                 f"{round((a.get('buy_pct') or 0) * 100)}% rate it a buy")
    reads = s.get("_reads") or []
    if reads:
        L.append("RECENT SEC FILINGS READ BY KORVUS:")
        for r in reads[:5]:
            L.append(f"  {str(r.get('at'))[:10]} {r.get('form')}: {r.get('title')} (read: {r.get('dir')})")
    return "\n".join(L).replace("n/a%", "n/a")


def signature(s: dict) -> str:
    """Changes only when something material about the stock changes."""
    reads = s.get("_reads") or []
    raw = "|".join(str(x) for x in (s.get("tier"), s.get("read"), s.get("trend_tone"), s.get("fund_accn"),
                                    (s.get("analysts") or {}).get("label"),
                                    reads[0].get("at") if reads else ""))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------
# cleaning + the rule-written brief
# ----------------------------------------------------------------------------
def _txt(x, cap: int) -> str:
    t = re.sub(r"\s+", " ", str(x or "")).strip()
    t = t.replace(" \u2014 ", ", ").replace("\u2014", ", ").replace(" \u2013 ", ", ")
    return t if len(t) <= cap else t[:cap - 1].rsplit(" ", 1)[0] + "\u2026"


def clean_brief(d) -> Optional[dict]:
    """Model output -> safe brief, or None if it is unusable or gives advice."""
    if not isinstance(d, dict):
        return None
    out = {"headline": _txt(d.get("headline"), 110), "summary": _txt(d.get("summary"), 520),
           "bull": [_txt(x, 200) for x in (d.get("bull") or []) if str(x).strip()][:3],
           "bear": [_txt(x, 200) for x in (d.get("bear") or []) if str(x).strip()][:3],
           "watch": _txt(d.get("watch"), 240), "fit": _txt(d.get("fit"), 240)}
    if not out["headline"] or not out["summary"] or not out["bull"] or not out["bear"]:
        return None
    if BANNED.search(" ".join([out["headline"], out["summary"], out["watch"], out["fit"]] + out["bull"] + out["bear"])):
        return None
    return out


def rule_brief(s: dict) -> dict:
    """A brief built only from the score notes. Always available, costs nothing."""
    parts = [p for p in (s.get("parts") or []) if p.get("value") is not None]
    srt = sorted(parts, key=lambda p: -p["value"])
    best = [p for p in srt if p["value"] >= 60][:3]
    worst = [p for p in reversed(srt) if p["value"] < 45][:3]
    f, v = s.get("fund") or {}, c.valuation(s)
    tier, sym = s.get("tier") or "Building", s["symbol"]
    if not parts:
        return {"headline": f"{sym}: the crawler is still gathering data",
                "summary": "There is not enough data on this stock to describe it yet. Prices, SEC financials and "
                           "analyst data fill in as the crawler works through the board.",
                "bull": [], "bear": [], "watch": "", "fit": "", "by": "rules"}
    lead = best[0]["label"].lower() if best else "no single area"
    lag = worst[0]["label"].lower() if worst else None
    headline = f"{tier}: {lead} stands out" + (f", {lag} holds it back" if lag else "")
    summary = (f"{s.get('name') or sym} scores {round(s.get('score') or 0)} of 100 on the Compass, which puts it in the "
               f"{tier} tier. Its long-term price trend is: {(s.get('trend') or 'not known yet').lower()}.")
    bull = [f"{p['label']}: {p['notes'][0]}" for p in best if p.get("notes")]
    bear = [f"{p['label']}: {p['notes'][0]}" for p in worst if p.get("notes")]
    if not bull:
        bull = ["No area scores above 60 right now."]
    if not bear:
        bear = ["No area scores below 45 right now. Strong numbers can still reverse."]
    watch = ("The next quarterly report: whether revenue growth of "
             f"{c.pct(f.get('q_revenue_yoy') if f.get('q_revenue_yoy') is not None else f.get('revenue_yoy'))} holds."
             if (f.get("q_revenue_yoy") is not None or f.get("revenue_yoy") is not None)
             else "Whether the price holds above its 200-day average.")
    vol, dy = s.get("vol_1y"), v.get("div_yield")
    if dy and dy >= 0.02 and (vol or 1) < 0.28:
        fit = "Profiles like this, a dividend payer with calmer price swings, tend to suit income-minded holders."
    elif (f.get("revenue_cagr3") or 0) >= 0.15:
        fit = "Profiles like this, fast growers, tend to suit holders who accept bigger swings for growth."
    elif vol and vol >= 0.45:
        fit = "Profiles like this, with large yearly price swings, tend to suit holders with a high tolerance for volatility."
    else:
        fit = "A steady, middle-of-the-road profile that tends to suit diversified long-term holders."
    return {"headline": _txt(headline, 110), "summary": summary, "bull": bull, "bear": bear,
            "watch": watch, "fit": fit, "by": "rules"}


# ----------------------------------------------------------------------------
# the models
# ----------------------------------------------------------------------------
def _first_json(text: str) -> Optional[dict]:
    text = re.sub(r"(?is)<think>.*?</think>", "", text or "")
    i = text.find("{")
    while i != -1:
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        break
        i = text.find("{", i + 1)
    return None


def ask_haiku(sheet: str) -> Optional[dict]:
    client = _haiku()
    if client is None:
        return None
    resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=900, system=PROMPT,
                                  messages=[{"role": "user", "content": sheet}])
    return _first_json("".join(b.text for b in resp.content if b.type == "text"))


def ask_crown(sheet: str) -> Optional[dict]:
    if not crown_ready():
        return None
    r = requests.post(f"{CROWN_BASE}/chat/completions",
                      headers={"Authorization": f"Bearer {CROWN_KEY}", "Content-Type": "application/json",
                               "HTTP-Referer": "https://korvus.industries", "X-Title": "Korvus Compass"},
                      json={"model": CROWN_MODEL, "max_tokens": 1200, "temperature": 0.2,
                            "messages": [{"role": "system", "content": PROMPT},
                                         {"role": "user", "content": sheet}]},
                      timeout=CROWN_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter HTTP {r.status_code}")
    body = r.json()
    return _first_json(((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def write_brief(s: dict, askers: Optional[list] = None) -> dict:
    """Try the configured models in order; fall back to the rule-written brief."""
    if askers is None:
        askers = [] if AGENT == "off" else ([("crown", ask_crown)] if AGENT == "crown" else []) + [("haiku", ask_haiku)]
    if (s.get("parts") and s.get("tier") != "Building"):
        sheet = data_sheet(s)
        for name, fn in askers:
            try:
                b = clean_brief(fn(sheet))
            except Exception as e:
                c.status_set("agent", error=f"{name}: {e.__class__.__name__}: {str(e)[:140]}", at=c.iso())
                b = None
            if b:
                b["by"] = name
                return b
    return rule_brief(s)


# ----------------------------------------------------------------------------
# jobs
# ----------------------------------------------------------------------------
def fill_rule_briefs() -> int:
    """Give every scored stock a rule-written brief right away if it has none."""
    conn = c.db()
    rows = conn.execute("""SELECT symbol FROM stocks WHERE active=1 AND COALESCE(kind,'stock')='stock'
                           AND brief IS NULL AND parts IS NOT NULL""").fetchall()
    n = 0
    for r in rows:
        s = c.load_stock(r["symbol"], conn)
        b = rule_brief(s)
        b["at"] = c.iso()
        c.save_fields(r["symbol"], {"brief": b, "brief_utc": c.iso()}, conn)     # no sig: the model still upgrades it
        n += 1
    conn.commit()
    conn.close()
    return n


def job_briefs(limit: Optional[int] = None, verbose: bool = True, symbols: Optional[list] = None,
               askers: Optional[list] = None) -> int:
    """Write or refresh briefs where the stock changed. Best-ranked first."""
    limit = limit or PER_TICK
    conn = c.db()
    if symbols:
        marks = ",".join("?" * len(symbols))
        rows = conn.execute(f"SELECT symbol FROM stocks WHERE active=1 AND COALESCE(kind,'stock')='stock' "
                            f"AND symbol IN ({marks})", list(symbols)).fetchall()
    else:
        rows = conn.execute("""SELECT symbol FROM stocks WHERE active=1 AND COALESCE(kind,'stock')='stock'
                               AND score IS NOT NULL AND tier!='Building' ORDER BY score DESC""").fetchall()
    conn.close()
    done, by = 0, {}
    for r in rows:
        if done >= limit or c._stop.is_set():
            break
        s = c.load_stock(r["symbol"])
        if not s:
            continue
        s["_reads"] = c.korvus_reads(s["symbol"])
        sig = signature(s)
        old = s.get("brief") or {}
        can_model = AGENT != "off" and (bool(os.getenv("ANTHROPIC_API_KEY")) or (AGENT == "crown" and crown_ready()))
        # a rule-written brief gets upgraded as soon as a model can write it
        same = s.get("brief_sig") == sig
        upgrade = (old.get("by") == "rules" and bool(askers or can_model) and s.get("tier") != "Building"
                   and not (same and (old.get("tries") or 0) >= 3))      # stop retrying a model that keeps failing
        if s.get("brief_sig") == sig and not upgrade:
            continue
        new_report = bool(s.get("brief_sig")) and old.get("accn") != s.get("fund_accn")
        if (s.get("brief_sig") and not new_report and not upgrade and not symbols
                and c.age_hours(s.get("brief_utc") or "") < 24):
            continue
        b = write_brief(s, askers)
        b["at"], b["accn"] = c.iso(), s.get("fund_accn")
        if b["by"] == "rules" and (askers or can_model):
            b["tries"] = ((old.get("tries") or 0) if same else 0) + 1
        # a rule-written result while a model is configured means the call failed: retry next pass
        c.save_fields(s["symbol"], {"brief": b, "brief_sig": sig, "brief_utc": c.iso()})
        by[b["by"]] = by.get(b["by"], 0) + 1
        done += 1
        if b["by"] != "rules":
            time.sleep(0.2)
    if done or not symbols:
        c.status_set("agent", at=c.iso(), written=done, by=by, mode=AGENT,
                     **({"error": None} if by and "rules" not in by else {}))
    if verbose and done:
        print(f"  [compass] analyst wrote {done} brief(s): {by}")
    return done

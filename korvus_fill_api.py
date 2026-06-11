"""
Korvus — Promo Studio "Auto-fill" backend
==========================================
Routes the Auto-fill button in agent.html needs:

    POST /api/promo-fill        (owner / admin only)  -> fills a whole layout
    GET  /agent_autofill.js     (owner / admin only)  -> serves the fill add-on
    GET  /agent_backgrounds.js  (owner / admin only)  -> serves the background add-on

WHAT IT DOES
  • Korvus brand  -> fills using your Korvus voice + guardrails (SYSTEM) and, if
    the browser passed live engine context (top scored /api/news item), grounds
    the mockup in that real story (pinning conf/direction/impact to the feed).
  • Other brands  -> fetches that brand's FRONT PAGE server-side, reads the
    visible copy, and writes the ad to match THAT brand's offering and voice.
    It never invents facts/prices not on the page.
  • Every fill also suggests a background style + accent that fit the mood, so
    the whole ad gets dressed, not just the text.

Reuses the Anthropic client + Korvus brand voice from korvus_promo_api and the
same owner gate (admin_required). No second API key needed.

Wire-up: upload this + agent_autofill.js + agent_backgrounds.js + patch_autofill.py
into the project folder, run `python3 patch_autofill.py`, restart. (Manual:)
    from korvus_fill_api import korvus_fill_api
    app.register_blueprint(korvus_fill_api)
"""

import os
import re
import json
import time
import socket
import ipaddress
import urllib.request
import urllib.error
from urllib.parse import urlparse
from html.parser import HTMLParser

from flask import Blueprint, request, jsonify, send_from_directory

from korvus_admin import admin_required               # same owner gate as the rest
from korvus_promo_api import _anthropic, MODEL, SYSTEM  # reuse client + Korvus voice

korvus_fill_api = Blueprint("korvus_fill_api", __name__)

HERE = os.path.dirname(os.path.abspath(__file__))      # serve add-ons from the project dir

# ----------------------------------------------------------------------------
# Brand -> front-page URL. Edit/extend here, or just type the real URL into the
# Promo Studio "URL line" field and it will be used. Request value wins.
# ----------------------------------------------------------------------------
BRAND_SITES = {
    "blackcrown": "https://blackcrown-intelligence.com",
    # "custom": "https://yoursite.com",
}

# Suggestions the model may return (kept in sync with agent_backgrounds.js).
BG_KEYS = {"none", "mesh", "glow", "grid", "aurora", "dots", "graphite", "topo"}
ACCENT_KEYS = {"indigo", "gold", "crimson"}


# ----------------------------------------------------------------------------
# TRUE Korvus product facts — only metrics the model may use for KORVUS tiles.
# ----------------------------------------------------------------------------
PRODUCT_FACTS = (
    "TRUE product facts (use verbatim or lightly reworded — NEVER invent others):\n"
    "  - The engine runs 24/7 / always on.\n"
    "  - It scores 60+ market items a day (use the value '60+').\n"
    "  - Every item is tagged HIGH / MED / LOW with a bull/bear read and a confidence score.\n"
    "  - It watches SMT divergence across NQ / ES / YM.\n"
    "  - Pricing: Free to start; Pro is $3.99/mo. No other prices exist.\n"
)

HEADLINE_NOTE = (
    "The headline is THREE parts that read as ONE sentence and land an awareness "
    "punch. Example cadence: 'You saw' / 'this coming.' / \"They didn't.\""
)

# System prompt used for NON-Korvus brands (grounded entirely on the scraped site).
GENERIC_SYSTEM = (
    "You are a senior brand ad copywriter creating ONE promotional graphic. Write copy "
    "that matches the brand described in the BRAND PROFILE you are given — its real "
    "offering, audience, and tone. HARD RULES, never broken: never invent statistics, "
    "prices, testimonials, guarantees, or features that are not present in the profile; "
    "never promise profits, returns, or guaranteed outcomes; keep every claim accurate to "
    "the profile; if a fact isn't there, stay general rather than fabricate. Sharp, modern, credible."
)

_BG_ACCENT_NOTE = (
    "Then pick a background style and accent that fit the mood and ADD these two extra keys "
    "to your JSON object:\n"
    '  "_bg": one of [none, mesh, glow, grid, aurora, dots, graphite, topo]\n'
    '  "_accent": one of [indigo, gold, crimson]\n'
)

# ----------------------------------------------------------------------------
# Per-layout JSON schema (keys match agent.html state).
# ----------------------------------------------------------------------------
SCHEMAS = {
    "classified": """Return STRICT JSON, EXACTLY these keys, all values strings unless noted:
{
  "c_eyebrow":  "ALL-CAPS status line, e.g. 'CLASSIFIED · MARKET INTEL · 03:14 AM ET'",
  "c_chip1":    "short ALL-CAPS tag <=14 chars",
  "c_chip2":    "short ALL-CAPS tag",
  "c_chip3":    "short ALL-CAPS tag",
  "c_conf":     "integer 0-100 as a string, e.g. '78'",
  "c_dir":      "EXACTLY one of: BULLISH | BEARISH | NEUTRAL",
  "c_hmuted":   "headline part 1 (muted lead), 1-3 words",
  "c_hlight":   "headline part 2 (light middle), 1-3 words",
  "c_haccent":  "headline part 3 (accent punchline), 1-3 words",
  "c_sub":      "1-2 line subline in plain English",
  "s1v": "stat tile 1 value",  "s1k": "stat tile 1 label (ALL-CAPS)",
  "s2v": "stat tile 2 value",  "s2k": "stat tile 2 label (ALL-CAPS)",
  "s3v": "stat tile 3 value",  "s3k": "stat tile 3 label (ALL-CAPS)",
  "c_calla":    "closing line 1, a full short sentence",
  "c_callb":    "closing line 2 START (trailing space if it flows into the bold phrase)",
  "c_callbold": "closing line 2 BOLD phrase"
}""",

    "engine": """Return STRICT JSON, EXACTLY these keys (e1/e2 are nested objects):
{
  "e_eyebrow": "ALL-CAPS",
  "e_h1": "line 1", "e_h2": "line 2", "e_h3": "line 3 accent",
  "e_sub": "1-2 line subline",
  "e1": { "st":"status word","tm":"time","imp":"HIGH|MED|LOW","text":"ONE sentence",
          "dir":"short label","dk":"bull|neut|bear","tk":"tickers","cf":"0-100 string" },
  "e2": { "st":"...","tm":"...","imp":"...","text":"...","dir":"...","dk":"...","tk":"...","cf":"..." },
  "e_cta": "e.g. 'Start free'", "e_pbig": "big price", "e_pline": "price line"
}""",

    "statement": """Return STRICT JSON, EXACTLY these keys, all values strings:
{
  "c_hmuted":"headline part 1, 1-2 words","c_hlight":"headline part 2, 1-2 words",
  "c_haccent":"headline part 3 (accent), 1-2 words",
  "c_sub":"one tense, present-moment subline",
  "c_calla":"closing line 1","c_callb":"closing line 2 START","c_callbold":"closing line 2 BOLD phrase"
}""",
}

ALLOWED = {
    "classified": {"c_eyebrow", "c_chip1", "c_chip2", "c_chip3", "c_conf", "c_dir",
                   "c_hmuted", "c_hlight", "c_haccent", "c_sub",
                   "s1v", "s1k", "s2v", "s2k", "s3v", "s3k",
                   "c_calla", "c_callb", "c_callbold"},
    "engine":     {"e_eyebrow", "e_h1", "e_h2", "e_h3", "e_sub",
                   "e1", "e2", "e_cta", "e_pbig", "e_pline"},
    "statement":  {"c_hmuted", "c_hlight", "c_haccent", "c_sub",
                   "c_calla", "c_callb", "c_callbold"},
}
_CARD_KEYS = {"st", "tm", "imp", "text", "dir", "dk", "tk", "cf"}

_DIR_WORD = {"bull": "BULLISH", "bear": "BEARISH", "neut": "NEUTRAL"}
_DIR_LABEL = {"bull": "Bullish", "bear": "Bearish", "neut": "Neutral"}


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def _clamp_conf(v, default="72"):
    try:
        n = int(round(float(str(v).strip().replace("%", ""))))
        return str(max(0, min(100, n)))
    except Exception:
        return default


def _norm_dir_word(v):
    s = str(v or "").strip().upper()
    return s if s in ("BULLISH", "BEARISH", "NEUTRAL") else "NEUTRAL"


def _norm_dk(v):
    s = str(v or "").strip().lower()
    return s if s in ("bull", "bear", "neut") else "neut"


def _norm_imp(v):
    s = str(v or "").strip().upper()
    return s if s in ("HIGH", "MED", "LOW") else "MED"


# ----------------------------------------------------------------------------
# Brand-site scrape (server-side), with an SSRF guard and a short cache.
# ----------------------------------------------------------------------------
_BRAND_CACHE = {}      # normalized_url -> (ts, profile_dict)
_BRAND_TTL = 600       # 10 min


def _normalize_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _host_is_safe(host: str) -> bool:
    """Block loopback / private / link-local (esp. the EC2 metadata IP)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _is_safe_url(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    return _host_is_safe(p.hostname)


class _TextGrab(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []
        self.title = None
        self.desc = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "template"):
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            d = {k.lower(): (v or "") for k, v in attrs}
            key = d.get("name", "").lower() or d.get("property", "").lower()
            if key in ("description", "og:description") and d.get("content") and not self.desc:
                self.desc = d["content"].strip()
        if tag in ("h1", "h2", "h3", "p", "li", "br") and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template") and self.skip > 0:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self.skip:
            return
        t = data.strip()
        if not t:
            return
        if self._in_title and not self.title:
            self.title = t
        self.parts.append(t + " ")


def _domain_name(url: str) -> str:
    host = (urlparse(url).hostname or "").replace("www.", "")
    return host.split(".")[0].replace("-", " ").title() if host else "the brand"


def _fetch_brand_profile(url: str) -> dict:
    url = _normalize_url(url)
    if not url:
        return {"ok": False, "name": "the brand", "url": ""}
    now = time.time()
    hit = _BRAND_CACHE.get(url)
    if hit and (now - hit[0]) < _BRAND_TTL:
        return hit[1]

    fallback = {"ok": False, "name": _domain_name(url), "url": url, "text": ""}
    if not _is_safe_url(url):
        return fallback
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "KorvusPromoStudio/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(600_000)  # cap ~600KB
        html = raw.decode("utf-8", "ignore")
        g = _TextGrab()
        g.feed(html)
        body = re.sub(r"[ \t]+", " ", "".join(g.parts))
        body = re.sub(r"\n{2,}", "\n", body).strip()
        profile = {
            "ok": True,
            "name": (g.title or _domain_name(url)).strip()[:120],
            "url": url,
            "desc": (g.desc or "")[:400],
            "text": body[:2800],
        }
    except Exception:
        profile = fallback

    _BRAND_CACHE[url] = (now, profile)
    return profile


# ----------------------------------------------------------------------------
# prompt builders
# ----------------------------------------------------------------------------
def _ctx_block(ctx: dict) -> str:
    if not ctx or not (ctx.get("headline") or ctx.get("summary")):
        return ("NO LIVE CONTEXT — write an evergreen fill about Korvus itself. "
                "Do NOT reference any specific current event, price move, or headline.")
    bits = ["LIVE ENGINE CONTEXT — ground the mockup in THIS top scored item:"]
    if ctx.get("time"):        bits.append(f"  time: {ctx['time']} ET")
    if ctx.get("impact"):      bits.append(f"  impact: {ctx['impact']}")
    if ctx.get("direction"):   bits.append(f"  direction: {ctx['direction']}")
    if ctx.get("confidence") not in (None, ""):
        bits.append(f"  confidence: {ctx['confidence']}")
    if ctx.get("instruments"): bits.append(f"  instruments: {', '.join(map(str, ctx['instruments']))}")
    if ctx.get("headline"):    bits.append(f"  headline: {ctx['headline']}")
    if ctx.get("summary"):     bits.append(f"  summary: {ctx['summary']}")
    if ctx.get("smt"):         bits.append(f"  SMT read: {ctx['smt']}")
    bits.append("Use the real instruments for chips/tickers, the real impact/direction, and the "
                "real confidence number. The mockup must MATCH this item.")
    return "\n".join(bits)


def _build_korvus_prompt(layout: str, ctx: dict) -> str:
    parts = [
        f"Fill EVERY field of the Korvus promo '{layout}' mockup at once.",
        _ctx_block(ctx),
        HEADLINE_NOTE if layout in ("classified", "statement") else "",
        PRODUCT_FACTS if layout in ("classified", "engine") else "",
        "Stat tiles MUST use only the true product facts." if layout == "classified" else "",
        "Make this a FRESH variation — distinct phrasing from the obvious defaults, but on-brand: "
        "sharp, confident, nocturnal, credible. Awareness, not advice.",
        SCHEMAS[layout],
        _BG_ACCENT_NOTE,
        "Output ONLY the raw JSON object. No markdown, no code fences, no commentary.",
    ]
    return "\n\n".join(p for p in parts if p)


def _build_brand_prompt(layout: str, profile: dict) -> str:
    if profile and profile.get("ok"):
        prof = ["BRAND PROFILE (scraped from %s — the source of truth; do NOT invent facts/prices/claims "
                "not present here):" % profile.get("url", "")]
        prof.append("  name: " + (profile.get("name") or ""))
        if profile.get("desc"):
            prof.append("  description: " + profile["desc"])
        if profile.get("text"):
            prof.append("  page copy:\n" + profile["text"])
        profile_block = "\n".join(prof)
    else:
        nm = (profile or {}).get("name", "the brand")
        profile_block = (f"Could not read the brand site. Write evergreen, professional copy for a brand "
                         f"called '{nm}'. Stay general; do NOT invent specific stats, prices, or claims.")
    parts = [
        f"Fill EVERY field of this promo '{layout}' mockup at once, personalized to the brand below.",
        profile_block,
        HEADLINE_NOTE if layout in ("classified", "statement") else "",
        "For stat tiles, use ONLY metrics that appear in the brand profile; if none, use evergreen, "
        "brand-true labels (their category / what they do) and avoid invented numbers."
        if layout == "classified" else "",
        "Match the brand's real audience and tone. No profit/return promises; keep claims accurate.",
        SCHEMAS[layout],
        _BG_ACCENT_NOTE,
        "Output ONLY the raw JSON object. No markdown, no code fences, no commentary.",
    ]
    return "\n\n".join(p for p in parts if p)


def _coerce(layout: str, obj: dict, ctx: dict) -> dict:
    out = {}
    allow = ALLOWED[layout]
    for k, v in (obj or {}).items():
        if k not in allow:
            continue
        if k in ("e1", "e2") and isinstance(v, dict):
            card = {ck: ("" if v.get(ck) is None else str(v.get(ck))) for ck in _CARD_KEYS if ck in v}
            if "imp" in card: card["imp"] = _norm_imp(card["imp"])
            if "dk" in card:  card["dk"] = _norm_dk(card["dk"])
            if "cf" in card:  card["cf"] = _clamp_conf(card["cf"])
            out[k] = card
        else:
            out[k] = "" if v is None else str(v)

    if "c_conf" in out:
        out["c_conf"] = _clamp_conf(out["c_conf"])
    if "c_dir" in out:
        out["c_dir"] = _norm_dir_word(out["c_dir"])

    if ctx:  # pin grounded numbers to the live feed (Korvus only)
        dirn = str(ctx.get("direction") or "").lower()
        conf = ctx.get("confidence")
        imp = str(ctx.get("impact") or "").upper()
        if layout in ("classified", "statement"):
            if dirn in _DIR_WORD and "c_dir" in out:
                out["c_dir"] = _DIR_WORD[dirn]
            if conf not in (None, "") and "c_conf" in out:
                out["c_conf"] = _clamp_conf(conf)
        if layout == "engine" and isinstance(out.get("e1"), dict):
            if dirn in _DIR_LABEL:
                out["e1"]["dk"] = dirn if dirn in ("bull", "bear", "neut") else "neut"
                out["e1"]["dir"] = _DIR_LABEL.get(dirn, out["e1"].get("dir", ""))
            if imp in ("HIGH", "MED", "LOW"):
                out["e1"]["imp"] = imp
            if conf not in (None, ""):
                out["e1"]["cf"] = _clamp_conf(conf)
    return out


def _extract_json(text: str):
    text = (text or "").replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("model did not return a JSON object")


# ----------------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------------
@korvus_fill_api.route("/api/promo-fill", methods=["POST"])
@admin_required
def promo_fill():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set on the server."}), 500

    data = request.get_json(force=True, silent=True) or {}
    layout = (data.get("layout") or "classified").strip().lower()
    if layout not in SCHEMAS:
        return jsonify({"error": f"unknown layout '{layout}'"}), 400

    brand = (data.get("brand") or "korvus").strip().lower()
    ctx = data.get("context") or {}
    profile = None

    if brand == "korvus":
        system = SYSTEM
        user = _build_korvus_prompt(layout, ctx)
        grounded = bool(ctx)
        pin_ctx = ctx
    else:
        url = (data.get("brand_url") or "").strip() or BRAND_SITES.get(brand, "")
        profile = _fetch_brand_profile(url) if url else {"ok": False, "name": brand.title(), "url": ""}
        system = GENERIC_SYSTEM
        user = _build_brand_prompt(layout, profile)
        grounded = bool(profile and profile.get("ok"))
        pin_ctx = None

    try:
        msg = _anthropic().messages.create(
            model=MODEL,
            max_tokens=1000,
            temperature=1,                 # variety on every press = a real "refresh"
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        parsed = _extract_json(text)
        bg = str(parsed.pop("_bg", "") or "").strip().lower()
        accent = str(parsed.pop("_accent", "") or "").strip().lower()
        fields = _coerce(layout, parsed, pin_ctx)
        if not fields:
            raise ValueError("model returned no usable fields")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({
        "fields": fields,
        "layout": layout,
        "brand": brand,
        "grounded": grounded,
        "bg": bg if bg in BG_KEYS else None,
        "accent": accent if accent in ACCENT_KEYS else None,
        "brand_name": (profile or {}).get("name") if brand != "korvus" else "Korvus",
    })


@korvus_fill_api.route("/agent_autofill.js")
@admin_required
def agent_autofill_js():
    resp = send_from_directory(HERE, "agent_autofill.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@korvus_fill_api.route("/agent_backgrounds.js")
@admin_required
def agent_backgrounds_js():
    resp = send_from_directory(HERE, "agent_backgrounds.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

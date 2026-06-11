"""
Korvus — Promo Studio "Auto-fill" backend
==========================================
Routes the Promo Studio add-ons need:

    POST /api/promo-fill        (owner / admin only)  -> fills a whole layout
    POST /api/promo-caption     (owner / admin only)  -> caption + current hashtags
    GET  /agent_autofill.js     (owner / admin only)  -> serves the fill add-on
    GET  /agent_backgrounds.js  (owner / admin only)  -> serves the background add-on

WHAT IT DOES
  • Korvus brand  -> fills using your Korvus voice + guardrails; if "live" is on,
    grounds the mockup in the top scored /api/news item.
  • Other brands  -> fetches that brand's FRONT PAGE server-side and writes the
    ad to match it (never inventing facts/prices not on the page).
  • Each fill also suggests a background, accent, and a brand-correct footer line.
  • /api/promo-caption writes a platform caption + relevant CURRENT hashtags for
    the brand (tries a live web search for the freshest tags, falls back cleanly).

Reuses the Anthropic client + Korvus brand voice from korvus_promo_api and the
same owner gate (admin_required). No second API key needed.
"""

import os
import re
import json
import time
import random
import socket
import ipaddress
import urllib.request
import urllib.error
from urllib.parse import urlparse
from html.parser import HTMLParser

from flask import Blueprint, request, jsonify, send_from_directory

from korvus_admin import admin_required
from korvus_promo_api import _anthropic, MODEL, SYSTEM

korvus_fill_api = Blueprint("korvus_fill_api", __name__)

HERE = os.path.dirname(os.path.abspath(__file__))

BRAND_SITES = {
    "blackcrown": "https://blackcrown-intelligence.com",
    # "custom": "https://yoursite.com",
}

BG_KEYS = {"none", "mesh", "glow", "grid", "aurora", "dots", "graphite", "topo"}
ACCENT_KEYS = {"indigo", "gold", "crimson"}

PRODUCT_FACTS = (
    "TRUE product facts (use verbatim or lightly reworded — NEVER invent others):\n"
    "  - The engine runs 24/7 / always on.\n"
    "  - It scores 60+ market items a day (use the value '60+').\n"
    "  - Every item is tagged HIGH / MED / LOW with a bull/bear read and a confidence score.\n"
    "  - It watches SMT divergence across NQ / ES / YM.\n"
    "  - Pricing: Free to start; Pro is $3.99/mo. No other prices exist.\n"
)

HEADLINE_NOTE = (
    "The headline is THREE parts that read as ONE sentence and land an awareness punch. "
    "Example cadence: 'You saw' / 'this coming.' / \"They didn't.\""
)

GENERIC_SYSTEM = (
    "You are a senior brand ad copywriter creating ONE promotional graphic. Write copy that "
    "matches the brand described in the BRAND PROFILE you are given — its real offering, audience, "
    "and tone. HARD RULES, never broken: never invent statistics, prices, testimonials, guarantees, "
    "or features not present in the profile; never promise profits, returns, or guaranteed outcomes; "
    "keep every claim accurate to the profile; if a fact isn't there, stay general rather than fabricate."
)

# ---- creativity: a strong bar + a rotating lens so each take explores a new angle ----
_CREATIVE_RULES = (
    "CREATIVE BAR — sharp, not generic ad copy:\n"
    "  - Lead with a concrete, specific, slightly unexpected idea — not a category description.\n"
    "  - Use tension or contrast, vivid nouns, and rhythm. Short punches beat long clauses.\n"
    "  - BANNED clichés: seamless, powerful, game-changer, elevate, unlock, revolutionary, next-level, "
    "supercharge, effortless, cutting-edge, take it to the next level, in today's world.\n"
    "  - The punchline should land like the closing line of a great ad, not a feature bullet.\n"
    "  - Be specific to THIS brand. A line that could sell any product is a failed line."
)
_LENSES = [
    "Use a provocative before/after contrast.",
    "Open on a small, specific, human moment, then turn it.",
    "Use one bold claim and let it stand, unqualified.",
    "Use a vivid metaphor drawn from the brand's own world.",
    "Name the reader's quiet frustration in second person, then resolve it.",
    "Subvert an expectation in the final line.",
    "Use confident, almost cocky understatement.",
    "Name the old way as the enemy and dismiss it in a phrase.",
]


def _creative_block():
    return _CREATIVE_RULES + "\nCREATIVE LENS for THIS take: " + random.choice(_LENSES)


_EXTRAS_NOTE = (
    "Then ADD these extra keys to your JSON object:\n"
    '  "_bg": one of [none, mesh, glow, grid, aurora, dots, graphite, topo]\n'
    '  "_accent": one of [indigo, gold, crimson]\n'
    '  "_footer": a short ALL-CAPS bottom-strip line like "FREE TO START · $X/MO PRO", '
    "using the brand's REAL pricing if it is known from the profile/facts.\n"
)

SCHEMAS = {
    "classified": """Return STRICT JSON, EXACTLY these keys, all values strings unless noted:
{
  "c_eyebrow":"ALL-CAPS status line","c_chip1":"short ALL-CAPS tag <=14 chars",
  "c_chip2":"short ALL-CAPS tag","c_chip3":"short ALL-CAPS tag",
  "c_conf":"integer 0-100 as a string","c_dir":"EXACTLY one of: BULLISH | BEARISH | NEUTRAL",
  "c_hmuted":"headline part 1, 1-3 words","c_hlight":"headline part 2, 1-3 words",
  "c_haccent":"headline part 3 (accent), 1-3 words","c_sub":"1-2 line subline",
  "s1v":"stat tile 1 value","s1k":"stat tile 1 label (ALL-CAPS)",
  "s2v":"stat tile 2 value","s2k":"stat tile 2 label (ALL-CAPS)",
  "s3v":"stat tile 3 value","s3k":"stat tile 3 label (ALL-CAPS)",
  "c_calla":"closing line 1","c_callb":"closing line 2 START","c_callbold":"closing line 2 BOLD phrase"
}""",
    "engine": """Return STRICT JSON, EXACTLY these keys (e1/e2 nested):
{
  "e_eyebrow":"ALL-CAPS","e_h1":"line 1","e_h2":"line 2","e_h3":"line 3 accent","e_sub":"1-2 line subline",
  "e1":{"st":"status","tm":"time","imp":"HIGH|MED|LOW","text":"ONE sentence","dir":"label","dk":"bull|neut|bear","tk":"tags","cf":"0-100 string"},
  "e2":{"st":"...","tm":"...","imp":"...","text":"...","dir":"...","dk":"...","tk":"...","cf":"..."},
  "e_cta":"e.g. 'Start free'","e_pbig":"big price","e_pline":"price line"
}""",
    "statement": """Return STRICT JSON, EXACTLY these keys, all values strings:
{
  "c_hmuted":"headline part 1, 1-2 words","c_hlight":"headline part 2, 1-2 words",
  "c_haccent":"headline part 3 (accent), 1-2 words","c_sub":"one tense present-moment subline",
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


def _clamp_conf(v, d="72"):
    try:
        n = int(round(float(str(v).strip().replace("%", ""))))
        return str(max(0, min(100, n)))
    except Exception:
        return d


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
# Brand-site scrape (server-side), SSRF-guarded, short cache.
# ----------------------------------------------------------------------------
_BRAND_CACHE = {}
_BRAND_TTL = 600


def _normalize_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _host_is_safe(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _is_safe_url(url):
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.hostname) and _host_is_safe(p.hostname)


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


def _domain_name(url):
    host = (urlparse(url).hostname or "").replace("www.", "")
    return host.split(".")[0].replace("-", " ").title() if host else "the brand"


def _fetch_brand_profile(url):
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
            raw = resp.read(600_000)
        html = raw.decode("utf-8", "ignore")
        g = _TextGrab()
        g.feed(html)
        body = re.sub(r"[ \t]+", " ", "".join(g.parts))
        body = re.sub(r"\n{2,}", "\n", body).strip()
        profile = {"ok": True, "name": (g.title or _domain_name(url)).strip()[:120],
                   "url": url, "desc": (g.desc or "")[:400], "text": body[:2800]}
    except Exception:
        profile = fallback
    _BRAND_CACHE[url] = (now, profile)
    return profile


# ----------------------------------------------------------------------------
# prompt builders
# ----------------------------------------------------------------------------
def _ctx_block(ctx):
    if not ctx or not (ctx.get("headline") or ctx.get("summary")):
        return ("NO LIVE CONTEXT — write an evergreen fill about Korvus itself. Do NOT reference any "
                "specific current event, price move, or headline.")
    bits = ["LIVE ENGINE CONTEXT — ground the mockup in THIS top scored item:"]
    for k, lab in (("time", "time"), ("impact", "impact"), ("direction", "direction"),
                   ("confidence", "confidence"), ("headline", "headline"), ("summary", "summary"),
                   ("smt", "SMT read")):
        if ctx.get(k) not in (None, ""):
            bits.append(f"  {lab}: {ctx[k]}")
    if ctx.get("instruments"):
        bits.append("  instruments: " + ", ".join(map(str, ctx["instruments"])))
    bits.append("Use the real instruments for chips/tickers, the real impact/direction, and the real "
                "confidence number. The mockup must MATCH this item.")
    return "\n".join(bits)


def _build_korvus_prompt(layout, ctx):
    parts = [
        f"Fill EVERY field of the Korvus promo '{layout}' mockup at once.",
        _ctx_block(ctx),
        HEADLINE_NOTE if layout in ("classified", "statement") else "",
        PRODUCT_FACTS if layout in ("classified", "engine") else "",
        "Stat tiles MUST use only the true product facts." if layout == "classified" else "",
        _creative_block(),
        SCHEMAS[layout],
        _EXTRAS_NOTE,
        "Output ONLY the raw JSON object. No markdown, no code fences, no commentary.",
    ]
    return "\n\n".join(p for p in parts if p)


def _build_brand_prompt(layout, profile):
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
    repurpose = ""
    if layout == "classified":
        repurpose = ("This layout was built for a markets product, so a few fields are trading-shaped — "
                     "repurpose them for THIS brand: the three chips = short ALL-CAPS feature/benefit tags; "
                     "'c_conf' = just a tasteful number to display; 'c_dir' = use NEUTRAL unless a directional "
                     "word genuinely fits; the eyebrow reads as a brand/category line, not a market alert. Do "
                     "NOT use markets/trading language unless the brand is actually about markets.")
    elif layout == "engine":
        repurpose = ("This layout's 'news cards' were built for a markets product. For THIS brand, treat each "
                     "card as a product moment / feature highlight: 'text' = a one-line benefit, 'tk' = a short "
                     "feature label (NOT stock tickers), keep 'imp'/'dir'/'dk'/'cf' tasteful and neutral.")
    parts = [
        f"Fill EVERY field of this promo '{layout}' mockup at once, personalized to the brand below.",
        profile_block,
        repurpose,
        HEADLINE_NOTE if layout in ("classified", "statement") else "",
        "For stat tiles, use ONLY metrics that appear in the brand profile; if none, use evergreen, "
        "brand-true labels and avoid invented numbers." if layout == "classified" else "",
        _creative_block(),
        "Match the brand's real audience and tone. No profit/return promises; keep claims accurate.",
        SCHEMAS[layout],
        _EXTRAS_NOTE,
        "Output ONLY the raw JSON object. No markdown, no code fences, no commentary.",
    ]
    return "\n\n".join(p for p in parts if p)


def _coerce(layout, obj, ctx):
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
    if ctx:
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


def _extract_json(text):
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
        system, user, grounded, pin_ctx = SYSTEM, _build_korvus_prompt(layout, ctx), bool(ctx), ctx
    else:
        url = (data.get("brand_url") or "").strip() or BRAND_SITES.get(brand, "")
        profile = _fetch_brand_profile(url) if url else {"ok": False, "name": brand.title(), "url": ""}
        system, user, grounded, pin_ctx = GENERIC_SYSTEM, _build_brand_prompt(layout, profile), bool(profile.get("ok")), None

    try:
        msg = _anthropic().messages.create(
            model=MODEL, max_tokens=1100, temperature=1, system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        parsed = _extract_json(text)
        bg = str(parsed.pop("_bg", "") or "").strip().lower()
        accent = str(parsed.pop("_accent", "") or "").strip().lower()
        footer = str(parsed.pop("_footer", "") or "").strip()[:64]
        fields = _coerce(layout, parsed, pin_ctx)
        if not fields:
            raise ValueError("model returned no usable fields")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({
        "fields": fields, "layout": layout, "brand": brand, "grounded": grounded,
        "bg": bg if bg in BG_KEYS else None,
        "accent": accent if accent in ACCENT_KEYS else None,
        "footer": footer or None,
        "brand_name": (profile or {}).get("name") if brand != "korvus" else "Korvus",
    })


# ---- caption + current hashtags ----
CAPTION_RULES = {
    "instagram": "Instagram caption: a punchy hook line, then 1-2 short value lines, casual and confident. 6-12 hashtags.",
    "x": "X/Twitter post under 270 characters: a scroll-stopping first line then 1-2 short lines. 2-4 hashtags.",
    "tiktok": "TikTok caption: short, native, casual, with a hook. 4-8 hashtags.",
    "linkedin": "LinkedIn caption: a credible hook then a short value line. Professional. 3-5 hashtags.",
}


def _brand_context_for_caption(brand, brand_url):
    if brand == "korvus":
        return ("Korvus — a 24/7 market-intelligence terminal for index-futures day traders "
                "(korvus.industries). Awareness of what's moving markets and why, including overnight.")
    p = _fetch_brand_profile(brand_url) if brand_url else None
    if p and p.get("ok"):
        return (p.get("name", "") + ". " + (p.get("desc") or "") + "\n" + (p.get("text") or ""))[:1800]
    return (p or {}).get("name", "the brand")


@korvus_fill_api.route("/api/promo-caption", methods=["POST"])
@admin_required
def promo_caption():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set on the server."}), 500
    d = request.get_json(force=True, silent=True) or {}
    platform = (d.get("platform") or "Instagram").strip()
    pkey = platform.lower()
    brand = (d.get("brand") or "korvus").strip().lower()
    brand_url = (d.get("brand_url") or "").strip() or BRAND_SITES.get(brand, "")
    ad_copy = (d.get("copy") or "").strip()[:600]
    brand_ctx = _brand_context_for_caption(brand, brand_url)

    system = SYSTEM if brand == "korvus" else GENERIC_SYSTEM
    user = (
        f"Write ONE {platform} caption for this promo, then the best CURRENT hashtags for maximum "
        f"visibility for this brand's niche on {platform}.\n\n"
        f"AD COPY:\n{ad_copy or '(none provided)'}\n\n"
        f"BRAND:\n{brand_ctx}\n\n"
        f"PLATFORM RULES: {CAPTION_RULES.get(pkey, CAPTION_RULES['instagram'])}\n"
        "Choose hashtags that are genuinely relevant and currently high-traffic — mix a few broad/"
        "high-volume tags with niche ones; no spammy or banned tags; each starts with '#', no spaces.\n"
        'Return ONLY raw JSON: {"caption":"...","hashtags":["#one","#two"]}. No markdown, no commentary.'
    )

    def _call(use_search):
        kwargs = dict(model=MODEL, max_tokens=900, temperature=0.9, system=system,
                      messages=[{"role": "user", "content": user}])
        if use_search:
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        msg = _anthropic().messages.create(**kwargs)
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()

    text = ""
    try:
        text = _call(True)                 # try with a live web search for fresh tags
    except Exception:
        try:
            text = _call(False)            # fall back: model knowledge only
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    if not text:
        try:
            text = _call(False)
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    try:
        obj = _extract_json(text)
        caption = str(obj.get("caption", "")).strip()
        tags = obj.get("hashtags", []) or []
        tags = ["#" + str(t).lstrip("#").strip() for t in tags if str(t).strip()]
    except Exception:
        caption, tags = text.strip(), []
    return jsonify({"caption": caption, "hashtags": tags, "platform": platform})


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

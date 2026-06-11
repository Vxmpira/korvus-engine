"""
Korvus — Promo Studio "Auto-fill" backend
==========================================
Adds the routes the Auto-fill button in agent.html needs:

    POST /api/promo-fill        (owner / admin only)  -> fills a whole layout
    GET  /agent_autofill.js     (owner / admin only)  -> serves the add-on script

Given the current layout (and, optionally, live engine context the browser
pulls from /api/news + /api/smt), /api/promo-fill asks Claude to fill EVERY
text field of that layout at once and returns a strict JSON object whose keys
match the agent.html state object exactly. Press the button again -> a fresh
variation.

It reuses your existing brand voice + guardrails (SYSTEM), the Anthropic client,
and the same server-side owner gate (admin_required) — so nothing here can be
edited from the browser and no second API key is needed.

Wire-up: just upload this file + agent_autofill.js + patch_autofill.py into the
project folder and run `python patch_autofill.py`. (Or register manually:)

    from korvus_fill_api import korvus_fill_api
    app.register_blueprint(korvus_fill_api)
"""

import os
import re
import json

from flask import Blueprint, request, jsonify, send_from_directory

from korvus_admin import admin_required               # same owner gate as the rest
from korvus_promo_api import _anthropic, MODEL, SYSTEM  # reuse client + brand voice

korvus_fill_api = Blueprint("korvus_fill_api", __name__)

HERE = os.path.dirname(os.path.abspath(__file__))      # serve the add-on from the project dir


# ----------------------------------------------------------------------------
# TRUE product facts — the ONLY metrics the model may use for stat tiles /
# pricing. Hard brand rule: never invent statistics or prices.
# ----------------------------------------------------------------------------
PRODUCT_FACTS = (
    "TRUE product facts (use verbatim or lightly reworded — NEVER invent others):\n"
    "  - The engine runs 24/7 / always on.\n"
    "  - It scores 60+ market items a day (use the value '60+').\n"
    "  - Every item is tagged HIGH / MED / LOW with a bull/bear read and a confidence score.\n"
    "  - It watches SMT divergence across NQ / ES / YM.\n"
    "  - Pricing: Free to start; Pro is $3.99/mo. No other prices exist.\n"
)

# Signature 3-part headline pattern, shown so the model keeps the brand cadence.
HEADLINE_NOTE = (
    "The headline is THREE parts that read as ONE sentence and land the Korvus "
    "awareness punch — you knew, they didn't. Example cadence: "
    "'You saw' / 'this coming.' / \"They didn't.\""
)

# ----------------------------------------------------------------------------
# Per-layout JSON schema the model must return. Keys match agent.html state.
# ----------------------------------------------------------------------------
SCHEMAS = {
    "classified": """Return STRICT JSON, EXACTLY these keys, all values strings unless noted:
{
  "c_eyebrow":  "ALL-CAPS status line, e.g. 'CLASSIFIED · MARKET INTEL · 03:14 AM ET'",
  "c_chip1":    "short ALL-CAPS tag <=14 chars, e.g. 'FED HOLD'",
  "c_chip2":    "short ALL-CAPS tag, e.g. 'NQ GAP +0.8%'",
  "c_chip3":    "short ALL-CAPS tag, e.g. 'DXY'",
  "c_conf":     "integer 0-100 as a string, e.g. '78'",
  "c_dir":      "EXACTLY one of: BULLISH | BEARISH | NEUTRAL",
  "c_hmuted":   "headline part 1 (muted lead), 1-3 words",
  "c_hlight":   "headline part 2 (light middle), 1-3 words",
  "c_haccent":  "headline part 3 (accent punchline), 1-3 words",
  "c_sub":      "1-2 line subline describing Korvus in plain English",
  "s1v": "stat tile 1 value",  "s1k": "stat tile 1 label (ALL-CAPS)",
  "s2v": "stat tile 2 value",  "s2k": "stat tile 2 label (ALL-CAPS)",
  "s3v": "stat tile 3 value",  "s3k": "stat tile 3 label (ALL-CAPS)",
  "c_calla":    "closing line 1, a full short sentence",
  "c_callb":    "closing line 2 START (leave a trailing space if it flows into the bold phrase)",
  "c_callbold": "closing line 2 BOLD phrase (the gut-punch ending)"
}
Stat tiles MUST use only the true product facts.""",

    "engine": """Return STRICT JSON, EXACTLY these keys (e1/e2 are nested objects):
{
  "e_eyebrow": "ALL-CAPS, e.g. 'WHILE YOU SLEEP'",
  "e_h1": "line 1, e.g. 'It reads.'",
  "e_h2": "line 2, e.g. 'It scores.'",
  "e_h3": "line 3 accent, e.g. 'It watches.'",
  "e_sub": "1-2 line subline",
  "e1": {
    "st": "status word, e.g. 'LIVE' or 'NEWSWIRE'",
    "tm": "time, e.g. '03:14 AM ET'",
    "imp": "HIGH | MED | LOW",
    "text": "ONE sentence: the scored headline in plain English",
    "dir": "short label, e.g. 'Bullish' or 'Neutral-Bullish'",
    "dk": "bull | neut | bear",
    "tk": "tickers, e.g. 'NQ · ES · DXY'",
    "cf": "confidence 0-100 as a string"
  },
  "e2": { "st":"...","tm":"...","imp":"...","text":"...","dir":"...","dk":"...","tk":"...","cf":"..." },
  "e_cta": "e.g. 'Start free'",
  "e_pbig": "big price, e.g. '$0'",
  "e_pline": "price line, e.g. 'FREE · PRO $3.99/MO'"
}
Card e1 is the freshest / highest-impact item; e2 a second, lower-impact item.""",

    "statement": """Return STRICT JSON, EXACTLY these keys, all values strings:
{
  "c_hmuted":   "headline part 1, 1-2 words",
  "c_hlight":   "headline part 2, 1-2 words",
  "c_haccent":  "headline part 3 (accent), 1-2 words",
  "c_sub":      "one tense, present-moment subline about the headline breaking now",
  "c_calla":    "closing line 1, a full short sentence",
  "c_callb":    "closing line 2 START (trailing space if it flows into the bold phrase)",
  "c_callbold": "closing line 2 BOLD phrase"
}""",
}

# Which keys we accept back per layout (everything else is dropped).
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


def _ctx_block(ctx: dict) -> str:
    """Fold live engine context into the prompt (or say there is none)."""
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
    bits.append("Use the real instruments for chips/tickers, the real impact/direction, "
                "and the real confidence number. The mockup must MATCH this item.")
    return "\n".join(bits)


def _build_user_prompt(layout: str, ctx: dict) -> str:
    schema = SCHEMAS[layout]
    parts = [
        f"Fill EVERY field of the Korvus promo '{layout}' mockup at once.",
        _ctx_block(ctx),
        HEADLINE_NOTE if layout in ("classified", "statement") else "",
        PRODUCT_FACTS if layout in ("classified", "engine") else "",
        "Make this a FRESH variation — distinct phrasing from the obvious defaults, "
        "but still on-brand: sharp, confident, nocturnal, credible. Never promise "
        "profits, returns, win-rates, or guaranteed outcomes; this is awareness, not advice.",
        schema,
        "Output ONLY the raw JSON object. No markdown, no code fences, no commentary.",
    ]
    return "\n\n".join(p for p in parts if p)


def _coerce(layout: str, obj: dict, ctx: dict) -> dict:
    """Whitelist keys, coerce types, and pin grounded numbers to the live feed."""
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

    # --- pin to the live feed so the visible numbers are TRUE, not invented ---
    if ctx:
        dirn = str(ctx.get("direction") or "").lower()
        conf = ctx.get("confidence")
        imp  = str(ctx.get("impact") or "").upper()
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


@korvus_fill_api.route("/api/promo-fill", methods=["POST"])
@admin_required
def promo_fill():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set on the server."}), 500

    data = request.get_json(force=True, silent=True) or {}
    layout = (data.get("layout") or "classified").strip().lower()
    if layout not in SCHEMAS:
        return jsonify({"error": f"unknown layout '{layout}'"}), 400
    ctx = data.get("context") or {}

    user = _build_user_prompt(layout, ctx)
    try:
        msg = _anthropic().messages.create(
            model=MODEL,
            max_tokens=900,
            temperature=1,                 # variety on every press = a real "refresh"
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        fields = _coerce(layout, _extract_json(text), ctx)
        if not fields:
            raise ValueError("model returned no usable fields")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"fields": fields, "layout": layout, "grounded": bool(ctx)})


@korvus_fill_api.route("/agent_autofill.js")
@admin_required
def agent_autofill_js():
    """Serve the Auto-fill client script from the project dir (same place as agent.html)."""
    resp = send_from_directory(HERE, "agent_autofill.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

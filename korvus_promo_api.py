"""
Korvus — Promo Studio backend API
==================================

Exposes:  POST /api/promo-generate   (admin only)

It builds the brand prompt + guardrails SERVER-SIDE (so they can't be edited
from the browser), calls the Anthropic API with YOUR key, and returns the
generated posts as JSON.

Setup
-----
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...      # set on the SERVER, never in a page or in git

    # in app.py, near where `app` is created:
    from korvus_promo_api import korvus_promo_api
    app.register_blueprint(korvus_promo_api)

Request body (JSON):
    {
      "platform": "TikTok",        # TikTok | Instagram | X | Discord | LinkedIn
      "angle": "...", "tone": "...", "extra": "...",
      "hashtags": true, "cta": true, "useContext": true,
      "context": { "headline": "...", "impact": "HIGH", "smt": "..." }
    }

Response: { "items": [ ... ] }  or  { "error": "..." }
"""

import os
import json
from flask import Blueprint, request, jsonify
import anthropic

from korvus_admin import admin_required   # reuse the same server-side gate

korvus_promo_api = Blueprint("korvus_promo_api", __name__)

# Model lives here so it's a one-line change.
#   claude-sonnet-4-6           -> balanced (default)
#   claude-haiku-4-5-20251001   -> cheaper / faster
#   claude-opus-4-8             -> highest quality
MODEL = "claude-sonnet-4-6"

_client = None
def _anthropic():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the environment
    return _client


SYSTEM = (
    "You are the promo copywriter for KORVUS — a 24/7 market-intelligence platform for "
    "index-futures day traders (korvus.industries), by BlackCrownVxJ.LLC. Korvus puts three "
    "things on one live dashboard: (1) a NEWS SCORING engine rating the real market impact of "
    "headlines and filtering noise, (2) SMT DIVERGENCE signals across NQ/ES/YM that flag when the "
    "indices disagree, (3) FUNDS & SESSION status in real time. Core promise: AWARENESS — knowing "
    "what's moving markets and WHY, including overnight while most traders sleep, so you're never "
    "blindsided. Voice: sharp, confident, nocturnal, a touch outlaw, always credible. You sell a "
    "feeling: relief from being caught off guard and the quiet edge of being informed first. HARD "
    "RULES, never broken: never promise profits, returns, win-rates, or guaranteed outcomes; never "
    "frame it as financial advice or signals to act on (it is intelligence/awareness); never invent "
    "statistics, prices, or testimonials, and never go beyond any live context you're given; keep "
    "feature claims accurate; index-futures trading carries real risk and you never imply otherwise."
)

VIDEO = {"TikTok", "Instagram"}

PLATFORM_RULES = {
    "TikTok": ("Native TikTok energy for retail index-futures day traders. HOOK = on-screen text for "
               "the first 1-2 seconds that stops the scroll. CAPTION = short, casual, real. SCRIPT = a "
               "15-30s concept in 3-5 fast beats (what's on screen / what's said), each beat on its own "
               "line starting with a dash."),
    "Instagram": ("Instagram Reel format. HOOK = bold on-screen opening text. CAPTION = punchy with one "
                  "clear value line, line breaks ok. SCRIPT = a 3-5 beat Reel concept, each beat on its "
                  "own line starting with a dash. Visual and confident."),
    "X": ("Strong X/Twitter post: scroll-stopping first line then 1-3 short lines, under 280 characters, "
          "line breaks welcome."),
    "Discord": ("Community announcement for the Eclipse-X / Korvus Discord. Casual but sharp, a few short "
                "lines, for traders already in the room. No @everyone/@here."),
    "LinkedIn": ("Credible LinkedIn post: tight hook line then a short paragraph or 3-4 punchy value lines. "
                 "Professional, minimal emojis."),
}


def build_user_prompt(d):
    p = d.get("platform", "X")
    video = p in VIDEO
    want_hash = bool(d.get("hashtags"))
    want_cta = bool(d.get("cta"))
    extra = (d.get("extra") or "").strip()
    use_ctx = bool(d.get("useContext"))
    ctx = d.get("context") or {}
    headline = (ctx.get("headline") or "").strip()
    impact = (ctx.get("impact") or "").strip()
    smt = (ctx.get("smt") or "").strip()

    ctx_block = ""
    if use_ctx and (headline or smt):
        lines = ["LIVE MARKET CONTEXT (real and current — weave in naturally; do NOT invent any "
                 "number, price, or fact beyond these lines):"]
        if headline:
            lines.append("- Top market driver: " + headline + (f" (impact: {impact})" if impact else ""))
        if smt:
            lines.append("- SMT read across NQ/ES/YM: " + smt)
        ctx_block = "\n".join(lines)

    rules = PLATFORM_RULES.get(p, PLATFORM_RULES["X"])

    if video:
        schema = ('Return ONLY a raw JSON array of exactly 3 objects, each '
                  '{"hook":"...","caption":"...","script":"..."}. No markdown, no code fences, no commentary.')
        hashtag = ("Put 3-6 relevant non-spammy hashtags in the caption (e.g. #daytrading #futures #NQ #ES "
                   "#trading).") if want_hash else "No hashtags."
        cta = ("End the caption with a soft CTA — 'link in bio' or korvus.industries.") if want_cta else "No links or CTA."
        noun = "concepts"
    else:
        schema = ('Return ONLY a raw JSON array of exactly 3 objects, each {"post":"..."}. '
                  'No markdown, no code fences, no commentary.')
        hashtag = ("Include 2-4 relevant non-spammy hashtags where the platform expects them.") if want_hash else "No hashtags."
        cta = ("End with a soft CTA pointing to korvus.industries.") if want_cta else "No links or CTA."
        noun = "posts"

    parts = [
        f"Write 3 distinct {p} promo {noun} for Korvus.",
        ctx_block,
        "ANGLE: " + (d.get("angle") or "") + ".",
        "TONE: " + (d.get("tone") or "") + ".",
        ("ALSO WEAVE IN: " + extra + ".") if extra else "",
        "PLATFORM RULES: " + rules,
        hashtag,
        cta,
        "" if ctx_block else "No live context provided — keep it evergreen and do not reference specific current events.",
        "The 3 variants must differ genuinely in hook and structure, not three rewrites of one line.",
        schema,
    ]
    return "\n".join(x for x in parts if x)


@korvus_promo_api.route("/api/promo-generate", methods=["POST"])
@admin_required
def promo_generate():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set on the server."}), 500

    data = request.get_json(force=True, silent=True) or {}
    user = build_user_prompt(data)

    try:
        msg = _anthropic().messages.create(
            model=MODEL,
            max_tokens=1000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        items = json.loads(text)
        if not isinstance(items, list) or not items:
            raise ValueError("model did not return a JSON array")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"items": items})

#!/usr/bin/env python3
"""
==============================================================================
 KORVUS SERVER  ·  Phase 2   (by BlackCrownVxJ.LLC)
==============================================================================
 The bridge between the engine and the dashboard.

   korvus_engine.py  ──writes──►  korvus.db  ──reads──►  THIS SERVER  ──►  dashboard
                                                         (localhost)

 A browser can't open a database file directly, so this tiny server reads
 korvus.db and serves the data as JSON at http://localhost:8000/api/news .
 It also serves the dashboard page itself, so you just open one URL.

 HOW TO RUN
   1. Make sure the engine has run at least once (so korvus.db exists)
   2. pip install -r requirements.txt      (adds flask)
   3. python korvus_server.py
   4. Open the link it prints:  http://localhost:8000
==============================================================================
"""
import os
import json
import sqlite3
import datetime as dt
from flask import Flask, jsonify, send_from_directory, request, redirect, session
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
import korvus_auth as auth
import korvus_billing as billing

# Owner/admin gate - comma-separated usernames in .env (e.g. ADMIN_USERNAMES=vxj).
# Empty by default, which means nobody is an admin until you set it.
ADMIN_USERNAMES = {u.strip() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip()}

# Tradovate live-feed gate - ONLY this single username ever receives the real
# CME quotes from your own Tradovate entitlement. Set TRADOVATE_OWNER=Vxmpira.N
# in .env. Empty by default, which means the live feed is served to nobody.
# This is what keeps the real-time data personal-use: it never reaches a member.
TRADOVATE_OWNER = os.getenv("TRADOVATE_OWNER", "").strip()
TRADOVATE_ROOTS = ["MNQ", "MES"]
_tv_started = False

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus.db")

app = Flask(__name__)
# SECRET_KEY signs the session cookie. Set a real one in .env for production.
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")

# make sure the users table exists on boot
auth.init_auth_db()

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

# Owner v2 console + Promo Studio live in blueprints. Registering them here wires:
#   /admin               -> admin.html  (hardened v2 console)
#   /agent               -> agent.html  (Promo Studio)
#   /api/admin/stats     -> hardened, self-sufficient stats
#   /api/promo-generate, /api/promo-image  -> owner-gated generation
# Both gate themselves server-side (korvus_admin.is_admin / admin_required).
from korvus_admin import korvus_admin
from korvus_promo_api import korvus_promo_api
app.register_blueprint(korvus_admin)
app.register_blueprint(korvus_promo_api)


class KorvusUser(UserMixin):
    """Thin wrapper so Flask-Login can track the logged-in member."""
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.email = row["email"]
        self.tier = row["tier"]
        self.email_verified = bool(row["email_verified"])
        keys = row.keys()
        self.subscription_status = row["subscription_status"] if "subscription_status" in keys else None
        self.current_period_end = row["current_period_end"] if "current_period_end" in keys else None
        self.stripe_customer_id = row["stripe_customer_id"] if "stripe_customer_id" in keys else None


@login_manager.user_loader
def load_user(user_id):
    row = auth.get_user_by_id(user_id)
    return KorvusUser(row) if row else None


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def to_et(iso_utc: str) -> str:
    """Turn a stored UTC timestamp into a 12-hour ET time like '9:41 AM'."""
    if not iso_utc:
        return ""
    try:
        # stored as timezone-aware UTC ISO; fall back gracefully
        d = dt.datetime.fromisoformat(iso_utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        # ET is UTC-5 (EST) / UTC-4 (EDT). Use a fixed -4 if zoneinfo absent.
        try:
            from zoneinfo import ZoneInfo
            et = d.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            et = d.astimezone(dt.timezone(dt.timedelta(hours=-4)))
        return et.strftime("%-I:%M %p") if os.name != "nt" else et.strftime("%#I:%M %p")
    except Exception:
        return ""


@app.route("/api/news")
def api_news():
    """Return the latest scored items as the dashboard's news format."""
    if not current_user.is_authenticated:
        return jsonify({"error": "login required", "items": []}), 401
    if not os.path.exists(DB_PATH):
        return jsonify({"error": "korvus.db not found - run the engine first", "items": []})
    conn = db()
    # hide items Claude flagged as pure non-market noise. Guard for older DBs
    # that may not have the column yet.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    noise_clause = "AND COALESCE(noise,0)=0" if "noise" in cols else ""
    rows = conn.execute(
        f"SELECT * FROM items WHERE processed=1 {noise_clause} ORDER BY created_at DESC LIMIT 60"
    ).fetchall()
    conn.close()

    items = []
    for r in rows:
        items.append({
            "time": to_et(r["created_at"]),
            "source": r["source"],          # 'wire' | 'reddit' | 'x'
            "source_name": r["source_name"] if "source_name" in r.keys() else "",
            "headline": r["headline"],
            "summary": r["summary"] or "",
            "impact_desc": (r["impact_desc"] if "impact_desc" in r.keys() else "") or "",
            "raw": r["raw_text"] or "",      # original blurb, for the detail view
            "impact": r["impact"] or "low",
            "dir": r["direction"] or "neut",
            "inst": json.loads(r["instruments"] or "[]"),
            "conf": r["confidence"] or 0,
            "url": r["url"] or "",
            "category": (r["category"] if "category" in r.keys() else "general") or "general",
        })
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/health")
def api_health():
    """Quick status so the dashboard can show engine liveness."""
    exists = os.path.exists(DB_PATH)
    total = scored = 0
    last = ""
    if exists:
        conn = db()
        total = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        scored = conn.execute("SELECT COUNT(*) c FROM items WHERE processed=1").fetchone()["c"]
        row = conn.execute("SELECT created_at FROM items ORDER BY created_at DESC LIMIT 1").fetchone()
        last = to_et(row["created_at"]) if row else ""
        conn.close()
    return jsonify({"db": exists, "total": total, "scored": scored, "last_update": last})


@app.route("/api/ff-calendar")
def api_ff_calendar():
    """This-week economic calendar for the Forex / Gov News page.
    The provider module (korvus_calendar.py) fetches Forex Factory / FMP
    server-side and caches it, so the browser never trips the feed's CORS
    block or rate limit. Degrades gracefully - a missing module or failed
    fetch just returns an empty list and the page shows its sample week."""
    try:
        from korvus_calendar import get_calendar
    except Exception as e:
        return jsonify({"error": f"calendar module not available: {e}", "events": []})
    try:
        events = get_calendar()
        return jsonify(events)          # page reads a bare array (or {events:[...]})
    except Exception as e:
        return jsonify({"error": str(e), "events": []})


@app.route("/")
def home():
    # logged-out visitors see the public landing page; members see the terminal
    if current_user.is_authenticated:
        return send_from_directory(HERE, "korvus_dashboard.html")
    return send_from_directory(HERE, "korvus_landing.html")


@app.route("/favicon.ico")
def favicon_ico():
    return send_from_directory(HERE, "favicon.ico")


@app.route("/favicon.png")
def favicon_png():
    return send_from_directory(HERE, "favicon.png")


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return send_from_directory(HERE, "apple-touch-icon.png")


@app.route("/og-image.png")
def og_image():
    return send_from_directory(HERE, "og-image.png")


@app.route("/terminal")
@login_required
def terminal():
    # explicit terminal route (always gated)
    return send_from_directory(HERE, "korvus_dashboard.html")


@app.route("/forex")
@login_required
def forex():
    # Forex / Gov News economic calendar - a sibling of the terminal (gated)
    return send_from_directory(HERE, "korvus_forex_calendar.html")


# ----------------------------------------------------------------------------
# AUTH ROUTES
# ----------------------------------------------------------------------------
def _render(page, message_html=""):
    """Load a static auth page and inject a message into the <!--MESSAGE--> slot."""
    with open(os.path.join(HERE, page), encoding="utf-8") as f:
        html = f.read()
    return html.replace("<!--MESSAGE-->", message_html)


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if current_user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        ok, msg, token = auth.create_user(
            request.form.get("username"),
            request.form.get("email"),
            request.form.get("password"),
        )
        if ok:
            auth.send_verification_email(request.form.get("email"), token)
            return _render("korvus_signup.html",
                f'<div class="msg ok">{msg} We sent a verification link to your email. '
                f'You can <a href="/login" style="color:#9fe9bd;text-decoration:underline">log in</a> now.</div>')
        return _render("korvus_signup.html", f'<div class="msg err">{msg}</div>')
    return _render("korvus_signup.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        row = auth.verify_password(request.form.get("username"), request.form.get("password"))
        if row:
            login_user(KorvusUser(row), remember=True)
            return redirect("/")
        return _render("korvus_login.html",
            '<div class="msg err">Incorrect username or password.</div>')
    return _render("korvus_login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect("/")


@app.route("/verify")
def verify():
    ok = auth.confirm_email(request.args.get("token"))
    note = ("Your email is verified - thank you." if ok
            else "That verification link is invalid or already used.")
    cls = "ok" if ok else "err"
    return _render("korvus_login.html", f'<div class="msg {cls}">{note}</div>')


@app.route("/forgot")
def forgot_page():
    return send_from_directory(HERE, "korvus_forgot.html")


@app.route("/reset")
def reset_page():
    return send_from_directory(HERE, "korvus_reset.html")


@app.route("/api/forgot", methods=["POST"])
def api_forgot():
    data = request.get_json(silent=True) or {}
    token, email = auth.create_reset_token(data.get("email"))
    if token:
        auth.send_reset_email(email, token)
    # generic response either way - never reveal whether an account exists
    return jsonify({"ok": True,
                    "message": "If an account exists for that email, a reset link is on its way."})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    data = request.get_json(silent=True) or {}
    ok, msg = auth.reset_password(data.get("token"), data.get("new"))
    return (jsonify({"ok": ok, "message": msg}), 200 if ok else 400)


@app.route("/api/me")
def api_me():
    """Lets the dashboard / account page know who's logged in, their tier,
    profile fields, and billing state."""
    if current_user.is_authenticated:
        _online[current_user.id] = dt.datetime.now(dt.timezone.utc)
        u = auth.get_user_by_id(current_user.id) or {}
        # Whether the configured feed serves REAL CME futures (Databento). In
        # native mode EVERY tier prices the real contracts: Pro reads the live
        # tape, free reads the same tape time-shifted 15 minutes server-side.
        _native_fut = False
        try:
            from korvus_quotes import native_futures
            _native_fut = bool(native_futures())
        except Exception:
            _native_fut = False
        return jsonify({"auth": True,
                        "username": u.get("username"),
                        "email": u.get("email"),
                        "tier": u.get("tier"),
                        "verified": bool(u.get("email_verified")),
                        "subscription_status": u.get("subscription_status"),
                        "current_period_end": u.get("current_period_end"),
                        "alert_opt_in": int(u.get("alert_opt_in") if u.get("alert_opt_in") is not None else 1),
                        "is_owner": bool(TRADOVATE_OWNER and u.get("username") == TRADOVATE_OWNER),
                        "native_futures": _native_fut,
                        "billing_enabled": billing.billing_enabled(),
                        "price_display": billing.price_display(),
                        "yearly_enabled": billing.yearly_enabled(),
                        "price_display_yearly": billing.price_display_yearly()})
    return jsonify({"auth": False,
                    "billing_enabled": billing.billing_enabled(),
                    "price_display": billing.price_display(),
                    "yearly_enabled": billing.yearly_enabled(),
                    "price_display_yearly": billing.price_display_yearly()})


# ----------------------------------------------------------------------------
# BILLING (Stripe subscriptions) - see korvus_billing.py
# ----------------------------------------------------------------------------
@app.route("/upgrade")
@login_required
def upgrade_page():
    return send_from_directory(HERE, "korvus_upgrade.html")


@app.route("/api/billing/checkout", methods=["POST"])
@login_required
def billing_checkout():
    data = request.get_json(silent=True) or {}
    interval = "year" if data.get("interval") == "year" else "month"
    user = auth.get_user_by_id(current_user.id)
    url, err = billing.create_checkout_url(user, interval=interval)
    return (jsonify({"url": url}) if url else (jsonify({"error": err}), 400))


# ----------------------------------------------------------------------------
# ACCOUNT / PROFILE
# ----------------------------------------------------------------------------
@app.route("/account")
@login_required
def account_page():
    return send_from_directory(HERE, "korvus_account.html")


@app.route("/api/account/username", methods=["POST"])
@login_required
def account_username():
    data = request.get_json(silent=True) or {}
    ok, msg = auth.update_username(current_user.id, data.get("username"))
    return (jsonify({"ok": ok, "message": msg}), 200 if ok else 400)


@app.route("/api/account/password", methods=["POST"])
@login_required
def account_password():
    data = request.get_json(silent=True) or {}
    ok, msg = auth.change_password(current_user.id, data.get("current"), data.get("new"))
    return (jsonify({"ok": ok, "message": msg}), 200 if ok else 400)


@app.route("/api/account/email", methods=["POST"])
@login_required
def account_email():
    data = request.get_json(silent=True) or {}
    ok, msg, token, new_email = auth.change_email(current_user.id, data.get("email"))
    if ok:
        auth.send_verification_email(new_email, token)
    return (jsonify({"ok": ok, "message": msg}), 200 if ok else 400)


@app.route("/api/account/resend-verification", methods=["POST"])
@login_required
def account_resend():
    u = auth.get_user_by_id(current_user.id) or {}
    token, email = auth.resend_verification(u.get("email"))
    if token:
        auth.send_verification_email(email, token)
    return jsonify({"ok": True, "message": "If your email needs verifying, a new link is on its way."})


@app.route("/api/account/alerts", methods=["POST"])
@login_required
def account_alerts():
    """Toggle high-impact email alerts for the current user."""
    data = request.get_json(silent=True) or {}
    auth.set_alert_opt_in(current_user.id, bool(data.get("on")))
    return jsonify({"ok": True,
                    "message": "Alerts on." if data.get("on") else "Alerts off."})


@app.route("/api/billing/portal", methods=["POST"])
@login_required
def billing_portal():
    user = auth.get_user_by_id(current_user.id)
    url, err = billing.create_portal_url(user)
    return (jsonify({"url": url}) if url else (jsonify({"error": err}), 400))


@app.route("/api/billing/webhook", methods=["POST"])
def billing_webhook():
    # raw body + signature header are required for verification
    status, msg = billing.handle_webhook(request.get_data(),
                                          request.headers.get("Stripe-Signature", ""))
    return (msg, status)


# in-memory "currently online" tracker: user_id -> last-seen UTC.
# Counts sessions seen in the last 5 minutes. Resets on server restart
# (fine - it's a live gauge, not a stored stat).
_online = {}

@app.route("/api/online")
def api_online():
    """Real count of members active in the last 5 minutes."""
    now = dt.datetime.now(dt.timezone.utc)
    if current_user.is_authenticated:
        _online[current_user.id] = now
    cutoff = now - dt.timedelta(minutes=5)
    active = [uid for uid, seen in list(_online.items()) if seen >= cutoff]
    # tidy stale entries
    for uid in list(_online.keys()):
        if _online[uid] < cutoff:
            del _online[uid]
    return jsonify({"online": len(active)})


# ----------------------------------------------------------------------------
# OWNER / ADMIN DASHBOARD  (gated by ADMIN_USERNAMES in .env)
# ----------------------------------------------------------------------------
def _is_admin():
    return current_user.is_authenticated and current_user.username in ADMIN_USERNAMES


def _is_tradovate_owner():
    """True only for the single owner account named in TRADOVATE_OWNER.
    Used to gate the live CME feed so it never reaches a paying member -
    the real-time data stays the owner's own personal-use entitlement."""
    return (current_user.is_authenticated and TRADOVATE_OWNER
            and current_user.username == TRADOVATE_OWNER)


@app.route("/legal")
def legal():
    # serves the Terms of Service / Privacy / Risk Disclosure page
    return send_from_directory(HERE, "korvus_legal.html")


# ----------------------------------------------------------------------------
# PHASE 3 - live quotes for the panels
# ----------------------------------------------------------------------------
from flask import request

WATCHLIST_PATH = os.path.join(HERE, "watchlist.json")


@app.route("/api/tradovate")
@login_required
def api_tradovate():
    """OWNER-ONLY live CME quotes (MNQ/MES) from the owner's own Tradovate feed.

    Hard-gated to TRADOVATE_OWNER: any other account gets 403, so the
    real-time exchange data is NEVER served to a member. That gate is what
    keeps this personal-use rather than redistribution. The background socket
    starts lazily on the first owner request and only if TRADOVATE_* creds are
    set in .env (and the account holds a CME data entitlement)."""
    if not _is_tradovate_owner():
        return jsonify({"error": "forbidden"}), 403
    try:
        import korvus_tradovate as tv
    except Exception as e:
        return jsonify({"configured": False, "quotes": {}, "note": f"module unavailable: {e}"})

    configured = bool(tv.TRADOVATE_USERNAME and tv.TRADOVATE_PASSWORD and tv.TRADOVATE_SECRET)
    if not configured:
        return jsonify({"configured": False, "quotes": {},
                        "note": "set TRADOVATE_* in .env (and hold a CME data subscription)"})

    global _tv_started
    client = tv.get_client()
    if not _tv_started:
        try:
            client.start(TRADOVATE_ROOTS)   # idempotent; spins up one bg socket
            _tv_started = True
        except Exception as e:
            return jsonify({"configured": True, "quotes": {}, "note": f"start error: {e}"})

    return jsonify({"configured": True,
                    "env": tv.TRADOVATE_ENV,
                    "quotes": client.get(TRADOVATE_ROOTS)})


@app.route("/api/quotes")
def api_quotes():
    """
    Live prices for a comma-separated ?symbols= list.
    TIER ENFORCEMENT (server-side, not bypassable):
      - pro users get the configured live provider (e.g. alphavantage)
      - free / logged-out users are forced onto the delayed feed
    """
    try:
        from korvus_quotes import get_quotes
    except Exception as e:
        return jsonify({"error": f"quotes module not available: {e}", "quotes": {}})
    symbols = (request.args.get("symbols") or "").split(",")
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        return jsonify({"quotes": {}, "_meta": {}})

    is_pro = current_user.is_authenticated and current_user.tier == "pro"
    # free/logged-out -> force delayed feed regardless of the configured provider
    data = get_quotes(symbols, force_delayed=not is_pro)
    meta = data.pop("_meta", {})
    meta["tier"] = "pro" if is_pro else "free"
    meta["delayed"] = not is_pro
    try:
        from korvus_quotes import FREE_DELAY_MIN
        meta["delay_min"] = 0 if is_pro else FREE_DELAY_MIN
    except Exception:
        pass
    return jsonify({"quotes": data, "meta": meta})


@app.route("/api/intraday")
def api_intraday():
    """
    Real intraday % series (price vs prev close) for the index tape charts.
    Pulls 5-minute bars from Alpha Vantage, server-cached and shared across all
    viewers. Tier-enforced the same way as /api/quotes: pro -> realtime
    entitlement, free / logged-out -> delayed. Returns an empty series per symbol
    on any miss so the client keeps its live-accumulated line.
    """
    try:
        from korvus_quotes import get_intraday
    except Exception as e:
        return jsonify({"error": f"intraday module not available: {e}", "_meta": {}})
    symbols = (request.args.get("symbols") or "").split(",")
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        return jsonify({"_meta": {}})

    is_pro = current_user.is_authenticated and current_user.tier == "pro"
    data = get_intraday(symbols, force_delayed=not is_pro)
    meta = data.pop("_meta", {}) or {}
    meta["tier"] = "pro" if is_pro else "free"
    data["_meta"] = meta
    return jsonify(data)


# Index proxies for the SMT panel: NQ->QQQ, ES->SPY, YM->DIA
_SMT_LEGS = [("NQ", "QQQ"), ("ES", "SPY"), ("YM", "DIA")]

@app.route("/api/smt")
def api_smt():
    """
    REAL intraday divergence between the three index proxies, computed from
    live data (no faked verdict). For each index we measure where price sits
    in TODAY's range: rangePos = (price - low) / (high - low), 0..1.
      ~1.0  -> trading at/near session highs   (confirming strength, 'HH')
      ~0.0  -> trading at/near session lows     (weakness, 'LL')
    Divergence = the indices disagree (one near highs while another lags).
    Honest labels only; if data is missing we say so rather than guess.
    """
    try:
        from korvus_quotes import get_quotes
    except Exception as e:
        return jsonify({"error": str(e), "legs": [], "verdict": None})

    is_pro = current_user.is_authenticated and current_user.tier == "pro"
    proxies = [p for _, p in _SMT_LEGS]
    data = get_quotes(proxies, force_delayed=not is_pro)
    meta = data.pop("_meta", {}) or {}
    market_open = bool(meta.get("market_open"))
    provider    = meta.get("provider", "")
    delayed     = provider in ("finnhub", "alphavantage-delayed")

    legs = []
    for sym, proxy in _SMT_LEGS:
        q = data.get(proxy) or {}
        price = q.get("price") or 0
        hi = q.get("high") or 0
        lo = q.get("low") or 0
        chg = q.get("chg_pct") or 0
        rng = hi - lo
        if price and rng > 0:
            pos = max(0.0, min(1.0, (price - lo) / rng))   # 0..1 in today's range
            # honest swing tag from range position
            if pos >= 0.80:   tag = "HH"   # holding session highs
            elif pos >= 0.55: tag = "MID+"
            elif pos >= 0.45: tag = "MID"
            elif pos >= 0.20: tag = "MID-"
            else:             tag = "LL"    # near session lows
            has_data = True
        else:
            pos, tag, has_data = None, "-", False
        legs.append({"sym": sym, "proxy": proxy, "chg": round(chg, 2),
                     "pos": (round(pos, 2) if pos is not None else None),
                     "swing": tag, "has_data": has_data})

    # ---- freshness: are we reading live RTH prices, a delayed feed, or a frozen
    # last-session close? The verdict is computed the same way, but we label its
    # provenance honestly instead of presenting a stale read as if it were live.
    if not market_open:
        freshness = "last_close"
        fresh_note = (" Markets are closed, so this reflects the last regular-session "
                      "close: the QQQ / SPY / DIA proxies do not trade overnight. Live "
                      "divergence resumes at the 9:30 AM ET open.")
    elif delayed:
        freshness = "delayed"
        fresh_note = (" Reading a 15-minute-delayed feed, so the position trails live "
                      "price by a few minutes.")
    else:
        freshness = "live"
        fresh_note = ""

    # ---- verdict, computed honestly from the leg positions + direction ----
    valid = [l for l in legs if l["has_data"]]
    if len(valid) < 2:
        verdict = {"state": "ok", "title": "Awaiting data",
                   "note": ("Index-range data is not available right now. The QQQ / SPY / DIA "
                            "proxies only trade during regular US hours (9:30 AM to 4:00 PM ET); "
                            "divergence resumes when they reopen.")}
    else:
        positions = [l["pos"] for l in valid]
        spread = max(positions) - min(positions)   # how far apart the indices sit in their ranges
        avg = sum(positions) / len(positions)
        leader  = max(valid, key=lambda l: l["pos"])
        laggard = min(valid, key=lambda l: l["pos"])
        # directional divergence: do the complexes disagree on the day's DIRECTION?
        # (one green while another is red). This is the cleanest, highest-confidence
        # non-confirmation and is caught even when range positions look similar.
        ups   = [l for l in valid if l["chg"] >  0.03]
        downs = [l for l in valid if l["chg"] < -0.03]
        directional = bool(ups) and bool(downs)

        if directional:
            up_s = " / ".join(l["sym"] for l in ups)
            dn_s = " / ".join(l["sym"] for l in downs)
            verdict = {"state": "warn",
                "title": f"Divergence: {up_s} up, {dn_s} down",
                "note": (f"The index complexes disagree on direction, {up_s} green while "
                         f"{dn_s} red. A clear non-confirmation: one is not following the "
                         f"others. Favor caution until they realign.")}
        elif spread >= 0.45:
            # genuine non-confirmation: one index strong, another clearly lagging
            verdict = {"state": "warn",
                "title": f"Divergence: {leader['sym']} leading, {laggard['sym']} lagging",
                "note": (f"{leader['sym']} is holding near its session highs while "
                         f"{laggard['sym']} lags in its range, so the indices are NOT confirming "
                         f"each other. Classic non-confirmation; favor caution until they realign.")}
        elif avg >= 0.70:
            verdict = {"state": "ok", "title": "Confirming: aligned strength",
                       "note": "All three indices are holding near session highs and confirming "
                               "each other. No divergence, trend in agreement."}
        elif avg <= 0.30:
            verdict = {"state": "ok", "title": "Confirming: aligned weakness",
                       "note": "All three indices are near session lows together and confirming "
                               "each other to the downside. No divergence."}
        else:
            verdict = {"state": "ok", "title": "In line: no divergence",
                       "note": "The indices are moving together in mid-range. No meaningful "
                               "non-confirmation to flag right now."}

        # Frozen overnight data: keep the read accurate but frame it as the prior
        # session and drop the live alarm state so a stale divergence cannot look live.
        if freshness == "last_close":
            verdict["title"] = "Last session: " + verdict["title"]
            verdict["state"] = "ok"

    verdict["note"] = (verdict.get("note", "") + fresh_note).strip()
    verdict["freshness"]   = freshness
    verdict["delayed"]     = delayed
    verdict["market_open"] = market_open

    return jsonify({"legs": legs, "verdict": verdict,
                    "freshness": freshness, "delayed": delayed,
                    "market_open": market_open})


@app.route("/api/watchlist", methods=["GET", "POST"])
@login_required
def api_watchlist():
    """
    Per-user personal watchlist. TIER ENFORCEMENT:
      - saving a personal watchlist (POST) is PRO-only
      - free users always get the default board (custom=False)
    Stored per user as watchlist_<userid>.json so members don't share lists.
    """
    user_path = os.path.join(HERE, f"watchlist_{current_user.id}.json")
    is_pro = current_user.tier == "pro"

    if request.method == "POST":
        if not is_pro:
            return jsonify({"ok": False, "error": "Personal watchlists are a Pro feature.",
                            "upgrade": True}), 403
        try:
            payload = request.get_json(force=True)
            with open(user_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            return jsonify({"ok": True, "saved": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    # GET - free users always get the default board
    if is_pro and os.path.exists(user_path):
        try:
            with open(user_path, encoding="utf-8") as f:
                wl = json.load(f)
            if wl and wl.get("groups"):
                return jsonify({"custom": True, "watchlist": wl})
        except Exception:
            pass
    return jsonify({"custom": False, "watchlist": None})


# ----------------------------------------------------------------------------
# Catch-all: unknown paths go to the front page instead of a bare 404.
# (Real API 404s above still return their own JSON; this only catches
#  unmatched page-style routes.)
# ----------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    # API calls should still get JSON 404s; everything else → home
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return redirect("/")


# ----------------------------------------------------------------------------
# Background warmer
# ----------------------------------------------------------------------------
# Keeps the full quote universe (futures + every Funds Watch / SMT / macro
# equity) and the economic calendar hot, so opening the terminal shows populated
# panels immediately instead of waiting on a cold fetch, and they refresh on a
# steady cadence. One daemon thread per worker process; starts on the first
# request (so it also works under Gunicorn's post-fork model) and on direct run.
import threading as _threading
import time as _time

_warmer_started = False
_warmer_lock = _threading.Lock()

def _warm_loop():
    last_cal = 0.0
    while True:
        try:
            from korvus_quotes import warm_quotes
            warm_quotes()               # also idempotently starts the CME feed
        except Exception as e:
            print(f"  [warm] quotes: {e}")
        now = _time.time()
        if now - last_cal > 180:        # economic calendar every 3 minutes
            try:
                from korvus_calendar import get_calendar
                get_calendar()
                last_cal = now
            except Exception as e:
                print(f"  [warm] calendar: {e}")
        _time.sleep(30)                 # quote-universe refresh cadence

def _start_warmer():
    global _warmer_started
    with _warmer_lock:
        if _warmer_started:
            return
        _warmer_started = True
    _threading.Thread(target=_warm_loop, name="korvus-warmer", daemon=True).start()
    print("  [warm] background warmer started")

@app.before_request
def _ensure_warmer():
    if not _warmer_started:             # starts once per worker, on first request
        _start_warmer()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  KORVUS server - open this in your browser:")
    print("      http://localhost:8000")
    print("=" * 60 + "\n")
    if not os.path.exists(DB_PATH):
        print("  ⚠  korvus.db not found yet. Run `python korvus_engine.py` first,")
        print("     then refresh the page. The dashboard will show live data once")
        print("     the engine has scored some items.\n")
    _start_warmer()
    app.run(host="127.0.0.1", port=8000, debug=False)

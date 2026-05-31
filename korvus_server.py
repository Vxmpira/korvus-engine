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

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus.db")

app = Flask(__name__)
# SECRET_KEY signs the session cookie. Set a real one in .env for production.
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")

# make sure the users table exists on boot
auth.init_auth_db()

login_manager = LoginManager(app)
login_manager.login_view = "login_page"


class KorvusUser(UserMixin):
    """Thin wrapper so Flask-Login can track the logged-in member."""
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.email = row["email"]
        self.tier = row["tier"]
        self.email_verified = bool(row["email_verified"])


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
        return jsonify({"error": "korvus.db not found — run the engine first", "items": []})
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE processed=1 ORDER BY created_at DESC LIMIT 60"
    ).fetchall()
    conn.close()

    items = []
    for r in rows:
        items.append({
            "time": to_et(r["created_at"]),
            "source": r["source"],          # 'wire' | 'reddit' | 'x'
            "headline": r["headline"],
            "summary": r["summary"] or "",
            "impact": r["impact"] or "low",
            "dir": r["direction"] or "neut",
            "inst": json.loads(r["instruments"] or "[]"),
            "conf": r["confidence"] or 0,
            "url": r["url"] or "",
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


@app.route("/")
def home():
    # logged-out visitors see the public landing page; members see the terminal
    if current_user.is_authenticated:
        return send_from_directory(HERE, "korvus_dashboard.html")
    return send_from_directory(HERE, "korvus_landing.html")


@app.route("/terminal")
@login_required
def terminal():
    # explicit terminal route (always gated)
    return send_from_directory(HERE, "korvus_dashboard.html")


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
    note = ("Your email is verified — thank you." if ok
            else "That verification link is invalid or already used.")
    cls = "ok" if ok else "err"
    return _render("korvus_login.html", f'<div class="msg {cls}">{note}</div>')


@app.route("/api/me")
def api_me():
    """Lets the dashboard know who's logged in and their tier."""
    if current_user.is_authenticated:
        return jsonify({"auth": True, "username": current_user.username,
                        "tier": current_user.tier, "verified": current_user.email_verified})
    return jsonify({"auth": False})


@app.route("/legal")
def legal():
    # serves the Terms of Service / Privacy / Risk Disclosure page
    return send_from_directory(HERE, "korvus_legal.html")


# ----------------------------------------------------------------------------
# PHASE 3 — live quotes for the panels
# ----------------------------------------------------------------------------
from flask import request

WATCHLIST_PATH = os.path.join(HERE, "watchlist.json")


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
    return jsonify({"quotes": data, "meta": meta})


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

    # GET — free users always get the default board
    if is_pro and os.path.exists(user_path):
        try:
            with open(user_path, encoding="utf-8") as f:
                wl = json.load(f)
            if wl and wl.get("groups"):
                return jsonify({"custom": True, "watchlist": wl})
        except Exception:
            pass
    return jsonify({"custom": False, "watchlist": None})


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  KORVUS server — open this in your browser:")
    print("      http://localhost:8000")
    print("=" * 60 + "\n")
    if not os.path.exists(DB_PATH):
        print("  ⚠  korvus.db not found yet. Run `python korvus_engine.py` first,")
        print("     then refresh the page. The dashboard will show live data once")
        print("     the engine has scored some items.\n")
    app.run(host="127.0.0.1", port=8000, debug=False)

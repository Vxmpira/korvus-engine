"""
Korvus - admin gating, pages, and owner analytics (hardened)
============================================================

Why this version exists: the first cut hardcoded the DB path and trusted the
gunicorn process to already have .env in its environment. On the evolved
deployment neither held, so the console reported "database missing" and all
keys "missing" even though the rest of the app reads them fine.

This version is self-sufficient:
  * DB path comes from korvus_auth.DB_PATH (the exact file the users table
    lives in) - falls back to ./korvus.db only if that import fails.
  * It loads .env itself (explicit paths), so key-presence checks are accurate
    no matter how gunicorn was launched.
  * It opens the DB read-only (mode=ro) so a wrong path can never silently
    create a phantom empty database.
  * The JSON includes a small `debug` block (resolved db_path, whether it
    exists, which .env was loaded) so any remaining mismatch is visible.

It never returns secrets: keys are reported present/absent only; passwords
are never read.
"""

import os
import re
import json
import time
import sqlite3
import datetime as dt
from functools import wraps
from flask import Blueprint, abort, send_from_directory, jsonify, request, session
from flask_login import current_user

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

HERE = os.path.dirname(os.path.abspath(__file__))
_STARTED = time.time()


# --- resolve the REAL users DB the same place the rest of the app does ------
def _resolve_db():
    try:
        import korvus_auth  # also triggers its module-level load_dotenv()
        p = getattr(korvus_auth, "DB_PATH", None)
        if p:
            return p
    except Exception:
        pass
    return os.path.join(HERE, "korvus.db")


DB_PATH = _resolve_db()

# --- make sure .env is in the environment for key-presence checks -----------
_ENV_FILE = None
if load_dotenv:
    for _cand in (os.path.join(HERE, ".env"),
                  os.path.join(os.path.dirname(DB_PATH), ".env"),
                  os.path.join(os.getcwd(), ".env")):
        if os.path.exists(_cand):
            load_dotenv(_cand, override=False)
            _ENV_FILE = _cand
            break

ADMIN_USERS = {"vxmpira.n"}


def is_admin():
    if not getattr(current_user, "is_authenticated", False):
        return False
    uname = (getattr(current_user, "username", "") or "").strip().lower()
    if uname in ADMIN_USERS:
        return True
    return bool(getattr(current_user, "is_owner", False))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


korvus_admin = Blueprint("korvus_admin", __name__)


@korvus_admin.route("/agent")
@admin_required
def agent_page():
    return send_from_directory(HERE, "agent.html")


@korvus_admin.route("/admin")
@admin_required
def admin_page():
    return send_from_directory(HERE, "admin.html")


def _db():
    # read-only: never create a phantom DB if the path is wrong
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _db_rw():
    # write-capable, but mode=rw (not rwc) keeps the same guarantee as _db():
    # if DB_PATH is wrong this fails loudly instead of creating an empty file.
    # The busy timeout rides out a concurrent write from auth or the engine.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=rw", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


@korvus_admin.route("/api/admin/stats")
@admin_required
def admin_stats():
    out = {"ok": True}

    # ---- users / tiers / subscriptions (schema-adaptive) -------------------
    try:
        if not os.path.exists(DB_PATH):
            raise FileNotFoundError(DB_PATH)
        conn = _db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}

        total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        by_tier = {}
        if "tier" in cols:
            by_tier = {r["tier"]: r["c"]
                       for r in conn.execute("SELECT tier, COUNT(*) c FROM users GROUP BY tier")}
        users = {"total": total, "pro": by_tier.get("pro", 0),
                 "free": by_tier.get("free", 0), "by_tier": by_tier}

        if "email_verified" in cols:
            users["verified"] = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE email_verified=1").fetchone()["c"]

        if "created_at" in cols:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
            users["new_7d"] = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE created_at >= ?", (cutoff,)).fetchone()["c"]
        out["users"] = users

        if "subscription_status" in cols:
            out["subscriptions"] = {
                (r["subscription_status"] or "none"): r["c"]
                for r in conn.execute(
                    "SELECT subscription_status, COUNT(*) c FROM users GROUP BY subscription_status")
            }

        if "tier" in cols and "email" in cols:
            sel = "username, email"
            if "created_at" in cols:
                sel += ", created_at"
            if "subscription_status" in cols:
                sel += ", subscription_status"
            order = "created_at DESC" if "created_at" in cols else "username"
            rows = conn.execute(
                f"SELECT {sel} FROM users WHERE tier='pro' ORDER BY {order}").fetchall()
            out["pro_members"] = [dict(r) for r in rows]

        if "created_at" in cols and "email" in cols:
            rows = conn.execute(
                "SELECT username, email, tier, created_at FROM users "
                "ORDER BY created_at DESC LIMIT 12").fetchall()
            out["recent_signups"] = [dict(r) for r in rows]

        # ---- LodeStone access roster: everyone with a TradingView username
        #      saved, plus anyone still flagged granted (so revokes are never
        #      lost when a member clears their name). Grant/revoke on
        #      TradingView is manual; the flag here is the owner's ledger. ---
        if "tv_username" in cols:
            has_flag = "lodestone_granted" in cols
            sel = "username, email, tier, tv_username"
            if "subscription_status" in cols:
                sel += ", subscription_status"
            sel += ", lodestone_granted" if has_flag else ", 0 AS lodestone_granted"
            where = "(tv_username IS NOT NULL AND tv_username != '')"
            if has_flag:
                where += " OR lodestone_granted = 1"
            rows = conn.execute(
                f"SELECT {sel} FROM users WHERE {where} "
                "ORDER BY (tier='pro') DESC, username COLLATE NOCASE").fetchall()
            out["lodestone"] = [dict(r) for r in rows]

        conn.close()
    except Exception as e:
        out["users_error"] = str(e)

    # ---- revenue (clearly an estimate) -------------------------------------
    try:
        # Single source of truth: the same Stripe display price the site shows.
        # Falls back to PRO_PRICE_MONTHLY, then 0 (never a stale hardcode).
        raw = os.getenv("STRIPE_PRICE_DISPLAY") or os.getenv("PRO_PRICE_MONTHLY") or "0"
        price = float(re.sub(r"[^0-9.]", "", raw) or 0)
        paying = None
        basis = "pro-tier member count"
        if out.get("subscriptions", {}).get("active") is not None:
            paying = out["subscriptions"]["active"]
            basis = "active subscriptions"
        if paying is None:
            paying = out.get("users", {}).get("pro", 0)
        out["revenue"] = {
            "monthly_price": price,
            "paying_members": paying,
            "estimated_mrr": round(price * (paying or 0), 2),
            "estimated_arr": round(price * (paying or 0) * 12, 2),
            "basis": basis,
            "note": "Estimate from the user table. For exact billed revenue, "
                    "wire your Stripe/billing module.",
        }
    except Exception as e:
        out["revenue_error"] = str(e)

    # ---- API key + data-source + health (presence only, never values) ------
    out["status"] = {
        "anthropic_key":    bool(os.getenv("ANTHROPIC_API_KEY")),
        "finnhub_key":      bool(os.getenv("FINNHUB_KEY")),
        "quotes_provider":  os.getenv("QUOTES_PROVIDER") or "(unset)",
        "news_provider":    os.getenv("NEWS_PROVIDER") or "(unset)",
        "email_provider":   os.getenv("EMAIL_PROVIDER") or "off",
        "discord_notify":   bool(os.getenv("DISCORD_WEBHOOK_URL")),
        "db_present":       os.path.exists(DB_PATH),
        "server_uptime_seconds": int(time.time() - _STARTED),
    }

    # ---- self-diagnosis (safe to expose; no secrets) -----------------------
    out["debug"] = {
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "env_file": _ENV_FILE or "(none found)",
        "cwd": os.getcwd(),
    }
    # ---- live feed telemetry (Databento stream health) ---------------------
    try:
        from korvus_quotes import feed_status
        out["feed"] = feed_status()
    except Exception as e:
        out["feed"] = {"error": str(e)}

    # ---- news engine health (heartbeat file + scoring throughput) ----------
    eng = {}
    try:
        hb_path = os.path.join(os.path.dirname(DB_PATH), "engine_status.json")
        if os.path.exists(hb_path):
            with open(hb_path) as f:
                hb = json.load(f)
            eng.update(hb)
            try:
                ts = dt.datetime.fromisoformat(hb.get("ts", ""))
                now = dt.datetime.now(dt.timezone.utc)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                eng["heartbeat_age_sec"] = round((now - ts).total_seconds())
            except Exception:
                eng["heartbeat_age_sec"] = None
        else:
            eng["heartbeat_age_sec"] = None
    except Exception as e:
        eng["error"] = str(e)
    try:
        conn = _db()
        now = dt.datetime.now(dt.timezone.utc)
        h1 = (now - dt.timedelta(hours=1)).isoformat()
        h24 = (now - dt.timedelta(hours=24)).isoformat()
        eng["items_1h"] = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE created_at >= ?", (h1,)).fetchone()["c"]
        eng["items_24h"] = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE created_at >= ?", (h24,)).fetchone()["c"]
        eng["by_impact_24h"] = {r["impact"]: r["c"] for r in conn.execute(
            "SELECT impact, COUNT(*) c FROM items "
            "WHERE created_at >= ? AND noise = 0 AND impact IS NOT NULL "
            "GROUP BY impact", (h24,))}
        eng["noise_24h"] = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE created_at >= ? AND noise = 1",
            (h24,)).fetchone()["c"]
        last = conn.execute(
            "SELECT created_at FROM items ORDER BY created_at DESC LIMIT 1").fetchone()
        if last:
            try:
                ts = dt.datetime.fromisoformat(last["created_at"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                eng["last_item_age_sec"] = round((now - ts).total_seconds())
            except Exception:
                eng["last_item_age_sec"] = None
        conn.close()
    except Exception as e:
        eng.setdefault("error", str(e))
    out["engine"] = eng

    # owner's free-tier preview switch state (per browser session)
    try:
        out["preview_free"] = bool(session.get("korvus_free_preview"))
    except Exception:
        out["preview_free"] = False

    return jsonify(out)


@korvus_admin.route("/api/admin/find-user")
@admin_required
def admin_find_user():
    """Owner tool: look up accounts by username or email substring."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"ok": True, "users": []})
    conn = None
    try:
        conn = _db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        fields = ["username"]
        for c in ("email", "tier", "created_at", "tv_username"):
            if c in cols:
                fields.append(c)
        sel = ", ".join(fields)
        like = f"%{q}%"
        if "email" in cols:
            where, params = "username LIKE ? OR email LIKE ?", (like, like)
        else:
            where, params = "username LIKE ?", (like,)
        rows = conn.execute(
            f"SELECT {sel} FROM users WHERE {where} ORDER BY username LIMIT 12",
            params).fetchall()
        return jsonify({"ok": True, "users": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@korvus_admin.route("/api/admin/set-tier", methods=["POST"])
@admin_required
def admin_set_tier():
    """Owner tool: promote/demote a single account between free and pro. Only
    the tier column is touched; never deletes. Admin-gated and audited."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    tier = (data.get("tier") or "").strip().lower()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    if tier not in ("free", "pro"):
        return jsonify({"ok": False, "error": "tier must be free or pro"}), 400
    conn = None
    try:
        conn = _db_rw()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "tier" not in cols:
            return jsonify({"ok": False, "error": "users table has no tier column"}), 400
        row = conn.execute("SELECT username, tier FROM users WHERE username = ?",
                           (username,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": f"no user '{username}'"}), 404
        old = row["tier"]
        conn.execute("UPDATE users SET tier = ? WHERE username = ?", (tier, username))
        conn.commit()
        actor = getattr(current_user, "username", "?")
        print(f"  [admin] {actor} set tier for {username}: {old} -> {tier}")
        try:
            import korvus_auth as auth
            auth.log_member_event(username, "tier", f"{old} -> {tier} by {actor}")
        except Exception:
            pass
        return jsonify({"ok": True, "username": username, "old": old, "new": tier})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@korvus_admin.route("/api/admin/lodestone-granted", methods=["POST"])
@admin_required
def admin_lodestone_granted():
    """Owner ledger: record that LodeStone access was granted or revoked on
    TradingView for this member. Does not touch TradingView itself; the grant
    and revoke are manual there. Admin-gated and audited."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    granted = data.get("granted")
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    if not isinstance(granted, bool):
        return jsonify({"ok": False, "error": "granted must be true or false"}), 400
    conn = None
    try:
        conn = _db_rw()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "lodestone_granted" not in cols:
            return jsonify({"ok": False, "error": "users table has no lodestone_granted "
                            "column; restart the server so the migration runs"}), 400
        row = conn.execute("SELECT username, lodestone_granted FROM users WHERE username = ?",
                           (username,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": f"no user '{username}'"}), 404
        conn.execute("UPDATE users SET lodestone_granted = ? WHERE username = ?",
                     (1 if granted else 0, username))
        conn.commit()
        actor = getattr(current_user, "username", "?")
        word = "granted" if granted else "revoked"
        print(f"  [admin] {actor} marked LodeStone {word} for {username}")
        try:
            import korvus_auth as auth
            auth.log_member_event(username, "lodestone", f"marked {word} by {actor}")
        except Exception:
            pass
        return jsonify({"ok": True, "username": username, "granted": granted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _sv(obj, key, default=None):
    """Version-proof Stripe field read. Stripe's Python library has shifted
    between dict-like objects and typed attribute objects across major
    versions, so this tries attribute access first, then dict-style get, then
    item access, and never raises. Returns default for missing/None values."""
    try:
        v = getattr(obj, key)
        if v is not None:
            return v
    except Exception:
        pass
    try:
        v = obj.get(key)
        if v is not None:
            return v
    except Exception:
        pass
    try:
        v = obj[key]
        if v is not None:
            return v
    except Exception:
        pass
    return default


@korvus_admin.route("/api/admin/stripe-status")
@admin_required
def admin_stripe_status():
    """Owner tool: live Stripe subscription status for one member, fetched from
    Stripe's API on demand (not from cached webhook state), plus a deep link to
    the customer in the Stripe dashboard. Read-only against Stripe."""
    username = (request.args.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    try:
        import korvus_auth as auth
        import korvus_billing as billing
        user = auth.get_user_by_username(username)
        if not user:
            return jsonify({"ok": False, "error": f"no user '{username}'"}), 404
        cid = (user.get("stripe_customer_id") or "").strip()
        if not cid:
            return jsonify({"ok": True, "linked": False, "username": username})
        st = billing._stripe()
        if st is None:
            return jsonify({"ok": False, "error": "Stripe is not configured on this server"}), 500
        test_mode = billing.STRIPE_SECRET_KEY.startswith("sk_test")
        dash = ("https://dashboard.stripe.com/test/customers/" if test_mode
                else "https://dashboard.stripe.com/customers/") + cid
        invoices = []
        try:
            _inv = st.Invoice.list(customer=cid, limit=5)
            for iv in (_sv(_inv, "data", []) or []):
                _crt = _sv(iv, "created")
                _amt = _sv(iv, "amount_paid") or _sv(iv, "amount_due") or 0
                invoices.append({
                    "created": (dt.datetime.fromtimestamp(_crt, dt.timezone.utc).isoformat()
                                if _crt else None),
                    "amount": _amt / 100.0,
                    "status": _sv(iv, "status")})
        except Exception:
            pass
        _subresp = st.Subscription.list(customer=cid, status="all", limit=5)
        subs = list(_sv(_subresp, "data", []) or [])
        if not subs:
            return jsonify({"ok": True, "linked": True, "username": username,
                            "customer_id": cid, "dashboard_url": dash,
                            "invoices": invoices, "subscription": None})
        # prefer a live subscription; otherwise the most recently created one
        rank = {"active": 0, "trialing": 1, "past_due": 2, "unpaid": 3}
        subs.sort(key=lambda x: (rank.get(_sv(x, "status"), 9),
                                 -(_sv(x, "created", 0) or 0)))
        sub = subs[0]
        amount, currency, interval = None, None, None
        try:
            _idata = _sv(_sv(sub, "items"), "data", []) or []
            price = _sv(_idata[0], "price") if _idata else None
            if price is not None:
                amount = (_sv(price, "unit_amount") or 0) / 100.0
                currency = str(_sv(price, "currency") or "usd").upper()
                interval = str(_sv(_sv(price, "recurring") or {}, "interval") or "")
        except Exception:
            pass
        cpe = _sv(sub, "current_period_end")
        return jsonify({"ok": True, "linked": True, "username": username,
                        "customer_id": cid, "dashboard_url": dash,
                        "invoices": invoices,
                        "subscription": {
                            "status": _sv(sub, "status"),
                            "cancel_at_period_end": bool(_sv(sub, "cancel_at_period_end")),
                            "current_period_end": (
                                dt.datetime.fromtimestamp(cpe, dt.timezone.utc).isoformat()
                                if cpe else None),
                            "amount": amount, "currency": currency,
                            "interval": interval}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@korvus_admin.route("/api/admin/send-reset", methods=["POST"])
@admin_required
def admin_send_reset():
    """Owner tool: fire the existing password-reset email flow for a member who
    is locked out. Reuses the exact token + SES path the public /forgot form
    uses; nothing new touches passwords here."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    try:
        import korvus_auth as auth
        user = auth.get_user_by_username(username)
        if not user:
            return jsonify({"ok": False, "error": f"no user '{username}'"}), 404
        email = (user.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": f"'{username}' has no email on file"}), 400
        token, em = auth.create_reset_token(email)
        if not token:
            return jsonify({"ok": False, "error": "could not issue a reset token"}), 500
        auth.send_reset_email(em, token)
        actor = getattr(current_user, "username", "?")
        print(f"  [admin] {actor} sent password reset to {username} <{em}>")
        return jsonify({"ok": True, "username": username, "email": em})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@korvus_admin.route("/api/admin/member")
@admin_required
def admin_member():
    """Owner tool: one member's full story for the drawer. Profile fields from
    the users table plus the recorded timeline (member_events). The timeline
    accumulates from the day this shipped; older history simply is not there,
    and the UI says so rather than inventing it."""
    username = (request.args.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    try:
        import korvus_auth as auth
        user = auth.get_user_by_username(username)
        if not user:
            return jsonify({"ok": False, "error": f"no user '{username}'"}), 404
        profile = {
            "username": user.get("username"),
            "email": user.get("email"),
            "tier": user.get("tier"),
            "created_at": user.get("created_at"),
            "last_login": user.get("last_login"),
            "email_verified": bool(user.get("email_verified")),
            "stripe_linked": bool((user.get("stripe_customer_id") or "").strip()),
            "subscription_status": user.get("subscription_status"),
            "current_period_end": user.get("current_period_end"),
        }
        events = []
        conn = None
        try:
            conn = _db()
            events = [dict(r) for r in conn.execute(
                "SELECT ts, kind, detail FROM member_events "
                "WHERE username = ? ORDER BY ts DESC LIMIT 30", (username,))]
        except Exception:
            events = []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return jsonify({"ok": True, "profile": profile, "events": events})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@korvus_admin.route("/api/admin/preview", methods=["POST"])
@admin_required
def admin_preview():
    """Owner tool: flip the free-tier preview switch for this browser session.
    While on, the server treats this session as free everywhere tiers matter,
    so the terminal renders the genuine free experience. Nothing is stored on
    the account; closing the session or toggling off restores normal service."""
    data = request.get_json(silent=True) or {}
    on = bool(data.get("on"))
    session["korvus_free_preview"] = on
    actor = getattr(current_user, "username", "?")
    print(f"  [admin] {actor} free-tier preview {'ON' if on else 'OFF'}")
    return jsonify({"ok": True, "preview_free": on})

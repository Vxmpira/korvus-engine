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
import time
import sqlite3
import datetime as dt
from functools import wraps
from flask import Blueprint, abort, send_from_directory, jsonify, request
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

        conn.close()
    except Exception as e:
        out["users_error"] = str(e)

    # ---- revenue (clearly an estimate) -------------------------------------
    try:
        price = float(os.getenv("PRO_PRICE_MONTHLY", "3.99"))
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
        for c in ("email", "tier", "created_at"):
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
        return jsonify({"ok": True, "username": username, "old": old, "new": tier})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

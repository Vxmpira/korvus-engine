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
from flask import Flask, jsonify, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus.db")

app = Flask(__name__)


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
def dashboard():
    # serves the dashboard html sitting next to this file
    return send_from_directory(HERE, "korvus_dashboard.html")


# ----------------------------------------------------------------------------
# PHASE 3 — live quotes for the panels
# ----------------------------------------------------------------------------
from flask import request

WATCHLIST_PATH = os.path.join(HERE, "watchlist.json")


@app.route("/api/quotes")
def api_quotes():
    """
    Live prices for a comma-separated ?symbols= list.
    Feeds the Futures Board, SMT panel, and Funds Watch.
    """
    try:
        from korvus_quotes import get_quotes
    except Exception as e:
        return jsonify({"error": f"quotes module not available: {e}", "quotes": {}})
    symbols = (request.args.get("symbols") or "").split(",")
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        return jsonify({"quotes": {}, "_meta": {}})
    data = get_quotes(symbols)
    meta = data.pop("_meta", {})
    return jsonify({"quotes": data, "meta": meta})


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    """
    GET  -> returns the saved personal watchlist (or {} if none imported yet,
            in which case the dashboard uses its built-in default groups).
    POST -> saves an imported watchlist (JSON: {"groups":[{group,rows:[{sym,name}]}]}).
            Accepts a simple imported list and stores it locally.
    """
    if request.method == "POST":
        try:
            payload = request.get_json(force=True)
            with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            return jsonify({"ok": True, "saved": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    # GET
    if os.path.exists(WATCHLIST_PATH):
        try:
            with open(WATCHLIST_PATH, encoding="utf-8") as f:
                wl = json.load(f)
            # a saved {"groups": null} means the user reset to default
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

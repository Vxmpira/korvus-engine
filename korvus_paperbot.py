#!/usr/bin/env python3
"""
==============================================================================
 KORVUS PAPER BOT  ·  filing-catalyst momentum, PAPER ONLY   (BlackCrownVxJ LLC)
==============================================================================
 Simulated trades on the Korvus Intel universe (stocks near $100). It never
 connects to a broker and never places a real order. Its whole job is to
 produce an honest, measurable win rate for one written-down rule set, so the
 strategy can be judged on evidence before any real money is considered.

 STRATEGY v1  "Catalyst Follow-Through"  (every number below is in .env)
   Trigger   a filing from a universe stock, scored by Korvus Intel as
             direction=bull, impact med or high, confidence >= 60, seen
             within the last 18 hours (so an after-hours earnings release is
             tradable at the next morning's session).
   Confirm   the stock is ABOVE its reference price (the last price when the
             filing was seen; for after-hours filings that is the close), and
             it is up less than 8% on the day (do not chase a blown-out gap).
   Window    entries only 9:45 AM to 3:30 PM ET on regular sessions.
   Size      $10,000 paper notional per trade, max 5 open, one trade per
             filing, one entry per symbol per day.
   Exit      +1.5% target, -1.0% stop, or flat at 3:55 PM ET, whichever first.
   Fills     last quote +/- 0.05% slippage. Quotes are polled every 60s, so a
             stop can fill worse than -1.0%; the ledger records what happened.

 HONESTY RULES
   - Paper results ignore market impact and use 60s-old prices; real fills
     would be worse. The scoreboard says so.
   - Win rate is not shown as meaningful until 30 closed trades.
   - The strategy version is stamped on every trade; changing the rules means
     a new version and a fresh scoreboard, never rewriting history.
==============================================================================
"""
import math
import datetime as dt

import korvus_intel as intel

_env, _num = intel._env, intel._num

STRATEGY      = "v1-catalyst-follow-through"
TICK_SECONDS  = max(30, int(_num("BOT_TICK_SECONDS", 60)))
NOTIONAL      = _num("BOT_NOTIONAL", 10_000)
MAX_OPEN      = int(_num("BOT_MAX_OPEN", 5))
TARGET_PCT    = _num("BOT_TARGET_PCT", 1.5)
STOP_PCT      = _num("BOT_STOP_PCT", 1.0)
MAX_DAY_UP    = _num("BOT_MAX_DAY_UP_PCT", 8.0)
MIN_CONF      = int(_num("BOT_MIN_CONFIDENCE", 60))
LOOKBACK_H    = _num("BOT_LOOKBACK_HOURS", 18)
SLIP          = _num("BOT_SLIPPAGE_PCT", 0.05) / 100.0
MIN_SAMPLE    = 30

ENTRY_START = (9, 45)
ENTRY_END   = (15, 30)
FLAT_AT     = (15, 55)


def _hm(t: dt.datetime) -> tuple:
    return (t.hour, t.minute)


def session_open(now_et: dt.datetime) -> bool:
    try:
        from korvus_quotes import market_is_open
        return bool(market_is_open())
    except Exception:
        return now_et.weekday() < 5 and (9, 30) <= _hm(now_et) < (16, 0)


# ----------------------------------------------------------------------------
# pure rule checks (unit-tested)
# ----------------------------------------------------------------------------
def is_trigger(f: dict) -> bool:
    return (f.get("direction") == "bull" and f.get("impact") in ("high", "med")
            and int(f.get("confidence") or 0) >= MIN_CONF)


def confirm_entry(f: dict, q: dict) -> tuple:
    """(ok, reason). Price must hold above the pre-reaction reference and the
    day's gain must not already be a chase."""
    ref = f.get("ref_price") or f.get("prev_close") or q.get("prev_close")
    if not ref:
        return False, "no reference price"
    if q["price"] <= ref:
        return False, f"below reference {ref:.2f}"
    if q.get("chg_pct", 0) >= MAX_DAY_UP:
        return False, f"already up {q['chg_pct']:.1f}% on the day"
    return True, "confirmed"


def exit_decision(t: dict, px: float, now_et: dt.datetime):
    """Return (reason, fill_px) or None."""
    if px >= t["target_px"]:
        return "target", px * (1 - SLIP)
    if px <= t["stop_px"]:
        return "stop", px * (1 - SLIP)
    if _hm(now_et) >= FLAT_AT:
        return "time", px * (1 - SLIP)
    try:   # missed the 3:55 flat (service was down): close at the first chance
        if dt.datetime.fromisoformat(t["entry_utc"]).astimezone(now_et.tzinfo).date() < now_et.date():
            return "overnight", px * (1 - SLIP)
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# one tick
# ----------------------------------------------------------------------------
def _close(conn, t: dict, reason: str, fill: float, now_utc: str):
    pnl = (fill - t["entry_px"]) * t["qty"]
    pct = (fill / t["entry_px"] - 1) * 100
    conn.execute("""UPDATE trades SET exit_utc=?, exit_px=?, exit_reason=?, pnl=?, pnl_pct=?, status='closed'
                    WHERE id=?""", (now_utc, round(fill, 4), reason,
                                    round(pnl, 2), round(pct, 3), t["id"]))
    conn.commit()
    print(f"  [paperbot] EXIT {t['symbol']} {reason} @ {fill:.2f}  {pct:+.2f}%  ${pnl:+.2f}")


def tick(now_et: dt.datetime = None, quote_fn=None) -> dict:
    """Manage exits, then look for entries. quote_fn is injectable for tests."""
    now_et = now_et or intel.et_now()
    quote_fn = quote_fn or intel.quote
    now_utc = now_et.astimezone(dt.timezone.utc)
    out = {"exits": 0, "entries": 0, "skipped": []}
    if not session_open(now_et):
        return out
    conn = intel.db()

    # ---- exits first ----
    open_trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='open'")]
    for t in open_trades:
        q = quote_fn(t["symbol"])
        if not q:
            continue
        d = exit_decision(t, q["price"], now_et)
        if d:
            _close(conn, t, *d, now_utc.isoformat())
            out["exits"] += 1

    # ---- entries ----
    if not (ENTRY_START <= _hm(now_et) < ENTRY_END):
        conn.close()
        return out
    n_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='open'").fetchone()["c"]
    since = (now_utc - dt.timedelta(hours=LOOKBACK_H)).isoformat()
    today_et = now_et.date().isoformat()
    cands = [dict(r) for r in conn.execute(
        "SELECT * FROM filings WHERE scored=1 AND seen_utc >= ? ORDER BY confidence DESC, seen_utc DESC",
        (since,))]
    for f in cands:
        if n_open >= MAX_OPEN:
            break
        if not is_trigger(f):
            continue
        if conn.execute("SELECT 1 FROM trades WHERE accession=?", (f["accession"],)).fetchone():
            continue
        same_day = conn.execute(
            "SELECT entry_utc FROM trades WHERE symbol=? ORDER BY id DESC LIMIT 1", (f["symbol"],)).fetchone()
        if same_day:
            try:
                from zoneinfo import ZoneInfo
                last_et = dt.datetime.fromisoformat(same_day["entry_utc"]).astimezone(ZoneInfo("America/New_York"))
                if last_et.date().isoformat() == today_et:
                    continue
            except Exception:
                pass
        q = quote_fn(f["symbol"])
        if not q:
            continue
        ok, why = confirm_entry(f, q)
        if not ok:
            out["skipped"].append((f["symbol"], why))
            continue
        fill = q["price"] * (1 + SLIP)
        qty = int(math.floor(NOTIONAL / fill))
        if qty < 1:
            continue
        conn.execute("""INSERT INTO trades (strategy,symbol,accession,qty,entry_utc,entry_px,target_px,stop_px,status)
                        VALUES (?,?,?,?,?,?,?,?, 'open')""",
                     (STRATEGY, f["symbol"], f["accession"], qty, now_utc.isoformat(), round(fill, 4),
                      round(fill * (1 + TARGET_PCT / 100), 4), round(fill * (1 - STOP_PCT / 100), 4)))
        conn.commit()
        n_open += 1
        out["entries"] += 1
        print(f"  [paperbot] ENTRY {f['symbol']} x{qty} @ {fill:.2f} on {f['catalyst']} "
              f"({f['impact']}/{f['confidence']}%)")
    conn.close()
    return out


# ----------------------------------------------------------------------------
# scoreboard
# ----------------------------------------------------------------------------
def scoreboard(strategy: str = STRATEGY) -> dict:
    conn = intel.db()
    closed = [dict(r) for r in conn.execute(
        "SELECT t.*, f.catalyst FROM trades t LEFT JOIN filings f ON f.accession=t.accession "
        "WHERE t.status='closed' AND t.strategy=? ORDER BY t.exit_utc", (strategy,))]
    n_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='open' AND strategy=?",
                          (strategy,)).fetchone()["c"]
    conn.close()
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    losses = [t for t in closed if (t["pnl"] or 0) <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    by_cat = {}
    for t in closed:
        c = t.get("catalyst") or "other"
        b = by_cat.setdefault(c, {"trades": 0, "wins": 0})
        b["trades"] += 1
        b["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
    exits = {}
    for t in closed:
        exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1
    n = len(closed)
    return {
        "strategy": strategy,
        "closed_trades": n,
        "open_trades": n_open,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / n, 1) if n else None,
        "sample_ok": n >= MIN_SAMPLE,
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 3) if wins else None,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 3) if losses else None,
        "expectancy_pct": round(sum(t["pnl_pct"] for t in closed) / n, 3) if n else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "paper_pnl": round(sum(t["pnl"] or 0 for t in closed), 2),
        "exits": exits,
        "by_catalyst": by_cat,
    }

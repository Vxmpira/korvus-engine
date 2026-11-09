#!/usr/bin/env python3
"""
==============================================================================
 KORVUS PAPER BOT  ·  filing-catalyst momentum, PAPER ONLY   (BlackCrownVxJ LLC)
==============================================================================
 Simulated trades on the Korvus Intel universe (stocks near $100). It never
 connects to a broker and never places a real order. Its whole job is to
 produce an honest, measurable win rate for one written-down rule set, so the
 strategy can be judged on evidence before any real money is considered.

 STRATEGY v2  "Catalyst Follow-Through, both ways"  (every number is in .env)
   Trigger   a filing from a universe stock scored impact med or high,
             confidence >= 60, seen within the last 18 hours (so an
             after-hours earnings release is tradable at the next session).
             direction=bull -> LONG.  direction=bear -> SHORT.  neut -> no trade.
   Confirm   LONG:  price ABOVE its reference (the last price when the filing
                    was seen; for after-hours filings that is the close) and up
                    less than 8% on the day (do not chase a blown-out gap).
             SHORT: price BELOW its reference and down less than 8% on the day
                    (do not press a collapsed gap; also stays clear of the
                    SEC Rule 201 short-sale restriction that starts at -10%).
   Window    entries only 9:45 AM to 3:30 PM ET on regular sessions.
   Size      $10,000 paper notional per trade, max 5 open, one trade per
             filing, one entry per symbol per day.
   Exit      1.5% in our favor (target), 1.0% against (stop), or flat at
             3:55 PM ET, whichever first. Same distances for both sides.
   Fills     last quote +/- 0.05% slippage, always against us (buy higher,
             sell lower). Quotes are polled every 60s, so a
             stop can fill worse than -1.0%; the ledger records what happened.

 HONESTY RULES
   - Paper results ignore market impact and use 60s-old prices; real fills
     would be worse. The scoreboard says so.
   - Paper shorts assume the shares can be borrowed and pay no borrow fee.
     Real shorting needs a margin account, an available borrow, and some
     names cost a daily fee to short. Live shorts would do somewhat worse.
   - Win rate is not shown as meaningful until 30 closed trades.
   - The strategy version is stamped on every trade; changing the rules means
     a new version and a fresh scoreboard, never rewriting history.
==============================================================================
"""
import math
import datetime as dt

import korvus_intel as intel

_env, _num = intel._env, intel._num

STRATEGY      = "v2-catalyst-both-ways"
ALLOW_SHORT   = _env("BOT_ALLOW_SHORT", "1") not in ("0", "false", "no", "off")
TICK_SECONDS  = max(30, int(_num("BOT_TICK_SECONDS", 60)))
NOTIONAL      = _num("BOT_NOTIONAL", 10_000)
MAX_OPEN      = int(_num("BOT_MAX_OPEN", 5))
TARGET_PCT    = _num("BOT_TARGET_PCT", 1.5)
STOP_PCT      = _num("BOT_STOP_PCT", 1.0)
MAX_DAY_UP    = _num("BOT_MAX_DAY_UP_PCT", 8.0)     # long: skip if already up this much
MAX_DAY_DOWN  = _num("BOT_MAX_DAY_DOWN_PCT", 8.0)   # short: skip if already down this much
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
def side_for(f: dict):
    """'long' for a bullish read, 'short' for a bearish one (when shorts are
    enabled), None when the read does not qualify."""
    if f.get("impact") not in ("high", "med") or int(f.get("confidence") or 0) < MIN_CONF:
        return None
    if f.get("direction") == "bull":
        return "long"
    if f.get("direction") == "bear" and ALLOW_SHORT:
        return "short"
    return None


def is_trigger(f: dict) -> bool:
    return side_for(f) is not None


def confirm_entry(f: dict, q: dict, side: str = "long") -> tuple:
    """(ok, reason). The move since the filing must agree with the read, and
    the day's move must not already be a chase in that direction."""
    ref = f.get("ref_price") or f.get("prev_close") or q.get("prev_close")
    if not ref:
        return False, "no reference price"
    chg = q.get("chg_pct", 0)
    if side == "short":
        if q["price"] >= ref:
            return False, f"short: not below reference {ref:.2f}"
        if chg <= -MAX_DAY_DOWN:
            return False, f"short: already down {abs(chg):.1f}% on the day"
        return True, "confirmed"
    if q["price"] <= ref:
        return False, f"below reference {ref:.2f}"
    if chg >= MAX_DAY_UP:
        return False, f"already up {chg:.1f}% on the day"
    return True, "confirmed"


def levels(side: str, fill: float) -> tuple:
    """(target_px, stop_px) for a fill, mirrored for shorts."""
    if side == "short":
        return fill * (1 - TARGET_PCT / 100), fill * (1 + STOP_PCT / 100)
    return fill * (1 + TARGET_PCT / 100), fill * (1 - STOP_PCT / 100)


def exit_decision(t: dict, px: float, now_et: dt.datetime):
    """Return (reason, fill_px) or None. A long exits by selling (fill below
    the quote), a short exits by buying back (fill above the quote)."""
    short = (t.get("side") or "long") == "short"
    fill = px * (1 + SLIP) if short else px * (1 - SLIP)
    hit_target = px <= t["target_px"] if short else px >= t["target_px"]
    hit_stop = px >= t["stop_px"] if short else px <= t["stop_px"]
    if hit_target:
        return "target", fill
    if hit_stop:
        return "stop", fill
    if _hm(now_et) >= FLAT_AT:
        return "time", fill
    try:   # missed the 3:55 flat (service was down): close at the first chance
        if dt.datetime.fromisoformat(t["entry_utc"]).astimezone(now_et.tzinfo).date() < now_et.date():
            return "overnight", fill
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# one tick
# ----------------------------------------------------------------------------
def _close(conn, t: dict, reason: str, fill: float, now_utc: str):
    sign = -1 if (t.get("side") or "long") == "short" else 1
    pnl = sign * (fill - t["entry_px"]) * t["qty"]
    pct = sign * (fill / t["entry_px"] - 1) * 100
    conn.execute("""UPDATE trades SET exit_utc=?, exit_px=?, exit_reason=?, pnl=?, pnl_pct=?, status='closed'
                    WHERE id=?""", (now_utc, round(fill, 4), reason,
                                    round(pnl, 2), round(pct, 3), t["id"]))
    conn.commit()
    verb = "COVER" if sign < 0 else "EXIT"
    msg = f"{verb} {t['symbol']} {reason} @ ${fill:.2f}, {pct:+.2f}% (${pnl:+.2f} paper)"
    print(f"  [paperbot] {msg}")
    return msg


def tick(now_et: dt.datetime = None, quote_fn=None) -> dict:
    """Manage exits, then look for entries. quote_fn is injectable for tests."""
    now_et = now_et or intel.et_now()
    quote_fn = quote_fn or intel.quote
    now_utc = now_et.astimezone(dt.timezone.utc)
    out = {"exits": 0, "entries": 0, "skipped": [], "log": [], "candidates": 0,
           "session_open": False, "in_window": False, "open_positions": 0}
    if not session_open(now_et):
        return out
    out["session_open"] = True
    out["in_window"] = ENTRY_START <= _hm(now_et) < ENTRY_END
    conn = intel.db()

    # ---- exits first ----
    open_trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='open'")]
    for t in open_trades:
        q = quote_fn(t["symbol"])
        if not q:
            continue
        d = exit_decision(t, q["price"], now_et)
        if d:
            out["log"].append(("exit", t["symbol"], _close(conn, t, *d, now_utc.isoformat())))
            out["exits"] += 1

    # ---- entries ----
    n_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='open'").fetchone()["c"]
    out["open_positions"] = n_open
    if not out["in_window"]:
        conn.close()
        return out
    since = (now_utc - dt.timedelta(hours=LOOKBACK_H)).isoformat()
    today_et = now_et.date().isoformat()
    cands = [dict(r) for r in conn.execute(
        "SELECT * FROM filings WHERE scored=1 AND seen_utc >= ? ORDER BY confidence DESC, seen_utc DESC",
        (since,))]
    for f in cands:
        if n_open >= MAX_OPEN:
            break
        side = side_for(f)
        if not side:
            continue
        out["candidates"] += 1
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
        ok, why = confirm_entry(f, q, side)
        if not ok:
            out["skipped"].append((f["symbol"], why))
            continue
        fill = q["price"] * (1 - SLIP) if side == "short" else q["price"] * (1 + SLIP)
        qty = int(math.floor(NOTIONAL / fill))
        if qty < 1:
            continue
        tgt, stp = levels(side, fill)
        conn.execute("""INSERT INTO trades (strategy,symbol,accession,side,qty,entry_utc,entry_px,target_px,stop_px,status)
                        VALUES (?,?,?,?,?,?,?,?,?, 'open')""",
                     (STRATEGY, f["symbol"], f["accession"], side, qty, now_utc.isoformat(), round(fill, 4),
                      round(tgt, 4), round(stp, 4)))
        conn.commit()
        n_open += 1
        out["entries"] += 1
        out["open_positions"] = n_open
        msg = (f"{'SHORT' if side == 'short' else 'ENTRY'} {f['symbol']} x{qty} @ ${fill:.2f} "
               f"on {f['catalyst'].replace('_', ' ')} "
               f"({f['impact']}/{f['confidence']}%)")
        out["log"].append(("entry", f["symbol"], msg))
        print(f"  [paperbot] {msg}")
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
    by_side = {}
    for t in closed:
        sd = t.get("side") or "long"
        b = by_side.setdefault(sd, {"trades": 0, "wins": 0, "pnl": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t["pnl"] or 0), 2)
    exits = {}
    for t in closed:
        exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1
    n = len(closed)
    # drawdown on the cumulative paper P&L curve, in dollars and as a % of the
    # capital the rules can have at work at once (notional x max open)
    capital = NOTIONAL * MAX_OPEN
    peak = cum = mdd = 0.0
    for t in closed:
        cum += t["pnl"] or 0
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    days = sorted({(t["exit_utc"] or "")[:10] for t in closed if t.get("exit_utc")})
    last30 = closed[-30:]
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
        "by_side": by_side,
        "capital_at_work": capital,
        "max_drawdown_usd": round(mdd, 2),
        "max_drawdown_pct": round(100 * mdd / capital, 2) if capital else None,
        "first_trade_utc": closed[0]["entry_utc"] if closed else None,
        "last_trade_utc": closed[-1]["exit_utc"] if closed else None,
        "trading_days": len(days),
        "last30_expectancy_pct": round(sum(t["pnl_pct"] for t in last30) / len(last30), 3) if last30 else None,
    }

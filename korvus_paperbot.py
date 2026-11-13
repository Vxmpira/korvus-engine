#!/usr/bin/env python3
"""
==============================================================================
 KORVUS PAPER BOT  ·  filing catalysts on company stock, PAPER ONLY
                                                          (BlackCrownVxJ LLC)
==============================================================================
 Simulated trades on the Korvus Intel universe (stocks near $100). It never
 connects to a broker and never places a real order. Its whole job is to
 produce an honest, measurable record for one written-down rule set, so the
 strategy can be judged on evidence before any real money is considered.

 STRATEGY v4  "Catalyst scalp and day swing"  (every number is in .env)

 Why v4 exists. The first 8 paper trades (1 under v2, 7 under v3, 1 winner)
 showed four execution faults, none of them about the reads themselves:
   - a 1.0% stop sat inside the normal noise of a $100 stock on news: six of
     seven losers were stopped inside 15 minutes
   - stops filled at 1.3% to 2.3%, not 1.0%, so the real payoff was upside
     down (make 1.5, lose 1.8)
   - five of eight entries were outside the regular session or in the first
     minutes of the open, on single thin prints
   - "price above the reference" by one cent counted as confirmation
 v4 fixes the execution and splits the book into two playbooks, each scored
 on its own.

 PLAYBOOK 1  CATALYST SCALP   (the only place this bot scalps)
   Trigger   a filing the crawler saw in the last 30 minutes, scored HIGH
             impact at 70%+ confidence, bullish (long) or bearish (short).
   Confirm   price has moved 0.3%+ past the reference price in the read's
             direction and holds there on 2 checks in a row.
   Window    regular session 9:40 AM to 3:40 PM ET. With BOT_EXTENDED on
             (default) also pre-market 7:00 to 9:25 AM and after-hours 4:05 to
             7:00 PM, on a print no older than 5 minutes, at half risk and
             with wider stops.
   Exit      target 1.5x the stop distance, the stop, or 45 minutes.

 PLAYBOOK 2  CATALYST DAY SWING   (the main book: in and out within the day)
   Trigger   a filing seen in the last 18 hours scored HIGH at 65%+ or MED
             at 72%+. An after-hours earnings release is a swing candidate
             at the next session.
   Confirm   price 0.5%+ past the reference in the read's direction, on the
             right side of today's open, holding on 3 checks in a row.
   Window    entries 10:00 AM to 2:30 PM ET only: after the opening range
             has formed, with time left for the move to work.
   Exit      target 2x the stop distance, the stop, or flat at 3:55 PM ET.
             Once the trade is up by one stop distance the stop moves to the
             entry price, so a working trade cannot become a loser.

 BOTH PLAYBOOKS
   Stop      sized to the stock's own range that day (never tighter than
             1.2% scalp / 1.5% swing), so normal noise does not stop it out.
   Size      risk based: every trade risks the same dollars ($100 by default)
             at its stop, capped at $10,000 notional. Max 5 open, one
             position per symbol, one trade per filing per playbook.
   Skips     routine catalysts (dividends, officer changes), filings with no
             reference price, and any stock already up or down 8% on the day.
   Fills     last quote with 0.05% slippage against us (0.15% outside the
             regular session). Quotes are polled every 60s, so a stop can
             fill worse than planned; the ledger records what happened.

 HONESTY RULES
   - Paper results ignore market impact and use 60s-old prices; real fills
     would be worse. The scoreboard says so.
   - Paper shorts assume the shares can be borrowed and pay no borrow fee.
   - Win rate is not shown as meaningful until 30 closed trades.
   - The strategy version is stamped on every trade; changing the rules means
     a new version and a fresh scoreboard, never rewriting history. The v3
     trades stay on the record under their own name.
==============================================================================
"""
import math
import datetime as dt

import korvus_intel as intel

_env, _num = intel._env, intel._num
_on = lambda key, default="1": _env(key, default).strip().lower() not in ("0", "false", "no", "off")

STRATEGY      = "v4-scalp-and-swing"
SESSION       = _env("BOT_SESSION", "all").lower()          # all | regular (regular = no extended-hours scalps)
EXTENDED      = SESSION != "regular" and _on("BOT_EXTENDED")
ALLOW_SHORT   = _on("BOT_ALLOW_SHORT")
ALLOW_SCALP   = _on("BOT_ALLOW_SCALP")
TICK_SECONDS  = max(30, int(_num("BOT_TICK_SECONDS", 60)))
RISK_USD      = _num("BOT_RISK_USD", 100)                    # dollars lost if the stop fills where planned
NOTIONAL      = _num("BOT_NOTIONAL", 10_000)                 # the most one position can be worth
MAX_OPEN      = int(_num("BOT_MAX_OPEN", 5))
MAX_DAY_MOVE  = _num("BOT_MAX_DAY_MOVE_PCT", 8.0)            # skip a stock already this far in our direction today
LOOKBACK_H    = _num("BOT_LOOKBACK_HOURS", 18)
SLIP          = _num("BOT_SLIPPAGE_PCT", 0.05) / 100.0
SLIP_EXT      = _num("BOT_SLIPPAGE_EXT_PCT", 0.15) / 100.0   # outside the regular session: wider spreads
MAX_QUOTE_AGE = _num("BOT_MAX_QUOTE_AGE_MIN", 15)            # exits: older than this is not a live market
ENTRY_QUOTE_AGE = _num("BOT_ENTRY_QUOTE_AGE_MIN", 5)         # entries need a print this recent
EXT_RISK_MULT = _num("BOT_EXT_RISK_MULT", 0.5)               # extended hours: half the risk
EXT_STOP_MULT = _num("BOT_EXT_STOP_MULT", 1.5)               # ... and stops this much wider
SKIP_CATALYSTS = tuple(x.strip() for x in _env("BOT_SKIP_CATALYSTS", "dividend,exec_change").split(",") if x.strip())
MIN_SAMPLE    = 30

PLAYBOOKS = {
    "scalp": {
        "label": "Catalyst scalp",
        "fresh_min": _num("BOT_SCALP_FRESH_MIN", 30),
        "conf_high": int(_num("BOT_SCALP_MIN_CONFIDENCE", 70)), "conf_med": None,
        "min_move": _num("BOT_SCALP_MIN_MOVE_PCT", 0.3),
        "checks": int(_num("BOT_SCALP_CONFIRM_CHECKS", 2)),
        "stop_floor": _num("BOT_SCALP_STOP_PCT", 1.2), "stop_range_k": 0.35, "stop_cap": 2.5,
        "rr": _num("BOT_SCALP_REWARD_RISK", 1.5),
        "max_hold_min": _num("BOT_SCALP_MAX_HOLD_MIN", 45),
    },
    "swing": {
        "label": "Catalyst day swing",
        "fresh_min": None,
        "conf_high": int(_num("BOT_SWING_MIN_CONFIDENCE", 65)), "conf_med": int(_num("BOT_SWING_MIN_CONFIDENCE_MED", 72)),
        "min_move": _num("BOT_SWING_MIN_MOVE_PCT", 0.5),
        "checks": int(_num("BOT_SWING_CONFIRM_CHECKS", 3)),
        "stop_floor": _num("BOT_SWING_STOP_PCT", 1.5), "stop_range_k": 0.5, "stop_cap": 3.5,
        "rr": _num("BOT_SWING_REWARD_RISK", 2.0),
        "max_hold_min": None,
    },
}

# windows, US Eastern
SCALP_REG   = ((9, 40), (15, 40))
SCALP_PRE   = ((7, 0), (9, 25))
SCALP_POST  = ((16, 5), (19, 0))
SWING_ENTRY = ((10, 0), (14, 30))
FLAT_AT     = (15, 55)       # every position opened before the close is flat here
EXT_FLAT_AT = (19, 50)       # after-hours scalps are flat here

PHASE_LABEL = {"regular": "regular session", "premarket": "pre-market", "afterhours": "after-hours",
               "weekend": "weekend", "closed": "market closed"}

# why the rules are what they are: printed in the vault and the admin, newest first
CHANGELOG = [
    {"date": "2026-11-13", "version": STRATEGY, "title": "The first 8 paper trades (1 under v2, 7 under v3) showed four execution faults. v4 fixes them.",
     "found": [
         "A 1.0% stop sat inside the normal noise of a $100 stock on news: six of the seven losers were stopped inside 15 minutes.",
         "Stops filled 1.3% to 2.3% away, not 1.0%, so the payoff was upside down: make 1.5%, lose 1.8%.",
         "Five of the eight entries were outside the regular session or in the first minutes of the open, on single thin prints.",
         "Price one cent above the reference counted as confirmation, and a dividend raise and a legal re-domicile were traded as catalysts.",
     ],
     "changed": [
         "Two playbooks scored separately: a scalp on a filing the crawler just read, and a swing held inside the day.",
         "Stops are sized to the stock's own range that day, and every trade risks the same dollars.",
         "Price has to move a real margin the read's way and hold there for two or three checks in a row.",
         "Swings enter 10:00 AM to 2:30 PM ET only, move the stop to the entry once they work, and are flat by 3:55 PM.",
         "Weak reads and routine catalysts are skipped. No trading overnight.",
     ]},
]

# how many checks in a row each candidate has passed (resets on a miss or a restart)
_streak = {}
LAST_WATCH = []          # what the bot is looking at right now, for the admin and the vault


def _t(hm) -> str:
    h, m = hm
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def window_text() -> str:
    """The trading windows in words, for the vault and the admin."""
    ext = (f", plus pre-market {_t(SCALP_PRE[0])} to {_t(SCALP_PRE[1])} and after-hours {_t(SCALP_POST[0])} to "
           f"{_t(SCALP_POST[1])} on a print no older than {ENTRY_QUOTE_AGE:.0f} minutes") if EXTENDED else ""
    scalp = f"scalps {_t(SCALP_REG[0])} to {_t(SCALP_REG[1])} ET{ext}; " if ALLOW_SCALP else ""
    return f"{scalp}day swings enter {_t(SWING_ENTRY[0])} to {_t(SWING_ENTRY[1])} ET"


def flat_text() -> str:
    return f"{_t(FLAT_AT)} ET" + (f" ({_t(EXT_FLAT_AT)} for after-hours scalps)" if EXTENDED and ALLOW_SCALP else "")


def rules_lines() -> list:
    """The rule set in plain English. The vault note and the admin both print this."""
    s, w = PLAYBOOKS["scalp"], PLAYBOOKS["swing"]
    out = []
    if ALLOW_SCALP:
        out.append(("Catalyst scalp",
                    f"A filing seen in the last {s['fresh_min']:.0f} minutes, scored high impact at {s['conf_high']}%+ confidence. "
                    f"Price must be {s['min_move']}%+ past the reference price in the read's direction on {s['checks']} checks in a row. "
                    f"Stop sized to the day's range (at least {s['stop_floor']}%), target {s['rr']}x the stop, out after "
                    f"{s['max_hold_min']:.0f} minutes at most."))
    out.append(("Catalyst day swing",
                f"A filing seen in the last {LOOKBACK_H:.0f} hours, scored high at {w['conf_high']}%+ or medium at {w['conf_med']}%+. "
                f"Price must be {w['min_move']}%+ past the reference in the read's direction, on the right side of today's open, "
                f"on {w['checks']} checks in a row. Stop sized to the day's range (at least {w['stop_floor']}%), target {w['rr']}x the stop. "
                f"Once up one stop distance the stop moves to the entry price. Flat by {_t(FLAT_AT)} ET, never held overnight."))
    out.append(("Windows", window_text() + "."))
    out.append(("Size", f"Every trade risks ${RISK_USD:,.0f} at its stop"
                + (f" (${RISK_USD * EXT_RISK_MULT:,.0f} outside the regular session)" if EXTENDED else "")
                + f", capped at ${NOTIONAL:,.0f} a position. At most {MAX_OPEN} open, one position per symbol, one trade per filing per playbook."))
    out.append(("Skips", f"Routine catalysts ({', '.join(c.replace('_', ' ') for c in SKIP_CATALYSTS) or 'none'}), filings with no reference "
                f"price, bearish reads when shorts are off, and any stock already {MAX_DAY_MOVE:.0f}% our way on the day."))
    out.append(("Fills", f"Last quote with {SLIP * 100:.2f}% slippage against us ({SLIP_EXT * 100:.2f}% outside the regular session), "
                f"quotes polled every {TICK_SECONDS}s. Real fills would be worse. Paper shorts assume a free borrow."))
    return out


def _hm(t: dt.datetime) -> tuple:
    return (t.hour, t.minute)


def _between(hm, win) -> bool:
    return win[0] <= hm < win[1]


def session_phase(now_et: dt.datetime) -> str:
    """Where the stock market is: premarket, regular, afterhours, closed, weekend."""
    wd, hm = now_et.weekday(), _hm(now_et)
    if wd >= 5:
        return "weekend"
    if (4, 0) <= hm < (9, 30):
        return "premarket"
    if (9, 30) <= hm < (16, 0):
        return "regular"
    if (16, 0) <= hm < (20, 0):
        return "afterhours"
    return "closed"


def scalp_window(now_et: dt.datetime, phase: str) -> bool:
    hm = _hm(now_et)
    if not ALLOW_SCALP:
        return False
    if phase == "regular":
        return _between(hm, SCALP_REG)
    if not EXTENDED:
        return False
    return (phase == "premarket" and _between(hm, SCALP_PRE)) or (phase == "afterhours" and _between(hm, SCALP_POST))


def swing_window(now_et: dt.datetime, phase: str) -> bool:
    return phase == "regular" and _between(_hm(now_et), SWING_ENTRY)


def can_enter(now_et: dt.datetime, phase: str) -> bool:
    return scalp_window(now_et, phase) or swing_window(now_et, phase)


def session_id(t: dt.datetime) -> str:
    """The trading day a moment belongs to: the calendar date in New York."""
    return t.date().isoformat()


def quote_fresh(q: dict, now_utc: dt.datetime, phase: str, max_age_min: float = None) -> bool:
    """Is this quote a live market right now? Outside the regular session a
    quote with no trade time, or an old one, is yesterday's price: never fill on it."""
    ts = q.get("ts")
    if not ts:
        return phase == "regular"
    return (now_utc.timestamp() - float(ts)) <= (MAX_QUOTE_AGE if max_age_min is None else max_age_min) * 60


# ----------------------------------------------------------------------------
# pure rule checks (unit-tested)
# ----------------------------------------------------------------------------
def side_for(f: dict):
    """'long' for a bullish read, 'short' for a bearish one (when shorts are
    enabled), None when the read has no direction worth trading."""
    if f.get("impact") not in ("high", "med"):
        return None
    if f.get("direction") == "bull":
        return "long"
    if f.get("direction") == "bear" and ALLOW_SHORT:
        return "short"
    return None


def grade(f: dict, pb: str) -> bool:
    """Does the read meet this playbook's bar?"""
    p, conf = PLAYBOOKS[pb], int(f.get("confidence") or 0)
    if f.get("impact") == "high":
        return conf >= p["conf_high"]
    if f.get("impact") == "med":
        return p["conf_med"] is not None and conf >= p["conf_med"]
    return False


def is_trigger(f: dict) -> bool:
    return side_for(f) is not None and (grade(f, "scalp") or grade(f, "swing"))


def playbook_for(f: dict, now_et: dt.datetime, phase: str) -> tuple:
    """(playbook or None, why). A fresh high-grade filing is a scalp while the
    scalp window is open; otherwise a filing that makes the swing grade waits
    for, or trades in, the day swing window."""
    if (f.get("catalyst") or "") in SKIP_CATALYSTS:
        return None, f"routine catalyst ({str(f.get('catalyst')).replace('_', ' ')}), not traded"
    if side_for(f) is None:
        return None, "no tradable direction"
    try:
        age = (now_et - dt.datetime.fromisoformat(f["seen_utc"])).total_seconds() / 60.0
    except Exception:
        age = 1e9
    if grade(f, "scalp") and 0 <= age <= PLAYBOOKS["scalp"]["fresh_min"] and scalp_window(now_et, phase):
        return "scalp", "fresh high-impact filing"
    if grade(f, "swing"):
        if swing_window(now_et, phase):
            return "swing", "inside the day swing window"
        return None, f"waiting for the day swing window ({_t(SWING_ENTRY[0])} to {_t(SWING_ENTRY[1])} ET)"
    return None, (f"read too weak for a day swing ({f.get('impact')} at {int(f.get('confidence') or 0)}%)"
                  + (", and no longer fresh enough to scalp" if grade(f, "scalp") else ""))


def move_since(f: dict, q: dict, side: str):
    """Percent move since the filing, signed so that positive agrees with the read."""
    ref = f.get("ref_price")
    if not ref:
        return None
    way = -1 if side == "short" else 1
    return way * (q["price"] / float(ref) - 1) * 100


def confirm_entry(f: dict, q: dict, side: str = "long", pb: str = "swing") -> tuple:
    """(ok, reason). The market has to agree with the read by a real margin,
    the day must not already be a chase, and for a swing the stock must be on
    the right side of its own open."""
    p = PLAYBOOKS[pb]
    mv = move_since(f, q, side)
    if mv is None:
        return False, "no reference price from the time of the filing"
    if mv < p["min_move"]:
        return False, f"{mv:+.2f}% since the filing, needs +{p['min_move']}% the read's way"
    way = -1 if side == "short" else 1
    chg = float(q.get("chg_pct") or 0) * way
    if chg >= MAX_DAY_MOVE:
        return False, f"already {'down' if side == 'short' else 'up'} {abs(chg):.1f}% on the day"
    if pb == "swing" and q.get("open"):
        if (q["price"] - float(q["open"])) * way <= 0:
            return False, f"on the wrong side of today's open ({float(q['open']):.2f})"
    return True, f"{mv:+.2f}% since the filing"


def day_range_pct(q: dict) -> float:
    hi, lo, px = float(q.get("high") or 0), float(q.get("low") or 0), float(q.get("price") or 0)
    return (hi - lo) / px * 100 if hi > lo > 0 and px > 0 else 0.0


def plan(pb: str, side: str, fill: float, q: dict, phase: str) -> dict:
    """Stop, target and size for a fill. The stop is the larger of the floor
    and a share of the stock's range today, the target a fixed multiple of it,
    and the size whatever makes a stop-out cost the same dollars every time."""
    p, ext = PLAYBOOKS[pb], phase != "regular"
    widen = EXT_STOP_MULT if ext else 1.0
    stop_pct = min(p["stop_cap"] * widen, max(p["stop_floor"] * widen, p["stop_range_k"] * day_range_pct(q)))
    target_pct = stop_pct * p["rr"]
    way = -1 if side == "short" else 1
    risk = RISK_USD * (EXT_RISK_MULT if ext else 1.0)
    qty = int(min(math.floor(risk / (fill * stop_pct / 100)), math.floor(NOTIONAL / fill)))
    return {"stop_pct": round(stop_pct, 3), "target_pct": round(target_pct, 3),
            "stop_px": fill * (1 - way * stop_pct / 100), "target_px": fill * (1 + way * target_pct / 100),
            "qty": qty, "risk_usd": round(qty * fill * stop_pct / 100, 2)}


def levels(side: str, fill: float, pb: str = "swing") -> tuple:
    """(target_px, stop_px) at the floor stop, mirrored for shorts."""
    pl = plan(pb, side, fill, {"price": fill}, "regular")
    return pl["target_px"], pl["stop_px"]


def exit_decision(t: dict, px: float, now_et: dt.datetime, fresh: bool = True, slip: float = None, phase: str = "regular"):
    """Return (reason, fill_px) or None. A long exits by selling (fill below
    the quote), a short exits by buying back (fill above it). Target and stop
    need a fresh quote; the time exits do not (they close at the last price)."""
    slip = SLIP if slip is None else slip
    short = (t.get("side") or "long") == "short"
    fill = px * (1 + slip) if short else px * (1 - slip)
    hit_target = px <= t["target_px"] if short else px >= t["target_px"]
    hit_stop = px >= t["stop_px"] if short else px <= t["stop_px"]
    if fresh and hit_target:
        return "target", fill
    if fresh and hit_stop:
        moved = t.get("init_stop_px") and abs(t["stop_px"] - t["init_stop_px"]) > 1e-9
        return ("breakeven stop" if moved else "stop"), fill
    pb = t.get("playbook") or "swing"
    try:
        ent = dt.datetime.fromisoformat(t["entry_utc"]).astimezone(now_et.tzinfo)
    except Exception:
        ent = None
    hold = PLAYBOOKS.get(pb, {}).get("max_hold_min")
    if ent and hold and (now_et - ent).total_seconds() >= hold * 60:
        return "time limit", fill
    if ent and session_id(ent) < session_id(now_et):      # missed the flat (service was down): close at the first chance
        return "overnight", fill
    hm = _hm(now_et)
    if (t.get("phase") or "regular") == "afterhours":
        if phase != "afterhours" or hm >= EXT_FLAT_AT:
            return "end of day", fill
    elif phase in ("afterhours", "closed", "weekend") or (phase == "regular" and hm >= FLAT_AT):
        return "end of day", fill
    return None


# ----------------------------------------------------------------------------
# one tick
# ----------------------------------------------------------------------------
def _close(conn, t: dict, reason: str, fill: float, now_utc: dt.datetime):
    sign = -1 if (t.get("side") or "long") == "short" else 1
    pnl = sign * (fill - t["entry_px"]) * t["qty"]
    pct = sign * (fill / t["entry_px"] - 1) * 100
    r_mult = round(pnl / t["risk_usd"], 2) if t.get("risk_usd") else None
    try:
        hold = round((now_utc - dt.datetime.fromisoformat(t["entry_utc"])).total_seconds() / 60.0, 1)
    except Exception:
        hold = None
    conn.execute("""UPDATE trades SET exit_utc=?, exit_px=?, exit_reason=?, pnl=?, pnl_pct=?, r_mult=?, hold_min=?, status='closed'
                    WHERE id=?""", (now_utc.isoformat(), round(fill, 4), reason, round(pnl, 2), round(pct, 3), r_mult, hold, t["id"]))
    conn.commit()
    verb = "COVER" if sign < 0 else "EXIT"
    msg = (f"{verb} {t['symbol']} {reason} @ ${fill:.2f}, {pct:+.2f}% (${pnl:+.2f} paper"
           + (f", {r_mult:+.2f}R" if r_mult is not None else "") + (f", held {hold:.0f} min" if hold is not None else "") + ")")
    print(f"  [paperbot] {msg}")
    return msg


def _age_min(f: dict, now_utc: dt.datetime):
    try:
        return round((now_utc - dt.datetime.fromisoformat(f["seen_utc"])).total_seconds() / 60.0)
    except Exception:
        return None


def tick(now_et: dt.datetime = None, quote_fn=None) -> dict:
    """Manage exits, then look for entries. quote_fn is injectable for tests."""
    global LAST_WATCH
    now_et = now_et or intel.et_now()
    quote_fn = quote_fn or intel.quote
    now_utc = now_et.astimezone(dt.timezone.utc)
    out = {"exits": 0, "entries": 0, "skipped": [], "log": [], "candidates": 0, "watch": [], "positions": [],
           "session_open": False, "in_window": False, "open_positions": 0}
    phase = session_phase(now_et)
    out["phase"] = phase
    conn = intel.db()
    n_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='open'").fetchone()["c"]
    out["open_positions"] = n_open
    out["session_open"] = phase not in ("closed", "weekend")
    out["in_window"] = can_enter(now_et, phase)
    slip = SLIP if phase == "regular" else SLIP_EXT

    # ---- exits first ----
    for t in [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='open'")]:
        q = quote_fn(t["symbol"])
        if not q:
            continue
        px, way = q["price"], (-1 if (t.get("side") or "long") == "short" else 1)
        fresh = quote_fresh(q, now_utc, phase)
        if fresh:
            run = way * (px / t["entry_px"] - 1) * 100
            mfe, mae = max(t.get("mfe_pct") or 0.0, run), min(t.get("mae_pct") or 0.0, run)
            conn.execute("UPDATE trades SET mfe_pct=?, mae_pct=? WHERE id=?", (round(mfe, 3), round(mae, 3), t["id"]))
            # a swing that is up one stop distance can no longer lose: stop to the entry price
            init = t.get("init_stop_px")
            if (t.get("playbook") == "swing" and init and abs(t["stop_px"] - init) < 1e-9
                    and way * (px - t["entry_px"]) >= abs(t["entry_px"] - init)
                    and way * (px - t["target_px"]) < 0):
                conn.execute("UPDATE trades SET stop_px=? WHERE id=?", (t["entry_px"], t["id"]))
                t["stop_px"] = t["entry_px"]
                msg = f"{t['symbol']} is up one stop distance: stop moved to the entry price ${t['entry_px']:.2f}"
                out["log"].append(("manage", t["symbol"], msg))
                print(f"  [paperbot] {msg}")
            conn.commit()
        d = exit_decision(t, px, now_et, fresh, slip, phase)
        if d:
            out["log"].append(("exit", t["symbol"], _close(conn, t, d[0], d[1], now_utc)))
            out["exits"] += 1
        else:                                             # still open: where it stands right now, for the admin
            out["positions"].append({"id": t["id"], "symbol": t["symbol"], "price": px, "fresh": fresh,
                                     "run_pct": round(way * (px / t["entry_px"] - 1) * 100, 2),
                                     "pnl": round(way * (px - t["entry_px"]) * t["qty"], 2), "stop_px": t["stop_px"]})

    # ---- entries ----
    n_open = conn.execute("SELECT COUNT(*) c FROM trades WHERE status='open'").fetchone()["c"]
    out["open_positions"] = n_open
    since = (now_utc - dt.timedelta(hours=LOOKBACK_H)).isoformat()
    today_et = session_id(now_et)
    cands = [dict(r) for r in conn.execute(
        "SELECT * FROM filings WHERE scored=1 AND seen_utc >= ? ORDER BY confidence DESC, seen_utc DESC", (since,))]
    live_keys = set()
    for f in cands:
        side = side_for(f)
        if side is None:
            continue
        out["candidates"] += 1 if is_trigger(f) else 0
        w = {"symbol": f["symbol"], "accession": f["accession"], "side": side, "impact": f.get("impact"),
             "confidence": f.get("confidence"), "catalyst": f.get("catalyst"), "headline": (f.get("headline") or "")[:140],
             "age_min": _age_min(f, now_utc), "ref": f.get("ref_price"), "playbook": None, "state": "waiting", "why": ""}
        out["watch"].append(w)

        def skip(why, state="skipped"):
            w["state"], w["why"] = state, why
            out["skipped"].append((f["symbol"], why))

        pb, why = playbook_for(f, now_et, phase)
        if not pb:
            w["state"], w["why"] = ("waiting" if why.startswith("waiting") else "skipped"), why
            continue
        w["playbook"] = pb
        key = (f["accession"], pb)
        done = conn.execute("SELECT status FROM trades WHERE accession=? AND (playbook=? OR playbook IS NULL) ORDER BY id DESC LIMIT 1",
                            (f["accession"], pb)).fetchone()
        if done:
            w["state"], w["why"] = "traded", f"already traded as a {pb}"
            continue
        if conn.execute("SELECT 1 FROM trades WHERE symbol=? AND status='open'", (f["symbol"],)).fetchone():
            skip("a position in this symbol is already open")
            continue
        last = conn.execute("SELECT entry_utc FROM trades WHERE symbol=? AND playbook=? ORDER BY id DESC LIMIT 1",
                            (f["symbol"], pb)).fetchone()
        if last:
            try:
                if session_id(dt.datetime.fromisoformat(last["entry_utc"]).astimezone(now_et.tzinfo)) == today_et:
                    w["state"], w["why"] = "traded", f"one {pb} per symbol per day, already taken"
                    continue
            except Exception:
                pass
        if n_open >= MAX_OPEN:
            skip(f"{MAX_OPEN} positions already open")
            continue
        q = quote_fn(f["symbol"])
        if not q:
            skip("no quote")
            continue
        w["price"] = q["price"]
        mv = move_since(f, q, side)
        w["move_pct"] = round(mv, 2) if mv is not None else None
        w["need_pct"] = PLAYBOOKS[pb]["min_move"]
        if not quote_fresh(q, now_utc, phase, ENTRY_QUOTE_AGE):
            _streak.pop(key, None)
            skip(f"no trade printed in the last {ENTRY_QUOTE_AGE:.0f} minutes ({PHASE_LABEL[phase]})")
            continue
        ok, why = confirm_entry(f, q, side, pb)
        if not ok:
            _streak.pop(key, None)
            skip(why)
            continue
        live_keys.add(key)
        _streak[key] = _streak.get(key, 0) + 1
        need = PLAYBOOKS[pb]["checks"]
        w["checks"], w["need_checks"] = _streak[key], need
        if _streak[key] < need:
            skip(f"confirming: {why}, held on {_streak[key]} of {need} checks", "confirming")
            continue
        fill = q["price"] * (1 - slip) if side == "short" else q["price"] * (1 + slip)
        pl = plan(pb, side, fill, q, phase)
        if pl["qty"] < 1:
            skip("position would be smaller than one share")
            continue
        note = (f"{PLAYBOOKS[pb]['label']}: {why}, held on {need} checks. Stop {pl['stop_pct']:.2f}% "
                f"(day range {day_range_pct(q):.1f}%), target {pl['target_pct']:.2f}%, risk ${pl['risk_usd']:.0f}.")
        conn.execute("""INSERT INTO trades (strategy,symbol,accession,side,qty,entry_utc,entry_px,target_px,stop_px,status,
                        playbook,phase,risk_usd,init_stop_px,stop_pct,mfe_pct,mae_pct,note)
                        VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,?,0,0,?)""",
                     (STRATEGY, f["symbol"], f["accession"], side, pl["qty"], now_utc.isoformat(), round(fill, 4),
                      round(pl["target_px"], 4), round(pl["stop_px"], 4), pb, phase, pl["risk_usd"],
                      round(pl["stop_px"], 4), pl["stop_pct"], note))
        conn.commit()
        _streak.pop(key, None)
        n_open += 1
        out["entries"] += 1
        out["open_positions"] = n_open
        w["state"], w["why"] = "traded", f"entered as a {pb}"
        msg = (f"{'SHORT' if side == 'short' else 'LONG'} {f['symbol']} x{pl['qty']} @ ${fill:.2f} ({PLAYBOOKS[pb]['label'].lower()}) "
               f"on {str(f.get('catalyst') or 'filing').replace('_', ' ')} ({f['impact']}/{f['confidence']}%), "
               f"stop ${pl['stop_px']:.2f}, target ${pl['target_px']:.2f}, risk ${pl['risk_usd']:.0f}"
               + ("" if phase == "regular" else f", in the {PHASE_LABEL[phase]}"))
        out["log"].append(("entry", f["symbol"], msg))
        print(f"  [paperbot] {msg}")
    for k in [k for k in _streak if k not in live_keys]:
        _streak.pop(k, None)
    conn.close()
    LAST_WATCH = out["watch"][:25]
    return out


# ----------------------------------------------------------------------------
# scoreboard
# ----------------------------------------------------------------------------
def _avg(xs, nd=2):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), nd) if xs else None


def _bucket(closed, key):
    out = {}
    for t in closed:
        b = out.setdefault(key(t), {"trades": 0, "wins": 0, "pnl": 0.0, "_r": [], "_h": []})
        b["trades"] += 1
        b["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t["pnl"] or 0), 2)
        b["_r"].append(t.get("r_mult"))
        b["_h"].append(t.get("hold_min"))
    for b in out.values():
        b["avg_r"], b["avg_hold_min"] = _avg(b.pop("_r")), _avg(b.pop("_h"), 0)
    return out


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
    by_cat = {k: {"trades": v["trades"], "wins": v["wins"], "pnl": v["pnl"]}
              for k, v in _bucket(closed, lambda t: t.get("catalyst") or "other").items()}
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
    # how much worse than planned the stops filled (0 means exactly at the stop)
    stops = [t for t in closed if t["exit_reason"] == "stop" and t.get("stop_pct")]
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
        "expectancy_r": _avg([t.get("r_mult") for t in closed]),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "paper_pnl": round(sum(t["pnl"] or 0 for t in closed), 2),
        "exits": exits,
        "by_catalyst": by_cat,
        "by_side": _bucket(closed, lambda t: t.get("side") or "long"),
        "by_playbook": _bucket(closed, lambda t: t.get("playbook") or "legacy"),
        "by_phase": _bucket(closed, lambda t: t.get("phase") or "regular"),
        "avg_hold_min": _avg([t.get("hold_min") for t in closed], 0),
        "avg_mfe_pct": _avg([t.get("mfe_pct") for t in closed]),
        "avg_mae_pct": _avg([t.get("mae_pct") for t in closed]),
        "stop_slip_pct": _avg([abs(t["pnl_pct"]) - t["stop_pct"] for t in stops]),
        "risk_per_trade": RISK_USD,
        "capital_at_work": capital,
        "max_drawdown_usd": round(mdd, 2),
        "max_drawdown_pct": round(100 * mdd / capital, 2) if capital else None,
        "first_trade_utc": closed[0]["entry_utc"] if closed else None,
        "last_trade_utc": closed[-1]["exit_utc"] if closed else None,
        "trading_days": len(days),
        "last30_expectancy_pct": round(sum(t["pnl_pct"] for t in last30) / len(last30), 3) if last30 else None,
    }

#!/usr/bin/env python3
"""
==============================================================================
 KORVUS LAB  ·  the bot finds its own futures strategy, PAPER ONLY
                                                          (BlackCrownVxJ LLC)
==============================================================================
 The filing bot (korvus_paperbot.py) tests ONE written rule set on stocks.
 This lab does the opposite on index futures: for each market it runs a small
 population of different rule sets side by side on paper, keeps score, retires
 the ones that lose, breeds new ones from the ones that win, and writes down
 every strategy it ever created and why. It never connects to a broker and
 never places a real order.

 MARKETS   MNQ (Micro Nasdaq-100) and MES (Micro S&P 500). MES trades at the
           same price as ES; it is one tenth the size.

 WHAT A STRATEGY IS   four families, each with its own dials:
   news_follow   a scored story lands for this market; if price starts moving
                 the story's way, go with it
   news_fade     a scored story lands; if price overreacts, trade against it
   breakout      price breaks the high or low of the last N minutes
   pullback      price stretches far from its recent average; trade the return
 Every strategy also carries a target, a stop (both in multiples of the
 market's typical 15 minute move, so they scale with volatility), a maximum
 holding time, and the session it is allowed to trade (all, Asia, London, NY).

 HOW IT LEARNS   once a day, in the 5 PM ET maintenance hour:
   - a strategy with enough closed trades and a negative result is retired
   - a strategy that has not fired in days is retired
   - each empty seat is filled by a mutation of a profitable strategy (one or
     two dials moved), or a fresh random one when nothing is profitable yet
   - the best strategy with a real sample becomes the CHAMPION for that market
 Every one of those steps is written to the log in plain English.

 HONESTY RULES
   - Paper only. Fills are the last price plus one tick of slippage against us
     on each side, and a round trip commission is charged on every trade.
     Real fills would be somewhat worse.
   - Running many strategies at once means the best one looks good partly by
     luck. So a champion's record is split: what it did BEFORE it was crowned
     (how it was picked) and what it did AFTER (the only number that counts).
   - Nothing is ever deleted. Retired strategies and their trades stay on the
     book.
   - The lab only trades on the real CME feed. On the ETF proxy feed it stays
     idle and says so.
==============================================================================
"""
import os
import json
import math
import random
import sqlite3
import datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus_lab.db")
NEWS_DB = os.path.join(HERE, "korvus.db")
ET = ZoneInfo("America/New_York")


def _num(key, default):
    try:
        v = (os.getenv(key, "") or "").split(" #")[0].strip()
        return float(v) if v else float(default)
    except ValueError:
        return float(default)


ENABLED       = (os.getenv("LAB_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
POPULATION    = int(_num("LAB_POPULATION", 12))        # strategies running per market
MIN_EVAL      = int(_num("LAB_MIN_EVAL_TRADES", 12))   # closed trades before a strategy can be judged
MIN_CHAMPION  = int(_num("LAB_MIN_CHAMPION_TRADES", 20))
IDLE_DAYS     = _num("LAB_IDLE_DAYS", 3)               # no trade in this long: retired as never firing
COMMISSION_RT = _num("LAB_COMMISSION_RT", 1.40)        # dollars per micro contract, round trip
SLIP_TICKS    = _num("LAB_SLIPPAGE_TICKS", 1)          # per side, always against us
COOLDOWN_MIN  = _num("LAB_COOLDOWN_MIN", 5)

MARKETS = {
    "MNQ": {"name": "Micro Nasdaq-100", "mult": 2.0, "tick": 0.25, "news": ("MNQ", "NQ")},
    "MES": {"name": "Micro S&P 500 (ES price)", "mult": 5.0, "tick": 0.25, "news": ("MES", "ES")},
}
REAL_FEEDS = ("databento", "tradovate")
SESSIONS = ("all", "asia", "london", "ny")
SESSION_LABEL = {"all": "around the clock", "asia": "the Asia session only (6 PM to 3 AM ET)",
                 "london": "the London session only (3 AM to 9:30 AM ET)", "ny": "the New York session only (9:30 AM to 5 PM ET)"}

# the dials each family can turn
GRID = {
    "target": (0.8, 1.0, 1.5, 2.0, 3.0),
    "stop": (0.5, 0.75, 1.0, 1.5, 2.0),
    "hold": (15, 30, 60, 120, 240),
    "session": SESSIONS,
    "impact": ("high", "med"),
    "conf": (50, 60, 70, 80),
    "age": (3, 5, 10, 20),
    "confirm": (0.1, 0.25, 0.5, 0.75),
    "overshoot": (0.5, 0.75, 1.0, 1.5),
    "lookback": (15, 30, 60, 120),
    "stretch": (1.5, 2.0, 2.5, 3.0),
}
FAMILY_DIALS = {
    "news_follow": ("impact", "conf", "age", "confirm"),
    "news_fade": ("impact", "conf", "age", "overshoot"),
    "breakout": ("lookback",),
    "pullback": ("lookback", "stretch"),
}
COMMON_DIALS = ("target", "stop", "hold", "session")
DIAL_NAME = {"target": "target", "stop": "stop", "hold": "maximum hold", "session": "session", "impact": "minimum impact",
             "conf": "minimum confidence", "age": "story age limit", "confirm": "confirmation move",
             "overshoot": "overreaction size", "lookback": "lookback", "stretch": "stretch"}


# ----------------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bars (sym TEXT, ts INTEGER, px REAL, PRIMARY KEY (sym, ts));
        CREATE TABLE IF NOT EXISTS strategies (
            id TEXT PRIMARY KEY, sym TEXT, gen INTEGER, parent TEXT, family TEXT, params TEXT,
            status TEXT, created_utc TEXT, retired_utc TEXT, promoted_utc TEXT, why TEXT, retired_why TEXT, state TEXT);
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, strat TEXT, sym TEXT, side TEXT, entry_utc TEXT, entry_px REAL,
            target_px REAL, stop_px REAL, deadline_utc TEXT, exit_utc TEXT, exit_px REAL, reason TEXT, pnl REAL,
            status TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT, sym TEXT, kind TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    conn.commit()
    conn.close()


def meta_get(conn, k, default=""):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(conn, k, v):
    conn.execute("INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def note(conn, sym, kind, text, now_utc):
    conn.execute("INSERT INTO log (t,sym,kind,text) VALUES (?,?,?,?)", (now_utc.isoformat(), sym, kind, text))
    print(f"  [lab] {sym} {kind}: {text}")


# ----------------------------------------------------------------------------
# sessions
# ----------------------------------------------------------------------------
def phase(now_et):
    """open | maintenance | weekend. CME index futures: Sunday 6 PM ET to Friday
    5 PM ET, with a daily break from 5 to 6 PM ET."""
    wd, hm = now_et.weekday(), (now_et.hour, now_et.minute)
    if wd == 5 or (wd == 4 and hm >= (17, 0)) or (wd == 6 and hm < (18, 0)):
        return "weekend"
    if (17, 0) <= hm < (18, 0):
        return "maintenance"
    return "open"


def can_enter(now_et):
    return phase(now_et) == "open" and not ((16, 30) <= (now_et.hour, now_et.minute) < (18, 0))


def must_flat(now_et):
    return (16, 55) <= (now_et.hour, now_et.minute) < (18, 0)


def session_of(now_et):
    hm = (now_et.hour, now_et.minute)
    if hm >= (18, 0) or hm < (3, 0):
        return "asia"
    if hm < (9, 30):
        return "london"
    return "ny"


# ----------------------------------------------------------------------------
# strategies: making them, describing them, changing them
# ----------------------------------------------------------------------------
def random_params(rng, family=None):
    family = family or rng.choice(list(FAMILY_DIALS))
    p = {d: rng.choice(GRID[d]) for d in COMMON_DIALS + FAMILY_DIALS[family]}
    if p["target"] < p["stop"] * 0.6:          # never risk far more than the trade can make
        p["target"] = p["stop"]
    return family, p


def mutate(rng, family, params):
    """Move one or two dials one notch. Returns (new params, plain description)."""
    p = dict(params)
    dials = list(COMMON_DIALS + FAMILY_DIALS[family])
    changed = []
    for d in rng.sample(dials, k=rng.choice((1, 1, 2))):
        opts = list(GRID[d])
        i = opts.index(p[d]) if p[d] in opts else 0
        j = min(len(opts) - 1, max(0, i + rng.choice((-1, 1))))
        if j == i:
            j = i + 1 if i == 0 else i - 1
        changed.append(f"{DIAL_NAME[d]} {p[d]} to {opts[j]}")
        p[d] = opts[j]
    return p, ", ".join(changed)


def describe(family, p):
    """The rule set in plain English. This is what the owner reads."""
    grade = "high impact" if p.get("impact") == "high" else "medium impact or higher"
    if family == "news_follow":
        head = (f"Go with the news. When a story scored {grade} at {p['conf']}%+ confidence "
                f"lands for this market, and price moves {p['confirm']}x a typical 15 minute move in the story's "
                f"direction within {p['age']} minutes, enter that way.")
    elif family == "news_fade":
        head = (f"Fade the overreaction. When a story scored {grade} at {p['conf']}%+ confidence "
                f"lands and price runs {p['overshoot']}x a typical 15 minute move in the story's direction within "
                f"{p['age']} minutes, enter against it.")
    elif family == "breakout":
        head = (f"Trade the break. When price trades above the high of the last {p['lookback']} minutes go long; "
                f"below the low, go short.")
    else:
        head = (f"Trade the snap back. When price stretches {p['stretch']} standard deviations away from its "
                f"{p['lookback']} minute average, enter back toward the average.")
    return (f"{head} Target {p['target']}x and stop {p['stop']}x a typical 15 minute move, out after {p['hold']} "
            f"minutes at most. Trades {SESSION_LABEL[p['session']]}.")


def _next_id(conn, sym):
    n = int(meta_get(conn, f"seq_{sym}", "0")) + 1
    meta_set(conn, f"seq_{sym}", n)
    return f"{sym}-{n:03d}", n


def create_strategy(conn, sym, family, params, gen, parent, why, now_utc):
    sid, _ = _next_id(conn, sym)
    conn.execute("""INSERT INTO strategies (id,sym,gen,parent,family,params,status,created_utc,why,state)
                    VALUES (?,?,?,?,?,?, 'active', ?,?, '{}')""",
                 (sid, sym, gen, parent, family, json.dumps(params), now_utc.isoformat(), why))
    note(conn, sym, "created", f"{sid} created. {why} Rules: {describe(family, params)}", now_utc)
    return sid


def seed(conn, sym, now_utc):
    """The first generation: every family represented, dials spread out."""
    rng = random.Random(f"{sym}-seed")
    fams = list(FAMILY_DIALS)
    for i in range(POPULATION):
        family, p = random_params(rng, fams[i % len(fams)])
        create_strategy(conn, sym, family, p, 1, None,
                        "First generation, a starting guess so the lab has something to measure.", now_utc)


# ----------------------------------------------------------------------------
# market data helpers
# ----------------------------------------------------------------------------
def save_bar(conn, sym, now_utc, px):
    ts = int(now_utc.timestamp() // 60 * 60)
    conn.execute("INSERT INTO bars (sym,ts,px) VALUES (?,?,?) ON CONFLICT(sym,ts) DO UPDATE SET px=excluded.px", (sym, ts, px))
    conn.execute("DELETE FROM bars WHERE sym=? AND ts < ?", (sym, ts - 4 * 86400))


def recent_bars(conn, sym, now_utc, minutes):
    since = int(now_utc.timestamp()) - minutes * 60
    return [(r["ts"], r["px"]) for r in conn.execute("SELECT ts,px FROM bars WHERE sym=? AND ts>=? ORDER BY ts", (sym, since))]


def vol_unit(bars, tick):
    """The market's typical 15 minute move, from the last two hours of 1 minute
    changes. Targets and stops are multiples of this, so they widen when the
    market is fast and tighten when it is quiet. None until there is enough tape."""
    if len(bars) < 45:
        return None
    diffs = [b[1] - a[1] for a, b in zip(bars, bars[1:]) if b[0] - a[0] <= 180]
    if len(diffs) < 30:
        return None
    mean = sum(diffs) / len(diffs)
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / len(diffs))
    return max(sd * math.sqrt(15), tick * 8)


def tape_is_live(bars):
    """A market that has printed the same price for 10 straight minutes is not
    trading (holiday, halt, dead feed)."""
    last = [b[1] for b in bars[-10:]]
    return len(last) < 10 or len(set(last)) > 1


def fetch_news(since_utc):
    """Scored stories from the terminal's own database, read-only."""
    if not os.path.exists(NEWS_DB):
        return []
    try:
        c = sqlite3.connect(f"file:{NEWS_DB}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT id, created_at, headline, impact, direction, instruments, confidence FROM items "
            "WHERE created_at >= ? AND impact IN ('high','med') AND direction IN ('bull','bear') ORDER BY created_at",
            (since_utc.isoformat(),))]
        c.close()
        return rows
    except Exception as e:
        print(f"  [lab] news read skipped: {e}")
        return []


def _news_for(sym, items):
    want = MARKETS[sym]["news"]
    out = []
    for it in items:
        try:
            inst = json.loads(it.get("instruments") or "[]")
        except Exception:
            inst = []
        if any(str(x).upper() in want for x in inst):
            out.append(it)
    return out


def _parse_utc(s):
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# ----------------------------------------------------------------------------
# signals
# ----------------------------------------------------------------------------
def signal(family, p, st, px, bars, unit, news, now_utc):
    """Returns (side, note) or (None, None). st is the strategy's own memory."""
    if family in ("news_follow", "news_fade"):
        rank = {"med": 1, "high": 2}
        for it in reversed(news):
            if rank.get(it["impact"], 0) < rank[p["impact"]] or (it.get("confidence") or 0) < p["conf"]:
                continue
            if it["id"] in st.get("used", []):
                continue
            t0 = _parse_utc(it["created_at"])
            age = (now_utc - t0).total_seconds() / 60.0
            if age < 0 or age > p["age"]:
                continue
            ref = next((b[1] for b in bars if b[0] >= int(t0.timestamp() // 60 * 60)), None)
            if ref is None:
                continue
            way = 1 if it["direction"] == "bull" else -1
            move = (px - ref) * way
            need = (p["confirm"] if family == "news_follow" else p["overshoot"]) * unit
            if move >= need:
                st.setdefault("used", []).append(it["id"])
                st["used"] = st["used"][-40:]
                side = way if family == "news_follow" else -way
                return ("long" if side > 0 else "short",
                        f"{it['impact']} {it['direction']} story: {str(it['headline'])[:90]}")
        return None, None
    look = [b[1] for b in bars if b[0] >= bars[-1][0] - p["lookback"] * 60][:-1]
    if len(look) < max(10, p["lookback"] // 3):
        return None, None
    if family == "breakout":
        if px > max(look):
            return "long", f"broke the {p['lookback']} minute high"
        if px < min(look):
            return "short", f"broke the {p['lookback']} minute low"
        return None, None
    mean = sum(look) / len(look)
    sd = math.sqrt(sum((x - mean) ** 2 for x in look) / len(look))
    if sd <= 0:
        return None, None
    z = (px - mean) / sd
    if z <= -p["stretch"]:
        return "long", f"{abs(z):.1f} deviations below its {p['lookback']} minute average"
    if z >= p["stretch"]:
        return "short", f"{z:.1f} deviations above its {p['lookback']} minute average"
    return None, None


# ----------------------------------------------------------------------------
# the tick: manage exits, look for entries, review once a day
# ----------------------------------------------------------------------------
def _close(conn, t, px, reason, now_utc, m):
    slip = m["tick"] * SLIP_TICKS
    fill = px - slip if t["side"] == "long" else px + slip
    way = 1 if t["side"] == "long" else -1
    pnl = round((fill - t["entry_px"]) * way * m["mult"] - COMMISSION_RT, 2)
    conn.execute("UPDATE trades SET exit_utc=?, exit_px=?, reason=?, pnl=?, status='closed' WHERE id=?",
                 (now_utc.isoformat(), round(fill, 2), reason, pnl, t["id"]))
    return pnl


def tick(now_utc=None, quotes_fn=None, news_fn=None):
    """One pass. quotes_fn and news_fn are injectable for tests."""
    out = {"enabled": ENABLED, "entries": 0, "exits": 0, "phase": "", "feed_ok": False}
    if not ENABLED:
        return out
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    now_et = now_utc.astimezone(ET)
    ph = phase(now_et)
    out["phase"] = ph
    db_init()
    conn = db()
    try:
        for sym in MARKETS:
            if not conn.execute("SELECT 1 FROM strategies WHERE sym=? LIMIT 1", (sym,)).fetchone():
                seed(conn, sym, now_utc)
        if ph == "maintenance" and meta_get(conn, "reviewed_day") != now_et.strftime("%Y-%m-%d"):
            for sym in MARKETS:
                review(conn, sym, now_utc)
            meta_set(conn, "reviewed_day", now_et.strftime("%Y-%m-%d"))
        conn.commit()
        if ph != "open":
            meta_set(conn, "last_tick", json.dumps({"t": now_utc.isoformat(), "phase": ph, "feed_ok": None, "feed": ""}))
            conn.commit()
            return out

        if quotes_fn is None:
            from korvus_quotes import get_quotes
            quotes_fn = get_quotes
        q = quotes_fn(list(MARKETS)) or {}
        provider = (q.get("_meta") or {}).get("provider", "")
        feed_ok = provider in REAL_FEEDS
        out["feed_ok"] = feed_ok
        meta_set(conn, "last_tick", json.dumps({"t": now_utc.isoformat(), "phase": ph, "feed_ok": feed_ok, "feed": provider}))
        if not feed_ok:
            conn.commit()
            return out
        news_all = (news_fn or fetch_news)(now_utc - dt.timedelta(minutes=30))

        for sym, m in MARKETS.items():
            px = (q.get(sym) or {}).get("price")
            if not px:
                continue
            px = float(px)
            save_bar(conn, sym, now_utc, px)
            bars = recent_bars(conn, sym, now_utc, 150)
            unit = vol_unit(bars, m["tick"])
            live = tape_is_live(bars)
            news = _news_for(sym, news_all)
            sess = session_of(now_et)
            for s in [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE sym=? AND status IN ('active','champion')", (sym,))]:
                p, st = json.loads(s["params"]), json.loads(s["state"] or "{}")
                t = conn.execute("SELECT * FROM trades WHERE strat=? AND status='open'", (s["id"],)).fetchone()
                if t:
                    t = dict(t)
                    way = 1 if t["side"] == "long" else -1
                    reason = None
                    if live and (px - t["target_px"]) * way >= 0:
                        reason = "target"
                    elif live and (t["stop_px"] - px) * way >= 0:
                        reason = "stop"
                    elif must_flat(now_et):
                        reason = "flat before maintenance"
                    elif now_utc >= _parse_utc(t["deadline_utc"]):
                        reason = "time limit"
                    if reason:
                        pnl = _close(conn, t, px, reason, now_utc, m)
                        st["last_exit"] = now_utc.isoformat()
                        conn.execute("UPDATE strategies SET state=? WHERE id=?", (json.dumps(st), s["id"]))
                        note(conn, sym, "exit", f"{s['id']} closed {t['side']} at {px:.2f} on {reason}: {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}", now_utc)
                        out["exits"] += 1
                    continue
                if unit is None or not live or not can_enter(now_et):
                    continue
                if p["session"] != "all" and p["session"] != sess:
                    continue
                if st.get("last_exit") and (now_utc - _parse_utc(st["last_exit"])).total_seconds() < COOLDOWN_MIN * 60:
                    continue
                side, why = signal(s["family"], p, st, px, bars, unit, news, now_utc)
                conn.execute("UPDATE strategies SET state=? WHERE id=?", (json.dumps(st), s["id"]))
                if not side:
                    continue
                way = 1 if side == "long" else -1
                fill = px + way * m["tick"] * SLIP_TICKS
                tgt, stp = fill + way * p["target"] * unit, fill - way * p["stop"] * unit
                dead = now_utc + dt.timedelta(minutes=p["hold"])
                conn.execute("""INSERT INTO trades (strat,sym,side,entry_utc,entry_px,target_px,stop_px,deadline_utc,status,note)
                                VALUES (?,?,?,?,?,?,?,?, 'open', ?)""",
                             (s["id"], sym, side, now_utc.isoformat(), round(fill, 2), round(tgt, 2), round(stp, 2), dead.isoformat(), why))
                note(conn, sym, "entry", f"{s['id']} {side} 1 {sym} at {fill:.2f}, {why}. Target {tgt:.2f}, stop {stp:.2f}", now_utc)
                out["entries"] += 1
        conn.commit()
    finally:
        conn.close()
    return out


# ----------------------------------------------------------------------------
# scoring and the daily review
# ----------------------------------------------------------------------------
def stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": None, "pnl": 0.0, "expectancy": None, "profit_factor": None}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] <= 0]
    pnl = sum(t["pnl"] for t in trades)
    return {"trades": n, "wins": len(wins), "win_rate": round(len(wins) / n * 100, 1), "pnl": round(pnl, 2),
            "expectancy": round(pnl / n, 2),
            "profit_factor": round(sum(wins) / sum(losses), 2) if sum(losses) > 0 else None}


def _closed(conn, sid):
    return [dict(r) for r in conn.execute("SELECT * FROM trades WHERE strat=? AND status='closed' ORDER BY exit_utc", (sid,))]


def review(conn, sym, now_utc):
    """The daily learning step. Retire, breed, and crown; write down why."""
    rng = random.Random(f"{sym}-{now_utc.date().isoformat()}")
    live = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE sym=? AND status IN ('active','champion')", (sym,))]
    scored = [(s, stats(_closed(conn, s["id"]))) for s in live]
    retire = []
    for s, st in scored:
        age_d = (now_utc - _parse_utc(s["created_utc"])).total_seconds() / 86400
        if st["trades"] >= MIN_EVAL and st["expectancy"] < 0:
            retire.append((s, f"Retired after {st['trades']} trades: it lost ${abs(st['expectancy']):.2f} a trade on average "
                              f"(${st['pnl']:.2f} in total, {st['win_rate']}% winners)."))
        elif st["trades"] == 0 and age_d >= IDLE_DAYS:
            retire.append((s, f"Retired: no trade in {age_d:.0f} days, its conditions never came up."))
    # at most a third of the bench turns over in one review, worst first
    retire.sort(key=lambda r: (stats(_closed(conn, r[0]["id"]))["expectancy"] or 0))
    retire = retire[: max(1, POPULATION // 3)]
    for s, why in retire:
        conn.execute("UPDATE strategies SET status='retired', retired_utc=?, retired_why=? WHERE id=?",
                     (now_utc.isoformat(), why, s["id"]))
        t = conn.execute("SELECT * FROM trades WHERE strat=? AND status='open'", (s["id"],)).fetchone()
        if t:
            conn.execute("UPDATE trades SET status='closed', exit_utc=?, exit_px=entry_px, reason='strategy retired', pnl=? WHERE id=?",
                         (now_utc.isoformat(), -COMMISSION_RT, t["id"]))
        note(conn, sym, "retired", f"{s['id']} retired. {why}", now_utc)
    gone = {s["id"] for s, _ in retire}
    keep = [(s, st) for s, st in scored if s["id"] not in gone]
    winners = sorted([(s, st) for s, st in keep if st["trades"] >= max(4, MIN_EVAL // 2) and st["expectancy"] > 0],
                     key=lambda x: -x[1]["expectancy"])
    existing = {(s["family"], s["params"]) for s, _ in keep}
    for _ in range(POPULATION - len(keep)):
        made = False
        if winners and rng.random() < 0.75:
            par, pst = winners[rng.randrange(min(3, len(winners)))]
            for _try in range(6):
                p, what = mutate(rng, par["family"], json.loads(par["params"]))
                if (par["family"], json.dumps(p)) not in existing:
                    create_strategy(conn, sym, par["family"], p, par["gen"] + 1, par["id"],
                                    f"Bred from {par['id']}, which is making ${pst['expectancy']:.2f} a trade over "
                                    f"{pst['trades']} trades. Changed: {what}.", now_utc)
                    existing.add((par["family"], json.dumps(p)))
                    made = True
                    break
        if not made:
            family, p = random_params(rng)
            create_strategy(conn, sym, family, p, 1, None,
                            "A fresh idea, because no strategy on the bench is profitable enough to breed from yet."
                            if not winners else "A fresh idea, to keep the search from narrowing too early.", now_utc)
            existing.add((family, json.dumps(p)))
    # the crown: best average result with a real sample and winners outweighing losers
    cands = [(s, st) for s, st in keep if st["trades"] >= MIN_CHAMPION and st["expectancy"] > 0 and (st["profit_factor"] or 9) >= 1.1]
    cur = next((s for s, _ in keep if s["status"] == "champion"), None)
    if cands:
        best, bst = max(cands, key=lambda x: x[1]["expectancy"])
        if not cur or cur["id"] != best["id"]:
            if cur:
                conn.execute("UPDATE strategies SET status='active', promoted_utc=NULL WHERE id=?", (cur["id"],))
            conn.execute("UPDATE strategies SET status='champion', promoted_utc=? WHERE id=?", (now_utc.isoformat(), best["id"]))
            note(conn, sym, "champion", f"{best['id']} is now the champion for {sym}: ${bst['expectancy']:.2f} a trade over "
                 f"{bst['trades']} trades, {bst['win_rate']}% winners. Its record from here on is the test that counts."
                 + (f" It replaces {cur['id']}." if cur else ""), now_utc)
    # a champion that stops working after it was crowned gives the crown back:
    # that after-the-crown record is the honest test, so it gets the last word
    cur = conn.execute("SELECT * FROM strategies WHERE sym=? AND status='champion'", (sym,)).fetchone()
    if cur and cur["promoted_utc"]:
        after = stats([t for t in _closed(conn, cur["id"]) if t["exit_utc"] > cur["promoted_utc"]])
        if after["trades"] >= MIN_EVAL and after["expectancy"] < 0:
            conn.execute("UPDATE strategies SET status='active', promoted_utc=NULL WHERE id=?", (cur["id"],))
            note(conn, sym, "champion", f"{cur['id']} lost the crown: since it was crowned it has lost ${abs(after['expectancy']):.2f} "
                 f"a trade over {after['trades']} trades. It looked good before the crown and did not hold up after. "
                 f"No champion for {sym} right now.", now_utc)
    note(conn, sym, "review", f"Daily review done: {len(retire)} retired, {POPULATION - len(keep)} created, "
         f"{len(winners)} profitable so far.", now_utc)


# ----------------------------------------------------------------------------
# the book: everything the admin panel shows
# ----------------------------------------------------------------------------
def book():
    if not os.path.exists(DB_PATH):
        return {"enabled": ENABLED, "started": False, "markets": [], "log": [], "trades": []}
    conn = db()
    try:
        last = json.loads(meta_get(conn, "last_tick", "{}") or "{}")
        markets = []
        for sym, m in MARKETS.items():
            rows = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE sym=? ORDER BY id", (sym,))]
            bar = conn.execute("SELECT ts,px FROM bars WHERE sym=? ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
            strategies, all_closed = [], []
            for s in rows:
                closed = _closed(conn, s["id"])
                all_closed += closed
                op = conn.execute("SELECT side,entry_px,target_px,stop_px,entry_utc,note FROM trades WHERE strat=? AND status='open'", (s["id"],)).fetchone()
                p = json.loads(s["params"])
                item = {"id": s["id"], "gen": s["gen"], "parent": s["parent"], "family": s["family"], "status": s["status"],
                        "created_utc": s["created_utc"], "retired_utc": s["retired_utc"], "why": s["why"],
                        "retired_why": s["retired_why"], "rules": describe(s["family"], p), "params": p,
                        "open": dict(op) if op else None, **stats(closed)}
                if s["status"] == "champion" and s["promoted_utc"]:
                    item["promoted_utc"] = s["promoted_utc"]
                    item["since_crowned"] = stats([t for t in closed if t["exit_utc"] > s["promoted_utc"]])
                strategies.append(item)
            order = {"champion": 0, "active": 1, "retired": 2}
            strategies.sort(key=lambda x: (order.get(x["status"], 3), -(x["expectancy"] if x["expectancy"] is not None else -1e9)))
            markets.append({"sym": sym, "name": m["name"], "price": bar["px"] if bar else None,
                            "price_utc": dt.datetime.fromtimestamp(bar["ts"], dt.timezone.utc).isoformat() if bar else None,
                            "champion": next((x["id"] for x in strategies if x["status"] == "champion"), None),
                            "created": len(rows), "running": sum(1 for x in strategies if x["status"] != "retired"),
                            "retired": sum(1 for x in strategies if x["status"] == "retired"),
                            "all": stats(all_closed), "strategies": strategies})
        log = [dict(r) for r in conn.execute("SELECT t,sym,kind,text FROM log ORDER BY id DESC LIMIT 80")]
        trades = [dict(r) for r in conn.execute(
            "SELECT strat,sym,side,entry_utc,entry_px,exit_utc,exit_px,reason,pnl,status,note FROM trades ORDER BY id DESC LIMIT 40")]
        return {"enabled": ENABLED, "started": True, "last_tick": last, "min_eval": MIN_EVAL, "min_champion": MIN_CHAMPION,
                "population": POPULATION, "costs": {"commission_rt": COMMISSION_RT, "slippage_ticks": SLIP_TICKS},
                "markets": markets, "log": log, "trades": trades}
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(book(), indent=1)[:4000])

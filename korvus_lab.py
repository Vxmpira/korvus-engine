#!/usr/bin/env python3
"""
==============================================================================
 KORVUS LAB  ·  a quantitative strategy lab for index futures, PAPER ONLY
                                                          (BlackCrownVxJ LLC)
==============================================================================
 The filing bot (korvus_paperbot.py) trades company stock on catalysts. This
 lab works index futures the way a quant desk does: every idea is a fixed,
 testable rule set; a new rule set is replayed on stored tape before it gets a
 seat; every trade risks about the same dollars; and a strategy is kept,
 retired or crowned on its statistics, never on a story. It never connects to
 a broker and never places a real order.

 MARKETS   MNQ (Micro Nasdaq-100) and MES (Micro S&P 500). MES trades at the
           same price as ES; it is one tenth the size.

 THE STYLE
   Owner's ruling, 2026-11-13: INTRADAY SWINGS ONLY, NO SCALPING. That stands.
   - targets and stops are multiples of the market's typical ONE HOUR move
   - the shortest maximum hold on the menu is 90 minutes
   - lookbacks run from one to eight hours, not minutes
   - a strategy takes at most 2 trades a day and rests 30 minutes after one
   - no new trade after 3:30 PM ET, always flat before the 5 PM ET maintenance hour
   Owner's ask, 2026-11-16: QUANTITATIVE. That added four things:
   - SIZED BY RISK. Each trade takes as many micro contracts (1 to 5) as keep
     the distance to its stop near a fixed dollar amount, so a wide-stop trade
     and a tight-stop trade lose about the same when they are wrong. Results
     are scored in R: profit divided by what the trade risked.
   - TESTED BEFORE SEATED. A new rule set and its rivals are replayed over the
     stored tape (up to two weeks of one minute bars) with the live rules,
     costs and sizing; the best gets the seat. The replay picks, so the replay
     proves nothing: only the live paper record counts.
   - JUDGED BY STATISTICS. A strategy is scored by its average R and its
     t-score (average result against its own noise). The crown needs a
     t-score of 2 or better; a statistically losing strategy is retired early.
   - AWARE OF THE TAPE. A strategy can be limited to calm or fast conditions,
     measured as the last hour's volatility against the last eight hours'.

 WHAT A STRATEGY IS   seven families, each with its own dials:
   momentum      price has moved further over the last one to eight hours than
                 its normal noise explains; go with it
   rel_value     Nasdaq against S&P: fit how this market normally moves against
                 its sister index; when it sits far from that fit, trade it
                 back and get out when the gap closes
   pullback      price stretches far from its multi-hour average; trade the
                 return
   breakout      price clears the high or low of the last one to eight hours
   open_range    price clears the range set in the first 15 to 60 minutes of
                 the Asia, London or New York session
   news_follow   a scored Korvus story lands for this market; if price starts
                 moving the story's way, go with it
   news_fade     a scored story lands; if price overreacts, trade against it
 Every strategy also carries a target, a stop, a maximum holding time, the
 session it may trade, the tape condition it needs, and whether it protects a
 working trade by moving the stop to the entry price.

 HOW IT LEARNS   once a day, in the 5 PM ET maintenance hour:
   - a strategy with enough closed trades and a negative average R is retired;
     one that is statistically losing (t-score of -2 or worse) goes sooner
   - a strategy that has not fired in days is retired
   - each empty seat is filled by the best of several candidates replayed on
     the tape: mutations of a profitable strategy, or fresh ideas
   - the strategy with the best t-score, a real sample and a t-score of 2 or
     better becomes the CHAMPION for that market
   - a champion whose record AFTER the crown also reaches a t-score of 2 over
     a full sample is marked CONFIRMED. That is the lab's standard of proof.
 Every one of those steps is written to the log in plain English.

 HONESTY RULES
   - Paper only. Fills are the last price plus one tick of slippage against us
     on each side, and a round trip commission is charged per contract.
     Prices are checked once a minute, so a stop can fill past its level; the
     book records the real fill. Real fills would be somewhat worse.
   - Running many strategies at once means the best one looks good partly by
     luck. The book shows how high a t-score luck alone would be expected to
     reach for that many tries, and a champion's record is split: what it did
     BEFORE it was crowned (how it was picked) and what it did AFTER (the only
     number that counts).
   - Nothing is ever deleted. Retired strategies and their trades stay on the
     book, including the scalping and swing eras that came before.
   - The lab only trades on the real CME feed. On the ETF proxy feed it stays
     idle and says so.
==============================================================================
"""
import os
import json
import math
import bisect
import random
import sqlite3
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus_lab.db")
NEWS_DB = os.path.join(HERE, "korvus.db")
ET = ZoneInfo("America/New_York")

RULES = "v3-quant"            # the rule era. A new era retires the old bench on the record and seeds a new one.
OLD_ERA = "v1-scalp"          # what a strategy with no era on file is
SWING_ERA = "v2-swing"
RULING = ("Retired on the owner's ruling of 2026-11-13: no scalping on index futures, intraday swing trades only. "
          "Its record stays on the book.")
QUANT_RULING = ("Retired on 2026-11-16 when the lab moved to quantitative rules: trades are now sized by risk and "
                "strategies are judged by statistics, so its one-contract record cannot be compared with the new bench. "
                "Its rule set was entered in the tape test for a seat on the quant bench. Its record stays on the book.")
ERA_RULING = {OLD_ERA: RULING, SWING_ERA: QUANT_RULING}


def _num(key, default):
    try:
        v = (os.getenv(key, "") or "").split(" #")[0].strip()
        return float(v) if v else float(default)
    except ValueError:
        return float(default)


ENABLED       = (os.getenv("LAB_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
POPULATION    = int(_num("LAB_POPULATION", 14))        # strategies running per market
MIN_EVAL      = int(_num("LAB_MIN_EVAL_TRADES", 10))   # closed trades before a strategy can be judged
MIN_CHAMPION  = int(_num("LAB_MIN_CHAMPION_TRADES", 20))
IDLE_DAYS     = _num("LAB_IDLE_DAYS", 4)               # no trade in this long: retired as never firing
COMMISSION_RT = _num("LAB_COMMISSION_RT", 1.40)        # dollars per micro contract, round trip
SLIP_TICKS    = _num("LAB_SLIPPAGE_TICKS", 1)          # per side, always against us
COOLDOWN_MIN  = _num("LAB_COOLDOWN_MIN", 30)           # rest after a trade closes
MAX_TRADES_DAY = int(_num("LAB_MAX_TRADES_DAY", 2))    # per strategy, per trading day
RISK_USD      = _num("LAB_RISK_PER_TRADE", 150)        # dollars a trade should lose at its stop
MAX_QTY       = int(_num("LAB_MAX_CONTRACTS", 5))      # micro contracts, per trade
CROWN_T       = _num("LAB_CROWN_T", 2.0)               # t-score a strategy needs for the crown, and again after it
BT_DAYS       = _num("LAB_TAPE_TEST_DAYS", 14)         # calendar days of stored tape a candidate is replayed on
BT_CANDIDATES = int(_num("LAB_TAPE_TEST_CANDIDATES", 6))   # rivals replayed for each empty seat
BARS_KEEP_DAYS = max(BT_DAYS + 2, 6)                   # one minute bars kept on file
LAST_ENTRY    = (15, 30)                               # no new swing after this, ET
BARS_MIN      = 600                                    # minutes of tape each live tick looks at
GAP_EXIT_MIN  = 60                                     # a relative value trade is held at least an hour before "gap closed" can end it: no scalps

MARKETS = {
    "MNQ": {"name": "Micro Nasdaq-100", "mult": 2.0, "tick": 0.25, "news": ("MNQ", "NQ")},
    "MES": {"name": "Micro S&P 500 (ES price)", "mult": 5.0, "tick": 0.25, "news": ("MES", "ES")},
}
PEER = {"MNQ": "MES", "MES": "MNQ"}
REAL_FEEDS = ("databento", "tradovate")
SESSIONS = ("all", "asia", "london", "ny")
SESSION_START = {"asia": (18, 0), "london": (3, 0), "ny": (9, 30)}
SESSION_LABEL = {"all": "around the clock", "asia": "the Asia session only (6 PM to 3 AM ET)",
                 "london": "the London session only (3 AM to 9:30 AM ET)", "ny": "the New York session only (9:30 AM to 5 PM ET)"}
REGIME_LABEL = {"calm": "calm (the last hour is quieter than the last eight)", "normal": "normal",
                "fast": "fast (the last hour is busier than the last eight)"}

# the dials each family can turn. Targets, stops, confirmation and buffers are
# all multiples of the market's typical one hour move.
GRID = {
    "target": (1.0, 1.5, 2.0, 3.0, 4.0),
    "stop": (0.75, 1.0, 1.5, 2.0),
    "hold": (90, 120, 180, 240, 360),
    "session": SESSIONS,
    "protect": (0, 1),
    "regime": ("calm", "any", "fast"),
    "impact": ("high", "med"),
    "conf": (60, 70, 80),
    "age": (5, 10, 20, 30),
    "confirm": (0.2, 0.35, 0.5, 0.75),
    "overshoot": (0.5, 0.75, 1.0, 1.5),
    "lookback": (60, 120, 240, 480),
    "buffer": (0.1, 0.2, 0.3),
    "stretch": (2.0, 2.5, 3.0),
    "range_min": (15, 30, 60),
    "mz": (1.0, 1.5, 2.0),
    "rv_z": (1.5, 2.0, 2.5),
}
FAMILY_DIALS = {
    "momentum": ("lookback", "mz"),
    "rel_value": ("lookback", "rv_z"),
    "pullback": ("lookback", "stretch"),
    "breakout": ("lookback", "buffer"),
    "open_range": ("range_min", "buffer"),
    "news_follow": ("impact", "conf", "age", "confirm"),
    "news_fade": ("impact", "conf", "age", "overshoot"),
}
COMMON_DIALS = ("target", "stop", "hold", "session", "protect", "regime")
DIAL_NAME = {"target": "target", "stop": "stop", "hold": "maximum hold", "session": "session", "protect": "stop to entry",
             "regime": "tape condition",
             "impact": "minimum impact", "conf": "minimum confidence", "age": "story age limit", "confirm": "confirmation move",
             "overshoot": "overreaction size", "lookback": "lookback", "buffer": "break buffer", "stretch": "stretch",
             "range_min": "opening range", "mz": "trend strength", "rv_z": "gap size"}
FAMILY_LABEL = {"news_follow": "Go with the news", "news_fade": "Fade the overreaction", "breakout": "Multi-hour breakout",
                "pullback": "Snap back to the average", "open_range": "Opening range break",
                "momentum": "Ride the multi-hour trend", "rel_value": "Nasdaq against S&P gap"}


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
    scols = {r[1] for r in conn.execute("PRAGMA table_info(strategies)")}
    if "era" not in scols:
        conn.execute(f"ALTER TABLE strategies ADD COLUMN era TEXT DEFAULT '{OLD_ERA}'")
    if "backtest" not in scols:                          # what the tape test said when the strategy was picked
        conn.execute("ALTER TABLE strategies ADD COLUMN backtest TEXT")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    for col in ("init_stop_px", "mfe", "mae"):          # how far each trade ran for and against us, in points
        if col not in tcols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
    if "qty" not in tcols:                               # contracts, and the dollars the trade risked at its stop
        conn.execute("ALTER TABLE trades ADD COLUMN qty INTEGER")
    if "risk_usd" not in tcols:
        conn.execute("ALTER TABLE trades ADD COLUMN risk_usd REAL")
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
    """A swing needs room to work: nothing new from 3:30 PM ET until the reopen."""
    return phase(now_et) == "open" and not (LAST_ENTRY <= (now_et.hour, now_et.minute) < (18, 0))


def must_flat(now_et):
    return (16, 55) <= (now_et.hour, now_et.minute) < (18, 0)


def session_of(now_et):
    hm = (now_et.hour, now_et.minute)
    if hm >= (18, 0) or hm < (3, 0):
        return "asia"
    if hm < (9, 30):
        return "london"
    return "ny"


def trading_day(now_et):
    """The futures day runs 6 PM to 5 PM ET, so Sunday evening is already Monday."""
    return (now_et + dt.timedelta(hours=6)).date().isoformat()


def session_start(now_et, sess):
    """When the current run of this session began, as an aware datetime."""
    h, m = SESSION_START[sess]
    start = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    if start > now_et:
        start -= dt.timedelta(days=1)
    return start


# ----------------------------------------------------------------------------
# strategies: making them, describing them, changing them
# ----------------------------------------------------------------------------
def random_params(rng, family=None):
    family = family or rng.choice(list(FAMILY_DIALS))
    p = {d: rng.choice(GRID[d]) for d in COMMON_DIALS + FAMILY_DIALS[family]}
    p["regime"] = rng.choice(("any", "any", "calm", "fast"))   # most ideas should not need a special tape
    if family == "open_range" and p["session"] == "all":   # an opening range belongs to one session
        p["session"] = rng.choice(SESSIONS[1:])
    if p["target"] < p["stop"]:                 # a swing never risks more than it can make
        p["target"] = p["stop"]
    return family, p


def mutate(rng, family, params):
    """Move one or two dials one notch. Returns (new params, plain description)."""
    p = dict(params)
    p.setdefault("regime", "any")
    dials = list(COMMON_DIALS + FAMILY_DIALS[family])
    changed = []
    for d in rng.sample(dials, k=rng.choice((1, 1, 2))):
        opts = [o for o in GRID[d] if not (family == "open_range" and d == "session" and o == "all")]
        i = opts.index(p[d]) if p[d] in opts else 0
        j = min(len(opts) - 1, max(0, i + rng.choice((-1, 1))))
        if j == i:
            j = i + 1 if i == 0 else i - 1
        changed.append(f"{DIAL_NAME[d]} {p[d]} to {opts[j]}")
        p[d] = opts[j]
    if p["target"] < p["stop"]:
        p["target"] = p["stop"]
    return p, ", ".join(changed)


def describe(family, p):
    """The rule set in plain English. This is what the owner reads."""
    grade = "high impact" if p.get("impact") == "high" else "medium impact or higher"
    if family == "news_follow":
        head = (f"Go with the news. When a story scored {grade} at {p['conf']}%+ confidence "
                f"lands for this market, and price moves {p['confirm']}x a typical hour's move in the story's "
                f"direction within {p['age']} minutes, enter that way and hold for the swing.")
    elif family == "news_fade":
        head = (f"Fade the overreaction. When a story scored {grade} at {p['conf']}%+ confidence "
                f"lands and price runs {p['overshoot']}x a typical hour's move in the story's direction within "
                f"{p['age']} minutes, enter against it.")
    elif family == "breakout":
        head = (f"Trade the multi-hour break. When price clears the high of the last {p['lookback'] // 60} hour(s) by "
                f"{p['buffer']}x a typical hour's move go long; the same below the low, go short.")
    elif family == "open_range":
        head = (f"Trade the opening range. Mark the high and low of the first {p['range_min']} minutes of the session; "
                f"when price clears that range by {p['buffer']}x a typical hour's move, enter that way. One try per session, "
                f"within three hours of the open.")
    elif family == "momentum":
        head = (f"Ride the trend. Measure the move over the last {p['lookback'] // 60} hour(s) against what normal noise "
                f"would produce in that time; when it is {p['mz']}x that size or more, enter in its direction.")
    elif family == "rel_value":
        head = (f"Trade the gap between the two indexes. Fit how this market has moved against its sister index "
                f"(Nasdaq against S&P) over the last {p['lookback'] // 60} hour(s); when it sits {p['rv_z']} standard deviations "
                f"rich to that fit go short, when that cheap go long, and get out when the gap has closed "
                f"(after at least {GAP_EXIT_MIN} minutes).")
    else:
        head = (f"Trade the snap back. When price stretches {p['stretch']} standard deviations away from its "
                f"{p['lookback'] // 60} hour average, enter back toward the average.")
    prot = " Once up one stop distance, the stop moves to the entry price." if p.get("protect") else ""
    reg = p.get("regime", "any")
    tape = "" if reg == "any" else f" Only when the tape is {REGIME_LABEL[reg]}."
    return (f"{head} Target {p['target']}x and stop {p['stop']}x a typical hour's move, out after {p['hold'] / 60:g} "
            f"hours at most and always flat before 5 PM ET.{prot} Trades {SESSION_LABEL[p['session']]}, "
            f"at most {MAX_TRADES_DAY} a day.{tape} Sized to risk about ${RISK_USD:g} at the stop (1 to {MAX_QTY} contracts).")


def _next_id(conn, sym):
    n = int(meta_get(conn, f"seq_{sym}", "0")) + 1
    meta_set(conn, f"seq_{sym}", n)
    return f"{sym}-{n:03d}", n


def create_strategy(conn, sym, family, params, gen, parent, why, now_utc, backtest=None):
    sid, _ = _next_id(conn, sym)
    conn.execute("""INSERT INTO strategies (id,sym,gen,parent,family,params,status,created_utc,why,state,era,backtest)
                    VALUES (?,?,?,?,?,?, 'active', ?,?, '{}', ?,?)""",
                 (sid, sym, gen, parent, family, json.dumps(params), now_utc.isoformat(), why, RULES,
                  json.dumps(backtest) if backtest else None))
    note(conn, sym, "created", f"{sid} created. {why} Rules: {describe(family, params)}", now_utc)
    return sid


def seed(conn, sym, now_utc, carry=(), bt=None):
    """The first generation of this era: every family represented. Each seat goes
    to the best of a few candidates replayed on the stored tape; rule sets from
    the era before are entered as candidates for their own family."""
    rng = random.Random(f"{sym}-seed-{RULES}")
    fams = list(FAMILY_DIALS)
    pool = {}
    for old_id, family, p in carry:
        if family in FAMILY_DIALS and all(d in p for d in FAMILY_DIALS[family]):
            pool.setdefault(family, []).append((old_id, {**p, "regime": p.get("regime", "any")}))
    taken = set()
    for i in range(POPULATION):
        family = fams[i % len(fams)]
        cands = []
        for _ in range(2):                               # up to two old rule sets get a try at each seat
            if pool.get(family):
                old_id, p = pool[family].pop(0)
                if json.dumps(p, sort_keys=True) not in taken:
                    cands.append((family, p, old_id))
        for _try in range(40):
            if len(cands) >= max(1, BT_CANDIDATES):
                break
            _, p = random_params(rng, family)
            if json.dumps(p, sort_keys=True) not in taken and all(p != c[1] for c in cands):
                cands.append((family, p, None))
        k, res, text = choose(conn, sym, cands, now_utc, bt)
        family, p, old_id = cands[k]
        taken.add(json.dumps(p, sort_keys=True))
        why = "First generation of the quant bench. " + text
        if old_id:
            why += f" It carries the rules of {old_id} from the swing era."
        create_strategy(conn, sym, family, p, 1, None, why, now_utc, res)


def migrate(conn, sym, px, now_utc, m, bt=None):
    """A new rule era. Close and retire the old bench on the record, seed the new one."""
    old = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE sym=? AND status IN ('active','champion') AND era != ?", (sym, RULES))]
    carry = []
    for s in old:
        t = conn.execute("SELECT * FROM trades WHERE strat=? AND status='open'", (s["id"],)).fetchone()
        if t:
            _close(conn, dict(t), px, "rules changed", now_utc, m)
        era = s.get("era") or OLD_ERA
        conn.execute("UPDATE strategies SET status='retired', retired_utc=?, retired_why=?, promoted_utc=NULL WHERE id=?",
                     (now_utc.isoformat(), ERA_RULING.get(era, QUANT_RULING), s["id"]))
        if era == SWING_ERA:
            try:
                carry.append((s["id"], s["family"], json.loads(s["params"])))
            except Exception:
                pass
    if old:
        eras = sorted({s.get("era") or OLD_ERA for s in old})
        note(conn, sym, "rules", f"New rule era {RULES}: quantitative. Still intraday swings with no scalping; now every trade is "
             f"sized to risk about ${RISK_USD:g}, a new rule set is replayed on stored tape before it gets a seat, and strategies "
             f"are judged by average R and t-score. {len(old)} strategies of the {' and '.join(eras)} era retired on the record; "
             f"their trades stay on the book.", now_utc)
    if not conn.execute("SELECT 1 FROM strategies WHERE sym=? AND era=? LIMIT 1", (sym, RULES)).fetchone():
        seed(conn, sym, now_utc, carry, bt)
    meta_set(conn, f"rules_{sym}", RULES)


# ----------------------------------------------------------------------------
# market data helpers
# ----------------------------------------------------------------------------
def save_bar(conn, sym, now_utc, px):
    ts = int(now_utc.timestamp() // 60 * 60)
    conn.execute("INSERT INTO bars (sym,ts,px) VALUES (?,?,?) ON CONFLICT(sym,ts) DO UPDATE SET px=excluded.px", (sym, ts, px))
    conn.execute("DELETE FROM bars WHERE sym=? AND ts < ?", (sym, ts - int(BARS_KEEP_DAYS * 86400)))


def recent_bars(conn, sym, now_utc, minutes):
    since = int(now_utc.timestamp()) - minutes * 60
    return [(r["ts"], r["px"]) for r in conn.execute("SELECT ts,px FROM bars WHERE sym=? AND ts>=? ORDER BY ts", (sym, since))]


class Tape:
    """One market's one minute bars, with the rolling numbers every strategy
    needs worked out once and remembered. The live tick builds one from the last
    ten hours; the tape test builds one from days of stored bars. Both run the
    same signal, sizing and exit code on it, which is what makes the test honest."""

    def __init__(self, bars, tick, peer=None):
        self.ts = [int(b[0]) for b in bars]
        self.px = [float(b[1]) for b in bars]
        self.tick = tick
        self.npx = np.asarray(self.px, dtype=float)
        nts = np.asarray(self.ts, dtype=np.int64)
        self._d = np.diff(self.npx) if len(bars) > 1 else np.zeros(0)
        self._ok = (np.diff(nts) <= 180) if len(bars) > 1 else np.zeros(0, dtype=bool)
        self._lpx = np.log(self.npx) if len(bars) else np.zeros(0)
        peer = peer or {}
        self.has_peer = bool(peer)
        self._lpeer = np.array([math.log(peer[t]) if peer.get(t) else np.nan for t in self.ts], dtype=float)
        self._unit, self._regime, self._clock = {}, {}, {}

    def __len__(self):
        return len(self.ts)

    def start(self, i, minutes):
        """Index of the first bar inside the last `minutes` ending at bar i."""
        return bisect.bisect_left(self.ts, self.ts[i] - minutes * 60, 0, i + 1)

    def unit(self, i):
        """The market's typical ONE HOUR move, from the last four hours of 1 minute
        changes. Targets and stops are multiples of this, so they widen when the
        market is fast and tighten when it is quiet. None until there is enough tape."""
        if i in self._unit:
            return self._unit[i]
        u = None
        a = max(i - 239, self.start(i, BARS_MIN), 0)      # never reach back past what a live tick can see
        if i + 1 - a >= 45:
            d = self._d[a:i][self._ok[a:i]]
            if d.size >= 30:
                u = max(float(d.std()) * math.sqrt(60), self.tick * 16)
        self._unit[i] = u
        return u

    def regime(self, i):
        """calm | normal | fast: the last hour's one minute volatility against the
        last eight hours'. normal when there is not enough tape to say."""
        if i in self._regime:
            return self._regime[i]
        r = "normal"
        a, b = self.start(i, 480), self.start(i, 60)
        long_, short = self._d[a:i][self._ok[a:i]], self._d[b:i][self._ok[b:i]]
        if long_.size >= 200 and short.size >= 30:
            sl = float(long_.std())
            if sl > 0:
                ratio = float(short.std()) / sl
                r = "calm" if ratio <= 0.85 else "fast" if ratio >= 1.15 else "normal"
        self._regime[i] = r
        return r

    def live(self, i):
        """A market that has printed the same price for 10 straight minutes is not
        trading (holiday, halt, dead feed)."""
        last = self.px[max(i - 9, self.start(i, BARS_MIN), 0):i + 1]
        return len(last) < 10 or len(set(last)) > 1

    def clock(self, i):
        """(utc, et, phase, can enter, must be flat, session, trading day) for bar i."""
        c = self._clock.get(i)
        if c is None:
            u = dt.datetime.fromtimestamp(self.ts[i], dt.timezone.utc)
            e = u.astimezone(ET)
            c = (u, e, phase(e), can_enter(e), must_flat(e), session_of(e), trading_day(e))
            self._clock[i] = c
        return c

    def gap_z(self, i, lookback):
        """How far this market sits from its usual relationship with its sister
        index, in standard deviations. The relationship is a straight line fit of
        this market's log price on the other's over the lookback, not counting
        the current minute. None when the two tapes do not line up well enough."""
        if not self.has_peer or math.isnan(self._lpeer[i]):
            return None
        a = self.start(i, lookback)
        x, y = self._lpeer[a:i], self._lpx[a:i]
        ok = ~np.isnan(x)
        if int(ok.sum()) < lookback * 0.6:
            return None
        x, y = x[ok], y[ok]
        vx = float(x.var())
        if vx <= 1e-14:
            return None
        beta = float(((x - x.mean()) * (y - y.mean())).mean()) / vx
        if not 0.2 <= beta <= 5.0:                       # the two indexes always move together; anything else is a bad fit
            return None
        resid = y - beta * x
        sd = float(resid.std())
        if sd <= 1e-9:
            return None
        return (float(self._lpx[i] - beta * self._lpeer[i]) - float(resid.mean())) / sd


def vol_unit(bars, tick):
    """The market's typical one hour move for a list of (ts, price) bars."""
    return Tape(bars, tick).unit(len(bars) - 1) if bars else None


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
def signal(family, p, st, tape, i, unit, news, now_utc):
    """Returns (side, note) or (None, None). st is the strategy's own memory;
    i is the current bar on the tape."""
    px = tape.px[i]
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
            j = bisect.bisect_left(tape.ts, int(t0.timestamp() // 60 * 60), 0, i + 1)
            if j > i:
                continue
            ref = tape.px[j]
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
    if family == "open_range":
        now_et = now_utc.astimezone(ET)
        sess = p["session"]
        start = session_start(now_et, sess)
        mins = (now_et - start).total_seconds() / 60.0
        if mins < p["range_min"] or mins > p["range_min"] + 180:
            return None, None
        key = f"{start.date().isoformat()}-{sess}"
        if st.get("or_done") == key:
            return None, None
        t0, t1 = int(start.timestamp()), int(start.timestamp()) + p["range_min"] * 60
        rng = tape.px[bisect.bisect_left(tape.ts, t0, 0, i + 1):bisect.bisect_left(tape.ts, t1, 0, i + 1)]
        if len(rng) < max(8, p["range_min"] * 0.6):
            return None, None
        hi, lo, buf = max(rng), min(rng), p["buffer"] * unit
        if px > hi + buf:
            st["or_done"] = key
            return "long", f"cleared the first {p['range_min']} minute high of the {sess} session ({hi:.2f})"
        if px < lo - buf:
            st["or_done"] = key
            return "short", f"cleared the first {p['range_min']} minute low of the {sess} session ({lo:.2f})"
        return None, None
    hours = p["lookback"] // 60
    if family == "rel_value":
        z = tape.gap_z(i, p["lookback"])
        if z is None:
            return None, None
        if z >= p["rv_z"]:
            return "short", f"{z:.1f} deviations rich to its sister index over {hours} hour(s)"
        if z <= -p["rv_z"]:
            return "long", f"{abs(z):.1f} deviations cheap to its sister index over {hours} hour(s)"
        return None, None
    a = tape.start(i, p["lookback"])
    if i - a < p["lookback"] * 0.6:              # needs most of the window on tape
        return None, None
    if family == "momentum":
        move = px - tape.px[a]
        z = move / (unit * math.sqrt(p["lookback"] / 60.0))
        if z >= p["mz"]:
            return "long", f"up {move:.2f} over {hours} hour(s), {z:.1f}x what normal noise gives"
        if z <= -p["mz"]:
            return "short", f"down {abs(move):.2f} over {hours} hour(s), {abs(z):.1f}x what normal noise gives"
        return None, None
    look = tape.npx[a:i]
    if family == "breakout":
        buf, hi, lo = p["buffer"] * unit, float(look.max()), float(look.min())
        if px > hi + buf:
            return "long", f"cleared the {hours} hour high ({hi:.2f})"
        if px < lo - buf:
            return "short", f"cleared the {hours} hour low ({lo:.2f})"
        return None, None
    mean, sd = float(look.mean()), float(look.std())
    if sd <= 0:
        return None, None
    z = (px - mean) / sd
    if z <= -p["stretch"]:
        return "long", f"{abs(z):.1f} deviations below its {hours} hour average"
    if z >= p["stretch"]:
        return "short", f"{z:.1f} deviations above its {hours} hour average"
    return None, None


# ----------------------------------------------------------------------------
# one trade: sizing, the plan, managing it, closing it. The live tick and the
# tape test both go through these, so a replayed trade is priced like a real one.
# ----------------------------------------------------------------------------
def plan_entry(side, px, p, unit, m):
    """Entry fill, target, stop, contracts and dollars at risk for a new trade."""
    way = 1 if side == "long" else -1
    fill = round(px + way * m["tick"] * SLIP_TICKS, 2)
    tgt = round(fill + way * p["target"] * unit, 2)
    stp = round(fill - way * p["stop"] * unit, 2)
    per = abs(fill - stp) * m["mult"]                    # dollars one contract loses at the stop
    qty = max(1, min(MAX_QTY, int(RISK_USD // per))) if per > 0 else 1
    return fill, tgt, stp, qty, round(per * qty, 2)


def exit_fill(t, px, m):
    """(fill, P&L) for closing trade t at price px, after slippage and commission."""
    slip = m["tick"] * SLIP_TICKS
    fill = px - slip if t["side"] == "long" else px + slip
    way = 1 if t["side"] == "long" else -1
    qty = t.get("qty") or 1
    return round(fill, 2), round((fill - t["entry_px"]) * way * m["mult"] * qty - COMMISSION_RT * qty, 2)


def manage(t, px, live, protect, now_et, now_utc):
    """Work an open trade for one minute. Updates its best and worst run and
    may move its stop to the entry. Returns (why it should close or None,
    whether the stop just moved)."""
    way = 1 if t["side"] == "long" else -1
    run = (px - t["entry_px"]) * way
    if live:
        t["mfe"] = round(max(t.get("mfe") or 0.0, run), 2)
        t["mae"] = round(min(t.get("mae") or 0.0, run), 2)
    init, moved = t.get("init_stop_px"), False
    if (live and protect and init is not None and abs(t["stop_px"] - init) < 1e-9
            and run >= abs(t["entry_px"] - init) and (px - t["target_px"]) * way < 0):
        t["stop_px"] = t["entry_px"]
        moved = True
    reason = None
    if live and (px - t["target_px"]) * way >= 0:
        reason = "target"
    elif live and (t["stop_px"] - px) * way >= 0:
        reason = "stop at entry" if init is not None and abs(t["stop_px"] - init) > 1e-9 else "stop"
    elif must_flat(now_et):
        reason = "flat before maintenance"
    elif now_utc >= (t.get("_dead") or _parse_utc(t["deadline_utc"])):
        reason = "time limit"
    return reason, moved


def gap_closed(t, tape, i, p, now_utc):
    """A relative value trade is done when the gap it traded has closed."""
    if (now_utc - (t.get("_in") or _parse_utc(t["entry_utc"]))).total_seconds() < GAP_EXIT_MIN * 60:
        return False
    z = tape.gap_z(i, p["lookback"])
    return z is not None and (z >= 0 if t["side"] == "long" else z <= 0)


# ----------------------------------------------------------------------------
# the tape test: replay a rule set over stored bars before it gets a seat
# ----------------------------------------------------------------------------
def simulate(sym, family, p, tape, news=(), start_ts=None):
    """Replay one rule set over a tape with the live rules, costs and sizing.
    Returns its closed trades, oldest first. A trade still open at the end of
    the tape is left out."""
    m = MARKETS[sym]
    items = sorted(((_parse_utc(it["created_at"]).timestamp(), it) for it in _news_for(sym, news)), key=lambda x: x[0])
    nts = [x[0] for x in items]
    st, out, t, per_day = {}, [], None, {}
    protect, reg, sess_want = p.get("protect"), p.get("regime", "any"), p["session"]
    i0 = 0 if start_ts is None else bisect.bisect_left(tape.ts, int(start_ts))
    for i in range(i0, len(tape)):
        now_utc, now_et, ph, may_enter, _flat, sess, day = tape.clock(i)
        if ph != "open":
            continue
        px, live = tape.px[i], tape.live(i)
        if t:
            reason, _ = manage(t, px, live, protect, now_et, now_utc)
            if not reason and family == "rel_value" and live and gap_closed(t, tape, i, p, now_utc):
                reason = "gap closed"
            if reason:
                fill, pnl = exit_fill(t, px, m)
                t.update(exit_utc=now_utc.isoformat(), exit_px=fill, reason=reason, pnl=pnl, status="closed")
                t.pop("_dead", None), t.pop("_in", None)
                out.append(t)
                st["last_exit"], t = now_utc, None
            continue
        unit = tape.unit(i)
        if unit is None or not live or not may_enter:
            continue
        if sess_want != "all" and sess_want != sess:
            continue
        if st.get("last_exit") and (now_utc - st["last_exit"]).total_seconds() < COOLDOWN_MIN * 60:
            continue
        if per_day.get(day, 0) >= MAX_TRADES_DAY:
            continue
        if reg != "any" and tape.regime(i) != reg:
            continue
        lo = bisect.bisect_left(nts, tape.ts[i] - 2700)
        hi = bisect.bisect_right(nts, tape.ts[i])
        side, why = signal(family, p, st, tape, i, unit, [x[1] for x in items[lo:hi]], now_utc)
        if not side:
            continue
        fill, tgt, stp, qty, risk = plan_entry(side, px, p, unit, m)
        dead = now_utc + dt.timedelta(minutes=p["hold"])
        t = {"sym": sym, "side": side, "entry_utc": now_utc.isoformat(), "entry_px": fill, "target_px": tgt, "stop_px": stp,
             "init_stop_px": stp, "deadline_utc": dead.isoformat(), "qty": qty, "risk_usd": risk, "note": why,
             "mfe": 0.0, "mae": 0.0, "_dead": dead, "_in": now_utc}
        per_day[day] = per_day.get(day, 0) + 1
    return out


def tape_for_test(conn, sym, now_utc, bt=None):
    """(tape, news, days of tape) for the tape test, or None when under a day of
    bars is on file. bt is a dict shared across one pass so each market's tape
    is loaded once."""
    bt = bt if bt is not None else {}
    if sym not in bt:
        since = int(now_utc.timestamp() - BT_DAYS * 86400)
        rows = {s: [(r["ts"], r["px"]) for r in conn.execute("SELECT ts,px FROM bars WHERE sym=? AND ts>=? ORDER BY ts", (s, since))]
                for s in (sym, PEER[sym])}
        bars = rows[sym]
        if len(bars) < 900:                              # less than about a day of minutes: nothing worth replaying
            bt[sym] = None
        else:
            tape = Tape(bars, MARKETS[sym]["tick"], dict(rows[PEER[sym]]))
            news = bt.get("_news")
            if news is None:
                news = bt["_news"] = fetch_news(dt.datetime.fromtimestamp(bars[0][0], dt.timezone.utc))
            bt[sym] = (tape, news, round(len(bars) / 1380.0, 1))
    return bt[sym]


def choose(conn, sym, cands, now_utc, bt=None):
    """Replay each candidate (family, params, ...) on the stored tape and pick
    one. Returns (index, the pick's test result or None, a sentence for the
    record). The pick is the best average R among candidates that traded at
    least three times, else the one that traded most."""
    ctx = tape_for_test(conn, sym, now_utc, bt)
    if not ctx or len(cands) < 1:
        return 0, None, "Not enough stored tape yet to test candidates first, so this one starts as a guess."
    tape, news, days = ctx
    res = []
    for family, p, *_ in cands:
        s = stats(simulate(sym, family, p, tape, news))
        res.append({"days": days, "trades": s["trades"], "exp_r": s["exp_r"], "win_rate": s["win_rate"], "of": len(cands)})
    real = [k for k, r in enumerate(res) if r["trades"] >= 3 and r["exp_r"] is not None]
    if real:
        k = max(real, key=lambda j: res[j]["exp_r"])
        r = res[k]
        text = (f"{len(cands)} candidates were replayed on {days:g} days of stored tape with live costs and sizing; this one "
                f"did best: {r['trades']} trades at {r['exp_r']:+.2f}R a trade. The replay earns the seat and proves nothing; "
                f"its live paper record is the test.")
    else:
        k = max(range(len(res)), key=lambda j: res[j]["trades"])
        text = (f"{len(cands)} candidates were replayed on {days:g} days of stored tape and none traded three times, "
                f"so there was too little to rank them; this one traded most ({res[k]['trades']}).")
    return k, res[k], text


# ----------------------------------------------------------------------------
# the tick: manage exits, look for entries, review once a day
# ----------------------------------------------------------------------------
def _close(conn, t, px, reason, now_utc, m):
    fill, pnl = exit_fill(t, px, m)
    conn.execute("UPDATE trades SET exit_utc=?, exit_px=?, reason=?, pnl=?, status='closed' WHERE id=?",
                 (now_utc.isoformat(), fill, reason, pnl, t["id"]))
    return pnl


def _trades_today(conn, sid, now_et):
    day_start = (now_et + dt.timedelta(hours=6)).replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(hours=6)
    return conn.execute("SELECT COUNT(*) c FROM trades WHERE strat=? AND entry_utc >= ?",
                        (sid, day_start.astimezone(dt.timezone.utc).isoformat())).fetchone()["c"]


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
    bt = {}                                              # tapes for the tape test, loaded at most once a pass
    try:
        if ph == "maintenance" and meta_get(conn, "reviewed_day") != now_et.strftime("%Y-%m-%d"):
            for sym in MARKETS:
                if meta_get(conn, f"rules_{sym}") == RULES:
                    review(conn, sym, now_utc, bt)
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
        news_all = (news_fn or fetch_news)(now_utc - dt.timedelta(minutes=45))

        # both markets' bars go on file first: a relative value strategy needs
        # this minute's price for its own market and for the sister index
        prices = {}
        for sym in MARKETS:
            px = (q.get(sym) or {}).get("price")
            if px:
                prices[sym] = float(px)
                save_bar(conn, sym, now_utc, prices[sym])
        raw = {sym: recent_bars(conn, sym, now_utc, BARS_MIN) for sym in prices}

        for sym, m in MARKETS.items():
            if sym not in prices:
                continue
            px = prices[sym]
            if meta_get(conn, f"rules_{sym}") != RULES:
                migrate(conn, sym, px, now_utc, m, bt)
            tape = Tape(raw[sym], m["tick"], dict(raw.get(PEER[sym]) or ()))
            i = len(tape) - 1
            unit = tape.unit(i)
            meta_set(conn, f"unit_{sym}", "" if unit is None else round(unit, 2))
            live = tape.live(i)
            regime = tape.regime(i)
            meta_set(conn, f"regime_{sym}", regime)
            news = _news_for(sym, news_all)
            sess = session_of(now_et)
            for s in [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE sym=? AND status IN ('active','champion')", (sym,))]:
                p, st = json.loads(s["params"]), json.loads(s["state"] or "{}")
                t = conn.execute("SELECT * FROM trades WHERE strat=? AND status='open'", (s["id"],)).fetchone()
                if t:
                    t = dict(t)
                    reason, moved = manage(t, px, live, p.get("protect"), now_et, now_utc)
                    if live:
                        conn.execute("UPDATE trades SET mfe=?, mae=?, stop_px=? WHERE id=?", (t["mfe"], t["mae"], t["stop_px"], t["id"]))
                    if moved:
                        note(conn, sym, "manage", f"{s['id']} is up one stop distance: stop moved to the entry price {t['entry_px']:.2f}", now_utc)
                    if not reason and s["family"] == "rel_value" and live and gap_closed(t, tape, i, p, now_utc):
                        reason = "gap closed"
                    if reason:
                        pnl = _close(conn, t, px, reason, now_utc, m)
                        st["last_exit"] = now_utc.isoformat()
                        conn.execute("UPDATE strategies SET state=? WHERE id=?", (json.dumps(st), s["id"]))
                        held = (now_utc - _parse_utc(t["entry_utc"])).total_seconds() / 60.0
                        risk = _risk(t)
                        note(conn, sym, "exit", f"{s['id']} closed {t['side']} at {px:.2f} on {reason} after {held:.0f} min: "
                             f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}" + (f" ({pnl / risk:+.2f}R)" if risk else ""), now_utc)
                        out["exits"] += 1
                    continue
                if unit is None or not live or not can_enter(now_et):
                    continue
                if p["session"] != "all" and p["session"] != sess:
                    continue
                if st.get("last_exit") and (now_utc - _parse_utc(st["last_exit"])).total_seconds() < COOLDOWN_MIN * 60:
                    continue
                if _trades_today(conn, s["id"], now_et) >= MAX_TRADES_DAY:
                    continue
                if p.get("regime", "any") != "any" and p["regime"] != regime:
                    continue
                side, why = signal(s["family"], p, st, tape, i, unit, news, now_utc)
                conn.execute("UPDATE strategies SET state=? WHERE id=?", (json.dumps(st), s["id"]))
                if not side:
                    continue
                fill, tgt, stp, qty, risk = plan_entry(side, px, p, unit, m)
                dead = now_utc + dt.timedelta(minutes=p["hold"])
                conn.execute("""INSERT INTO trades (strat,sym,side,entry_utc,entry_px,target_px,stop_px,deadline_utc,status,note,
                                                    init_stop_px,mfe,mae,qty,risk_usd)
                                VALUES (?,?,?,?,?,?,?,?, 'open', ?,?,0,0,?,?)""",
                             (s["id"], sym, side, now_utc.isoformat(), fill, tgt, stp, dead.isoformat(), why, stp, qty, risk))
                note(conn, sym, "entry", f"{s['id']} {side} {qty} {sym} at {fill:.2f}, {why}. Target {tgt:.2f} "
                     f"(+{abs(tgt - fill):.2f}), stop {stp:.2f} (-{abs(fill - stp):.2f}), ${risk:.0f} at risk, "
                     f"up to {p['hold'] / 60:g} hours", now_utc)
                out["entries"] += 1
        conn.commit()
    finally:
        conn.close()
    return out


# ----------------------------------------------------------------------------
# scoring and the daily review
# ----------------------------------------------------------------------------
def _risk(t):
    """Dollars the trade stood to lose at its first stop. Older one-contract
    trades carry no figure, so it is worked out from their stop."""
    r = t.get("risk_usd")
    if r:
        return float(r)
    if t.get("init_stop_px") is not None and t.get("entry_px") is not None and t.get("sym") in MARKETS:
        r = abs(t["entry_px"] - t["init_stop_px"]) * MARKETS[t["sym"]]["mult"] * (t.get("qty") or 1)
        return r if r > 0 else None
    return None


def stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": None, "pnl": 0.0, "expectancy": None, "profit_factor": None,
                "avg_win": None, "avg_loss": None, "avg_hold_min": None, "max_drawdown": 0.0,
                "exp_r": None, "total_r": 0.0, "t_stat": None}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] <= 0]
    pnl = sum(t["pnl"] for t in trades)
    holds = []
    for t in trades:
        try:
            holds.append((_parse_utc(t["exit_utc"]) - _parse_utc(t["entry_utc"])).total_seconds() / 60.0)
        except Exception:
            pass
    peak = cum = mdd = 0.0
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    # R: each result as a multiple of what the trade risked. The t-score is the
    # average R against its own noise: how many standard errors it sits from zero.
    rs = [t["pnl"] / r for t in trades for r in [_risk(t)] if r]
    exp_r = sum(rs) / len(rs) if rs else None
    t_stat = None
    if len(rs) >= 3:
        var = sum((x - exp_r) ** 2 for x in rs) / (len(rs) - 1)
        if var > 0:
            t_stat = exp_r / math.sqrt(var / len(rs))
    return {"trades": n, "wins": len(wins), "win_rate": round(len(wins) / n * 100, 1), "pnl": round(pnl, 2),
            "expectancy": round(pnl / n, 2),
            "profit_factor": round(sum(wins) / sum(losses), 2) if sum(losses) > 0 else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(-sum(losses) / len(losses), 2) if losses else None,
            "avg_hold_min": round(sum(holds) / len(holds)) if holds else None,
            "max_drawdown": round(mdd, 2),
            "exp_r": round(exp_r, 3) if exp_r is not None else None, "total_r": round(sum(rs), 2),
            "t_stat": round(t_stat, 2) if t_stat is not None else None}


def luck_bar(tried):
    """With this many strategies tried, about how high a t-score the luckiest
    one would be expected to reach if none of them had any edge at all."""
    return round(math.sqrt(2 * math.log(tried)), 2) if tried and tried > 1 else 0.0


def _closed(conn, sid):
    return [dict(r) for r in conn.execute("SELECT * FROM trades WHERE strat=? AND status='closed' ORDER BY exit_utc", (sid,))]


def _edge(st):
    """The number a strategy is judged on: average R, or dollars a trade when no R is on file."""
    return st["exp_r"] if st["exp_r"] is not None else (st["expectancy"] or 0)


def review(conn, sym, now_utc, bt=None):
    """The daily learning step. Retire, breed, and crown; write down why."""
    rng = random.Random(f"{sym}-{now_utc.date().isoformat()}")
    live = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE sym=? AND status IN ('active','champion')", (sym,))]
    scored = [(s, stats(_closed(conn, s["id"]))) for s in live]
    retire = []
    for s, st in scored:
        age_d = (now_utc - _parse_utc(s["created_utc"])).total_seconds() / 86400
        if st["trades"] >= MIN_EVAL and _edge(st) < 0:
            retire.append((s, st, f"Retired after {st['trades']} trades: it lost {abs(_edge(st)):.2f}R a trade on average "
                                  f"(${st['pnl']:.2f} in total, {st['win_rate']}% winners)."))
        elif st["trades"] >= 6 and st["t_stat"] is not None and st["t_stat"] <= -CROWN_T:
            retire.append((s, st, f"Retired early after {st['trades']} trades: a t-score of {st['t_stat']:.1f} means its losses "
                                  f"are too steady to be bad luck ({_edge(st):+.2f}R a trade, ${st['pnl']:.2f} in total)."))
        elif st["trades"] == 0 and age_d >= IDLE_DAYS:
            retire.append((s, st, f"Retired: no trade in {age_d:.0f} days, its conditions never came up."))
    # at most a third of the bench turns over in one review, worst first
    retire.sort(key=lambda r: _edge(r[1]))
    retire = retire[: max(1, POPULATION // 3)]
    for s, _st, why in retire:
        conn.execute("UPDATE strategies SET status='retired', retired_utc=?, retired_why=? WHERE id=?",
                     (now_utc.isoformat(), why, s["id"]))
        t = conn.execute("SELECT * FROM trades WHERE strat=? AND status='open'", (s["id"],)).fetchone()
        if t:
            conn.execute("UPDATE trades SET status='closed', exit_utc=?, exit_px=entry_px, reason='strategy retired', pnl=? WHERE id=?",
                         (now_utc.isoformat(), -COMMISSION_RT * (t["qty"] or 1), t["id"]))
        note(conn, sym, "retired", f"{s['id']} retired. {why}", now_utc)
    gone = {s["id"] for s, _st, _why in retire}
    keep = [(s, st) for s, st in scored if s["id"] not in gone]
    winners = sorted([(s, st) for s, st in keep if st["trades"] >= max(4, MIN_EVAL // 2) and _edge(st) > 0],
                     key=lambda x: -_edge(x[1]))
    existing = {(s["family"], json.dumps(json.loads(s["params"]), sort_keys=True)) for s, _ in keep}
    made_n = 0
    for _ in range(POPULATION - len(keep)):
        cands = []
        par = pst = None
        if winners and rng.random() < 0.75:
            par, pst = winners[rng.randrange(min(3, len(winners)))]
            for _try in range(12):
                if len(cands) >= max(1, BT_CANDIDATES):
                    break
                p, what = mutate(rng, par["family"], json.loads(par["params"]))
                if (par["family"], json.dumps(p, sort_keys=True)) not in existing and all(p != c[1] for c in cands):
                    cands.append((par["family"], p, what))
        if not cands:
            par = None
            for _try in range(40):
                if len(cands) >= max(1, BT_CANDIDATES):
                    break
                family, p = random_params(rng)
                if (family, json.dumps(p, sort_keys=True)) not in existing and all((family, p) != (c[0], c[1]) for c in cands):
                    cands.append((family, p, None))
        if not cands:
            continue
        k, res, text = choose(conn, sym, cands, now_utc, bt)
        family, p, what = cands[k]
        if par:
            why = (f"Bred from {par['id']}, which is making {_edge(pst):+.2f}R a trade over {pst['trades']} trades. "
                   f"Changed: {what}. {text}")
            create_strategy(conn, sym, family, p, par["gen"] + 1, par["id"], why, now_utc, res)
        else:
            why = ("A fresh idea, because no strategy on the bench is profitable enough to breed from yet. "
                   if not winners else "A fresh idea, to keep the search from narrowing too early. ") + text
            create_strategy(conn, sym, family, p, 1, None, why, now_utc, res)
        existing.add((family, json.dumps(p, sort_keys=True)))
        made_n += 1
    # the crown: the best t-score with a real sample, a positive average R,
    # winners outweighing losers, and a t-score that luck alone rarely reaches
    tried = conn.execute("SELECT COUNT(*) c FROM strategies WHERE sym=? AND era=?", (sym, RULES)).fetchone()["c"]
    cands = [(s, st) for s, st in keep if st["trades"] >= MIN_CHAMPION and _edge(st) > 0 and (st["profit_factor"] or 9) >= 1.1
             and (st["t_stat"] or 0) >= CROWN_T]
    cur = next((s for s, _ in keep if s["status"] == "champion"), None)
    if cands:
        best, bst = max(cands, key=lambda x: x[1]["t_stat"])
        if not cur or cur["id"] != best["id"]:
            if cur:
                conn.execute("UPDATE strategies SET status='active', promoted_utc=NULL WHERE id=?", (cur["id"],))
            conn.execute("UPDATE strategies SET status='champion', promoted_utc=? WHERE id=?", (now_utc.isoformat(), best["id"]))
            note(conn, sym, "champion", f"{best['id']} is now the champion for {sym}: {_edge(bst):+.2f}R a trade over "
                 f"{bst['trades']} trades, {bst['win_rate']}% winners, t-score {bst['t_stat']:.1f}. The lab has tried {tried} "
                 f"strategies in {sym}, and the luckiest of that many would be expected to reach about {luck_bar(tried):.1f} with no "
                 f"edge at all, so this is a lead, not proof. Its record from here on is the test that counts."
                 + (f" It replaces {cur['id']}." if cur else ""), now_utc)
    # a champion that stops working after it was crowned gives the crown back:
    # that after-the-crown record is the honest test, so it gets the last word.
    # One that keeps working over a full sample is confirmed.
    cur = conn.execute("SELECT * FROM strategies WHERE sym=? AND status='champion'", (sym,)).fetchone()
    if cur and cur["promoted_utc"]:
        after = stats([t for t in _closed(conn, cur["id"]) if t["exit_utc"] > cur["promoted_utc"]])
        if after["trades"] >= MIN_EVAL and _edge(after) < 0:
            conn.execute("UPDATE strategies SET status='active', promoted_utc=NULL WHERE id=?", (cur["id"],))
            note(conn, sym, "champion", f"{cur['id']} lost the crown: since it was crowned it has lost {abs(_edge(after)):.2f}R "
                 f"a trade over {after['trades']} trades. It looked good before the crown and did not hold up after. "
                 f"No champion for {sym} right now.", now_utc)
        elif _confirmed(after) and not meta_get(conn, f"confirmed_{cur['id']}"):
            meta_set(conn, f"confirmed_{cur['id']}", now_utc.isoformat())
            note(conn, sym, "champion", f"{cur['id']} is CONFIRMED: counting only the {after['trades']} trades since it was crowned, "
                 f"it has made {_edge(after):+.2f}R a trade (${after['pnl']:.2f}) with a t-score of {after['t_stat']:.1f}. Nothing "
                 f"picked those trades in hindsight, so this is the lab's standard of proof. Still paper, still one market.", now_utc)
    note(conn, sym, "review", f"Daily review done: {len(retire)} retired, {made_n} created, "
         f"{len(winners)} profitable so far.", now_utc)


def _confirmed(after):
    return bool(after and after["trades"] >= MIN_CHAMPION and _edge(after) > 0 and (after["t_stat"] or 0) >= CROWN_T)


# ----------------------------------------------------------------------------
# the book: everything the admin panel and the vault show
# ----------------------------------------------------------------------------
def _hold_min(t, now_utc=None):
    try:
        end = _parse_utc(t["exit_utc"]) if t.get("exit_utc") else (now_utc or dt.datetime.now(dt.timezone.utc))
        return round((end - _parse_utc(t["entry_utc"])).total_seconds() / 60.0)
    except Exception:
        return None


def _old_rules(era, family, p):
    if era == OLD_ERA:
        return "A first-generation scalping rule set, retired when the lab moved to intraday swings."
    try:
        return describe(family, p) + " (As it ran in the swing era: one contract a trade, no tape condition.)"
    except Exception:
        return "A rule set from an earlier era."


def book(trade_limit=80, log_limit=120):
    if not os.path.exists(DB_PATH):
        return {"enabled": ENABLED, "started": False, "rules": RULES, "markets": [], "log": [], "trades": []}
    db_init()
    conn = db()
    try:
        now_utc = dt.datetime.now(dt.timezone.utc)
        last = json.loads(meta_get(conn, "last_tick", "{}") or "{}")
        fam = {r["id"]: (r["family"], r["era"] or OLD_ERA) for r in conn.execute("SELECT id,family,era FROM strategies")}
        markets = []
        for sym, m in MARKETS.items():
            rows = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE sym=? ORDER BY id", (sym,))]
            bar = conn.execute("SELECT ts,px FROM bars WHERE sym=? ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
            px = bar["px"] if bar else None
            unit = meta_get(conn, f"unit_{sym}", "")
            strategies, cur_closed, old_closed, open_now = [], [], [], []
            for s in rows:
                closed = _closed(conn, s["id"])
                era = s.get("era") or OLD_ERA
                (cur_closed if era == RULES else old_closed).extend(closed)
                op = conn.execute("SELECT * FROM trades WHERE strat=? AND status='open'", (s["id"],)).fetchone()
                p = json.loads(s["params"])
                opd = None
                if op:
                    op = dict(op)
                    way = 1 if op["side"] == "long" else -1
                    opd = {k: op.get(k) for k in ("side", "entry_px", "target_px", "stop_px", "entry_utc", "deadline_utc", "note", "mfe", "mae")}
                    opd["qty"], opd["risk_usd"] = op.get("qty") or 1, _risk(op)
                    opd["unrealized"] = round((px - op["entry_px"]) * way * m["mult"] * opd["qty"], 2) if px else None
                    opd["hold_min"] = _hold_min(op, now_utc)
                    open_now.append({"strat": s["id"], "family": s["family"], **opd})
                item = {"id": s["id"], "gen": s["gen"], "parent": s["parent"], "family": s["family"],
                        "family_label": FAMILY_LABEL.get(s["family"], s["family"]), "status": s["status"], "era": era,
                        "created_utc": s["created_utc"], "retired_utc": s["retired_utc"], "why": s["why"],
                        "retired_why": s["retired_why"],
                        "rules": describe(s["family"], p) if era == RULES else _old_rules(era, s["family"], p),
                        "params": p, "open": opd, "backtest": json.loads(s["backtest"]) if s.get("backtest") else None,
                        **stats(closed)}
                if s["status"] == "champion" and s["promoted_utc"]:
                    item["promoted_utc"] = s["promoted_utc"]
                    item["since_crowned"] = stats([t for t in closed if t["exit_utc"] > s["promoted_utc"]])
                    item["confirmed"] = _confirmed(item["since_crowned"])
                strategies.append(item)
            order = {"champion": 0, "active": 1, "retired": 2}
            strategies.sort(key=lambda x: (order.get(x["status"], 3), 0 if x["era"] == RULES else 1,
                                           -(x["exp_r"] if x["exp_r"] is not None else -1e9)))
            cur_closed.sort(key=lambda t: t["exit_utc"])
            cum, equity = 0.0, []
            for t in cur_closed[-400:]:
                cum += t["pnl"]
                equity.append({"t": t["exit_utc"], "v": round(cum, 2)})
            day0 = trading_day(now_utc.astimezone(ET))
            today = [t for t in cur_closed if trading_day(_parse_utc(t["exit_utc"]).astimezone(ET)) == day0]
            by_family = {}
            for t in cur_closed:
                by_family.setdefault(fam.get(t["strat"], ("?", ""))[0], []).append(t)
            cur_rows = [x for x in strategies if x["era"] == RULES]
            old_eras = sorted({x["era"] for x in strategies if x["era"] != RULES})
            champ = next((x for x in strategies if x["status"] == "champion"), None)
            markets.append({"sym": sym, "name": m["name"], "price": px,
                            "price_utc": dt.datetime.fromtimestamp(bar["ts"], dt.timezone.utc).isoformat() if bar else None,
                            "hour_move": float(unit) if unit else None, "point_value": m["mult"],
                            "regime": meta_get(conn, f"regime_{sym}", "") or None,
                            "champion": champ["id"] if champ else None, "champion_confirmed": bool(champ and champ.get("confirmed")),
                            "created": len(cur_rows), "running": sum(1 for x in cur_rows if x["status"] != "retired"),
                            "retired": sum(1 for x in cur_rows if x["status"] == "retired"),
                            "luck_bar": luck_bar(len(cur_rows)),
                            "all": stats(cur_closed), "today": stats(today),
                            "previous": {"era": ", ".join(old_eras) or OLD_ERA, "strategies": sum(1 for x in strategies if x["era"] != RULES), **stats(old_closed)},
                            "by_family": {FAMILY_LABEL.get(k, k): stats(v) for k, v in by_family.items()},
                            "equity": equity, "open": open_now, "strategies": strategies})
        log = [dict(r) for r in conn.execute("SELECT t,sym,kind,text FROM log ORDER BY id DESC LIMIT ?", (log_limit,))]
        trades = []
        for r in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (trade_limit,)):
            t = dict(r)
            f, era = fam.get(t["strat"], ("", OLD_ERA))
            risk = _risk(t)
            trades.append({**{k: t.get(k) for k in ("id", "strat", "sym", "side", "entry_utc", "entry_px", "target_px", "stop_px",
                                                    "init_stop_px", "exit_utc", "exit_px", "reason", "pnl", "status", "note", "mfe", "mae")},
                           "qty": t.get("qty") or 1, "risk_usd": round(risk, 2) if risk else None,
                           "r": round(t["pnl"] / risk, 2) if risk and t.get("pnl") is not None and t["status"] == "closed" else None,
                           "family": FAMILY_LABEL.get(f, f), "era": era, "hold_min": _hold_min(t, now_utc)})
        return {"enabled": ENABLED, "started": True, "rules": RULES, "last_tick": last, "min_eval": MIN_EVAL,
                "min_champion": MIN_CHAMPION, "population": POPULATION,
                "style": {"label": "Quantitative: intraday swings, sized by risk, judged by statistics",
                          "max_trades_day": MAX_TRADES_DAY,
                          "cooldown_min": COOLDOWN_MIN, "min_hold_cap_min": min(GRID["hold"]), "last_entry_et": "3:30 PM",
                          "flat_et": "4:55 PM", "unit": "the market's typical one hour move",
                          "risk_usd": RISK_USD, "max_contracts": MAX_QTY, "crown_t": CROWN_T,
                          "tape_test_days": BT_DAYS, "tape_test_candidates": BT_CANDIDATES},
                "costs": {"commission_rt": COMMISSION_RT, "slippage_ticks": SLIP_TICKS},
                "markets": markets, "log": log, "trades": trades}
    finally:
        conn.close()


def export():
    """The whole record for the vault: every strategy and every trade, with
    nothing that changes minute to minute (so an idle sync writes nothing)."""
    b = book(trade_limit=100000, log_limit=400)
    if not b.get("started"):
        return None
    markets = []
    for m in b["markets"]:
        markets.append({k: m[k] for k in ("sym", "name", "champion", "champion_confirmed", "created", "running", "retired",
                                           "luck_bar", "all", "previous", "by_family")}
                       | {"strategies": [{k: s.get(k) for k in ("id", "gen", "parent", "family", "family_label", "status", "era", "created_utc",
                                                                 "retired_utc", "why", "retired_why", "rules", "params", "backtest", "trades", "wins",
                                                                 "win_rate", "pnl", "expectancy", "profit_factor", "avg_hold_min",
                                                                 "exp_r", "total_r", "t_stat",
                                                                 "promoted_utc", "since_crowned", "confirmed")} for s in m["strategies"]]})
    trades = []
    for t in b["trades"]:
        live = t["status"] != "closed"
        trades.append({k: (None if live and k in ("mfe", "mae", "hold_min") else t.get(k)) for k in t})
    marks = [t["exit_utc"] or t["entry_utc"] for t in trades] + [l["t"] for l in b["log"]]
    return {"rules": b["rules"], "style": b["style"], "costs": b["costs"], "min_eval": b["min_eval"], "min_champion": b["min_champion"],
            "asof": max(marks) if marks else "", "markets": markets, "trades": trades, "log": b["log"]}


if __name__ == "__main__":
    print(json.dumps(book(), indent=1)[:4000])

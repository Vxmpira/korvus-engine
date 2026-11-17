"""The futures lab under the quantitative rules: the scalping and swing eras are
retired on the record, the quant bench still only takes swing-sized trades (at
most two a day each, never after 3:30 PM ET, never across the 5 PM maintenance
hour), every trade is sized by risk, new rule sets are replayed on stored tape
before they get a seat, and the replay prices a trade exactly like the live
tick does. No network.  Run: python tests/test_lab.py
"""
import os, sys, json, math, random, tempfile, builtins, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import korvus_lab as lab
lab.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="lab_test_"), "lab.db")
UTC = dt.timezone.utc
NEWS = []


def _quiet(fn):
    p = builtins.print
    builtins.print = lambda *a, **k: None
    try:
        return fn()
    finally:
        builtins.print = p


def _old_bench():
    """A database as the earlier eras left it: a first-generation scalper and a
    swing strategy, each with a trade open."""
    lab.db_init()
    c = lab.db()
    t = dt.datetime(2026, 11, 13, 15, 0, tzinfo=UTC).isoformat()
    c.execute("""INSERT INTO strategies (id,sym,gen,parent,family,params,status,created_utc,why,state,era)
                 VALUES ('MNQ-001','MNQ',1,NULL,'breakout',?, 'retired', ?, 'first', '{}', 'v1-scalp')""",
              (json.dumps({"target": 0.8, "stop": 0.5, "hold": 15, "session": "all", "lookback": 15}), t))
    c.execute("""INSERT INTO trades (strat,sym,side,entry_utc,entry_px,target_px,stop_px,deadline_utc,exit_utc,exit_px,reason,pnl,status,note)
                 VALUES ('MNQ-001','MNQ','long',?,28290,28300,28280,?,?,28295,'rules changed',8.6,'closed','old')""", (t, t, t))
    swing = {"target": 2.0, "stop": 1.0, "hold": 180, "session": "all", "protect": 1, "lookback": 120, "buffer": 0.2}
    c.execute("""INSERT INTO strategies (id,sym,gen,parent,family,params,status,created_utc,why,state,era)
                 VALUES ('MNQ-002','MNQ',1,NULL,'breakout',?, 'champion', ?, 'swing', '{}', 'v2-swing')""", (json.dumps(swing), t))
    c.execute("""INSERT INTO trades (strat,sym,side,entry_utc,entry_px,target_px,stop_px,deadline_utc,status,note,init_stop_px,mfe,mae)
                 VALUES ('MNQ-002','MNQ','long',?,28290,28390,28240,?, 'open','swing',28240,0,0)""", (t, t))
    c.execute("INSERT INTO meta (k,v) VALUES ('seq_MNQ','2')")
    c.execute("INSERT INTO meta (k,v) VALUES ('rules_MNQ','v2-swing')")
    c.commit(); c.close()


def test_dials_are_swing_sized():
    assert min(lab.GRID["hold"]) >= 90 and min(lab.GRID["lookback"]) >= 60
    assert {"momentum", "rel_value", "pullback", "breakout", "open_range", "news_follow", "news_fade"} == set(lab.FAMILY_DIALS)
    rng = random.Random(1)
    seen = set()
    for _ in range(400):
        fam, p = lab.random_params(rng)
        seen.add(fam)
        assert p["target"] >= p["stop"], (fam, p)
        assert p["regime"] in ("any", "calm", "fast")
        assert not (fam == "open_range" and p["session"] == "all")
        d = lab.describe(fam, p)
        assert "hour" in d and "flat before 5 PM" in d and "Sized to risk" in d
        q, _ = lab.mutate(rng, fam, p)
        assert q["target"] >= q["stop"] and not (fam == "open_range" and q["session"] == "all")
    assert seen == set(lab.FAMILY_DIALS)


def test_sessions():
    et = lambda d, h, m=0: dt.datetime(2026, 11, d, h, m, tzinfo=lab.ET)      # Nov 2026: 16th is a Monday
    assert lab.can_enter(et(16, 10)) and lab.can_enter(et(16, 15, 29)) and not lab.can_enter(et(16, 15, 30))
    assert not lab.can_enter(et(16, 17, 30)) and lab.can_enter(et(16, 18, 0))
    assert lab.must_flat(et(16, 16, 55)) and not lab.must_flat(et(16, 16, 54))
    assert lab.trading_day(et(15, 19)) == lab.trading_day(et(16, 10)) == "2026-11-16"
    assert lab.session_start(et(16, 1), "asia") == et(15, 18) and lab.session_start(et(16, 11), "ny") == et(16, 9, 30)


def test_sizing_and_scores():
    m = lab.MARKETS["MES"]
    p = {"target": 2.0, "stop": 1.0}
    fill, tgt, stp, qty, risk = lab.plan_entry("long", 6900.0, p, 10.0, m)          # $50 a contract at the stop
    assert (fill, tgt, stp) == (6900.25, 6920.25, 6890.25) and qty == 3 and risk == 150.0
    assert lab.plan_entry("short", 6900.0, p, 2.0, m)[3] == lab.MAX_QTY             # tight stop: capped
    f2 = lab.plan_entry("long", 28300.0, p, 120.0, lab.MARKETS["MNQ"])
    assert f2[3] == 1 and f2[4] == 240.0                                           # wide stop: one contract, real risk on file
    t = {"side": "long", "entry_px": fill, "qty": qty}
    assert lab.exit_fill(t, 6910.25, m) == (6910.0, round(9.75 * 5 * 3 - 1.4 * 3, 2))
    mk = lambda r: {"pnl": r * 100.0, "risk_usd": 100.0, "entry_utc": "2026-11-16T15:00:00+00:00", "exit_utc": "2026-11-16T17:00:00+00:00"}
    s = lab.stats([mk(r) for r in (1.0, -1.0, 2.0, 1.0, -0.5, 1.5)])
    rs = [1.0, -1.0, 2.0, 1.0, -0.5, 1.5]
    mean = sum(rs) / 6
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / 5)
    assert s["exp_r"] == round(mean, 3) and abs(s["t_stat"] - mean / (sd / math.sqrt(6))) < 0.01 and s["total_r"] == 4.0
    assert lab.stats([mk(1.0)])["t_stat"] is None and lab.stats([])["exp_r"] is None
    old = {"pnl": 30.0, "sym": "MNQ", "entry_px": 28300.0, "init_stop_px": 28285.0, "entry_utc": "x", "exit_utc": "x"}
    assert lab._risk(old) == 30.0                                                  # a one-contract trade from before sizing
    assert 2.5 < lab.luck_bar(30) < 2.7 and lab.luck_bar(1) == 0.0


def test_tape_numbers():
    rng = random.Random(3)
    bars, peer, a, b = [], {}, 28000.0, 6800.0
    for k in range(700):
        g = rng.gauss(0, 1)
        a += 4 * (0.9 * g + 0.4 * rng.gauss(0, 1))
        b += 0.97 * (0.9 * g + 0.4 * rng.gauss(0, 1))
        bars.append((1_795_000_000 + 60 * k, round(a * 4) / 4))
        peer[bars[-1][0]] = round(b * 4) / 4
    tp = lab.Tape(bars, 0.25, peer)
    i = len(tp) - 1
    assert 20 < tp.unit(i) < 50 and tp.unit(10) is None and tp.regime(i) in ("calm", "normal", "fast")
    z = tp.gap_z(i, 240)
    assert z is not None and abs(z) < 6
    bars2 = bars[:-1] + [(bars[-1][0], bars[-1][1] + 60.0)]                         # this market jumps, its sister does not
    z2 = lab.Tape(bars2, 0.25, peer).gap_z(i, 240)
    assert z2 > z + 2 and lab.signal("rel_value", {"lookback": 240, "rv_z": 2.0}, {}, lab.Tape(bars2, 0.25, peer), i, 30.0, [], None)[0] == "short"
    assert lab.Tape(bars, 0.25).gap_z(i, 240) is None                               # no sister tape, no signal
    up = bars[:-1] + [(bars[-1][0], bars[-61][1] + 100.0)]
    assert lab.signal("momentum", {"lookback": 60, "mz": 2.0}, {}, lab.Tape(up, 0.25), i, 30.0, [], None)[0] == "long"
    flat = [(t, 100.0) for t, _ in bars]
    assert not lab.Tape(flat, 0.25).live(i) and lab.Tape(bars, 0.25).live(i)


def test_two_weeks_of_quant():
    _old_bench()
    rng = random.Random(11)
    px = {"MNQ": 28300.0, "MES": 6900.0}
    news, n = NEWS, 0
    t = dt.datetime(2026, 11, 15, 23, 0, tzinfo=UTC)                          # Sunday 6 PM ET
    end = t + dt.timedelta(days=9)
    def run():
        nonlocal t, n
        while t < end:
            g = rng.gauss(0, 1)                                               # the two indexes share most of every move
            for s in px:
                drift = 0.0
                for it in news[-3:]:
                    age = (t - dt.datetime.fromisoformat(it["created_at"])).total_seconds() / 60
                    if 0 <= age < 40:
                        drift += (1 if it["direction"] == "bull" else -1) * (0.9 if s == "MNQ" else 0.25)
                step = (0.85 * g + 0.53 * rng.gauss(0, 1)) * (4.0 if s == "MNQ" else 1.0)
                px[s] = round((px[s] + drift + step) * 4) / 4
            if rng.random() < 0.004:
                news.append({"id": f"n{n}", "created_at": t.isoformat(), "headline": "Fed speaker surprises on rates",
                             "impact": rng.choice(["high", "med"]), "direction": rng.choice(["bull", "bear"]),
                             "instruments": json.dumps(["MNQ", "MES"]), "confidence": rng.choice([55, 65, 75, 85])})
                n += 1
            q = {"MNQ": {"price": px["MNQ"]}, "MES": {"price": px["MES"]}, "_meta": {"provider": "databento"}}
            lab.tick(t, lambda syms: q, lambda since: [x for x in news if dt.datetime.fromisoformat(x["created_at"]) >= since])
            t += dt.timedelta(minutes=1)
    _quiet(run)

    c = lab.db()
    old = dict(c.execute("SELECT * FROM strategies WHERE id='MNQ-001'").fetchone())
    assert old["status"] == "retired" and old["era"] == lab.OLD_ERA
    sw = dict(c.execute("SELECT * FROM strategies WHERE id='MNQ-002'").fetchone())
    assert sw["status"] == "retired" and "quantitative" in sw["retired_why"] and sw["era"] == lab.SWING_ERA and sw["promoted_utc"] is None
    ot = dict(c.execute("SELECT * FROM trades WHERE strat='MNQ-002'").fetchone())
    assert ot["status"] == "closed" and ot["reason"] == "rules changed"
    new = [dict(r) for r in c.execute("SELECT * FROM strategies WHERE era=?", (lab.RULES,))]
    gen1 = [s for s in new if s["sym"] == "MNQ" and s["gen"] == 1]
    assert len(gen1) >= lab.POPULATION and all(s["id"] not in ("MNQ-001", "MNQ-002") for s in new)
    heir = [s for s in new if "carries the rules of MNQ-002" in s["why"]]
    assert len(heir) == 1 and heir[0]["family"] == "breakout" and json.loads(heir[0]["params"])["regime"] == "any"
    assert {s["family"] for s in new if s["sym"] == "MES" and s["gen"] == 1 and "First generation" in s["why"]} == set(lab.FAMILY_DIALS)
    tested = [s for s in new if s["backtest"]]
    assert tested and all(json.loads(s["backtest"])["days"] >= 0.6 and "replayed on" in s["why"] for s in tested)
    trades = [dict(r) for r in c.execute("SELECT t.* FROM trades t JOIN strategies s ON s.id=t.strat WHERE s.era=? AND t.status='closed'", (lab.RULES,))]
    assert len(trades) > 60, len(trades)
    per_day, holds = {}, []
    for tr in trades:
        e, x = lab._parse_utc(tr["entry_utc"]).astimezone(lab.ET), lab._parse_utc(tr["exit_utc"]).astimezone(lab.ET)
        assert lab.can_enter(e), ("entered outside the swing window", tr)
        assert lab.trading_day(e) == lab.trading_day(x), ("held across the maintenance hour", tr)
        assert not ((17, 0) <= (x.hour, x.minute) < (18, 0)), tr
        k = (tr["strat"], lab.trading_day(e))
        per_day[k] = per_day.get(k, 0) + 1
        if tr["reason"] != "strategy retired":
            holds.append((x - e).total_seconds() / 60)
        way = 1 if tr["side"] == "long" else -1
        assert (tr["target_px"] - tr["entry_px"]) * way >= (tr["entry_px"] - tr["init_stop_px"]) * way > 0, tr
        per = abs(tr["entry_px"] - tr["init_stop_px"]) * lab.MARKETS[tr["sym"]]["mult"]
        assert 1 <= tr["qty"] <= lab.MAX_QTY and abs(tr["risk_usd"] - per * tr["qty"]) < 0.02, tr
        assert tr["qty"] == 1 or tr["risk_usd"] <= lab.RISK_USD + 0.01, ("sized past the risk budget", tr)
        assert tr["qty"] == lab.MAX_QTY or per * (tr["qty"] + 1) > lab.RISK_USD, ("left risk budget unused", tr)
        if tr["reason"] == "gap closed":
            assert (x - e).total_seconds() >= lab.GAP_EXIT_MIN * 60, tr
    assert max(per_day.values()) <= lab.MAX_TRADES_DAY, max(per_day.values())
    assert any(tr["qty"] > 1 for tr in trades) and any(tr["qty"] == 1 for tr in trades)
    holds.sort()
    assert holds[len(holds) // 2] >= 45, ("median hold is scalp-sized", holds[len(holds) // 2])
    assert any(tr["reason"] == "stop at entry" for tr in trades), "the protect dial never fired"
    assert {tr["reason"] for tr in trades} >= {"target", "stop", "gap closed"}, {tr["reason"] for tr in trades}

    b = lab.book()
    m = next(x for x in b["markets"] if x["sym"] == "MNQ")
    assert b["rules"] == lab.RULES and m["previous"]["strategies"] == 2 and m["previous"]["trades"] == 2
    assert m["previous"]["era"] == "v1-scalp, v2-swing" and "Quantitative" in b["style"]["label"] and b["style"]["risk_usd"] == lab.RISK_USD
    assert m["all"]["trades"] > 0 and m["all"]["avg_hold_min"] and m["equity"] and m["hour_move"] and m["all"]["exp_r"] is not None
    assert m["regime"] in ("calm", "normal", "fast") and m["luck_bar"] > 2
    assert m["created"] >= lab.POPULATION and m["retired"] >= 1                # the daily review turned the bench over
    assert all(s["rules"] for s in m["strategies"]) and b["trades"][0]["family"] and b["trades"][0]["qty"] >= 1
    assert any(t["r"] is not None for t in b["trades"])
    assert "swing era" in next(s for s in m["strategies"] if s["id"] == "MNQ-002")["rules"]
    log = lab.book(log_limit=100000)["log"]
    assert any(l["kind"] == "rules" and "quantitative" in l["text"] for l in log)
    assert any(l["kind"] == "created" and "Bred from" in l["text"] and "replayed on" in l["text"] for l in log)
    fams = {s["family"] for mk in b["markets"] for s in mk["strategies"] if s["era"] == lab.RULES and s["trades"]}
    assert len(fams) >= 6 and {"momentum", "rel_value"} <= fams, fams
    c.close()
    print(f"   {len(trades)} quant trades, median hold {holds[len(holds) // 2]:.0f} min, families trading: {sorted(fams)}")


def test_replay_matches_live():
    """The tape test must price a strategy exactly as the live tick did. Replay
    every first-generation strategy from the minute it was created and compare
    with the trades it really took."""
    c = lab.db()
    checked = n_trades = 0
    for sym in lab.MARKETS:
        bars = {s: [(r["ts"], r["px"]) for r in c.execute("SELECT ts,px FROM bars WHERE sym=? ORDER BY ts", (s,))] for s in lab.MARKETS}
        tape = lab.Tape(bars[sym], lab.MARKETS[sym]["tick"], dict(bars[lab.PEER[sym]]))
        for s in [dict(r) for r in c.execute("SELECT * FROM strategies WHERE sym=? AND era=? AND gen=1 AND why LIKE 'First generation%'", (sym, lab.RULES))]:
            real = [dict(r) for r in c.execute("SELECT * FROM trades WHERE strat=? AND status='closed' ORDER BY entry_utc", (s["id"],))]
            sim = lab.simulate(sym, s["family"], json.loads(s["params"]), tape, NEWS, lab._parse_utc(s["created_utc"]).timestamp())
            assert len(sim) >= len(real), (s["id"], len(sim), len(real))
            for a, b in zip(real, sim):
                assert (a["entry_utc"], a["side"], a["entry_px"], a["target_px"], a["qty"], a["risk_usd"]) == \
                       (b["entry_utc"], b["side"], b["entry_px"], b["target_px"], b["qty"], b["risk_usd"]), (s["id"], a, b)
                if a["reason"] != "strategy retired":
                    assert (a["exit_utc"], a["reason"], a["pnl"], a["mfe"], a["mae"]) == (b["exit_utc"], b["reason"], b["pnl"], b["mfe"], b["mae"]), (s["id"], a, b)
            checked += 1
            n_trades += len(real)
    c.close()
    assert checked >= 2 * lab.POPULATION and n_trades > 40
    print(f"   {n_trades} live trades from {checked} strategies reproduced exactly by the replay")


def test_champion_needs_statistics():
    """A steady winner earns the crown and then confirmation; a lucky streak with one big win does not."""
    c = lab.db()
    now = dt.datetime(2026, 12, 7, 22, 30, tzinfo=UTC)
    def put(sid, rs, start):
        c.execute("""INSERT INTO strategies (id,sym,gen,parent,family,params,status,created_utc,why,state,era)
                     VALUES (?,?,1,NULL,'momentum',?, 'active', ?, 'test', '{}', ?)""",
                  (sid, "MES", json.dumps({"target": 2.0, "stop": 1.0, "hold": 120, "session": "all", "protect": 0, "regime": "any",
                                           "lookback": 60 if sid == "MES-901" else 120, "mz": 1.5}), start.isoformat(), lab.RULES))
        for k, r in enumerate(rs):
            e = start + dt.timedelta(hours=3 * k + 1)
            c.execute("""INSERT INTO trades (strat,sym,side,entry_utc,entry_px,target_px,stop_px,deadline_utc,exit_utc,exit_px,reason,pnl,status,note,init_stop_px,qty,risk_usd)
                         VALUES (?,?,'long',?,6900,6920,6890,?,?,6900,'target',?, 'closed','t',6890,3,150)""",
                      (sid, "MES", e.isoformat(), e.isoformat(), (e + dt.timedelta(hours=2)).isoformat(), r * 150.0))
    steady = [1.5, -1, 2, 1.2, -1, 2, 1.5, -0.5, 2, -1, 1.8, 2, -1, 1.5, 2, -1, 2, 1.2, 1.5, -1, 2, 1.6]
    streak = [-1] * 18 + [-0.9, -0.8, 28.0]                                    # positive on average only because of one trade
    put("MES-901", steady, now - dt.timedelta(days=6))
    put("MES-902", streak, now - dt.timedelta(days=6))
    c.commit()
    _quiet(lambda: lab.review(c, "MES", now))
    c.commit()
    st = {r["id"]: r["status"] for r in c.execute("SELECT id,status FROM strategies WHERE id IN ('MES-901','MES-902')")}
    s2 = lab.stats(lab._closed(c, "MES-902"))
    assert s2["exp_r"] > 0 and s2["t_stat"] < lab.CROWN_T
    assert st == {"MES-901": "champion", "MES-902": "active"}, st
    assert any("lead, not proof" in r["text"] for r in c.execute("SELECT text FROM log WHERE kind='champion'"))
    b = next(x for x in lab.book()["markets"] if x["sym"] == "MES")
    assert b["champion"] == "MES-901" and not b["champion_confirmed"]
    crowned = lab._parse_utc(c.execute("SELECT promoted_utc FROM strategies WHERE id='MES-901'").fetchone()[0])
    for k, r in enumerate(steady):                                             # it keeps working after the crown
        e = crowned + dt.timedelta(hours=3 * k + 1)
        c.execute("""INSERT INTO trades (strat,sym,side,entry_utc,entry_px,target_px,stop_px,deadline_utc,exit_utc,exit_px,reason,pnl,status,note,init_stop_px,qty,risk_usd)
                     VALUES ('MES-901','MES','long',?,6900,6920,6890,?,?,6900,'target',?, 'closed','t',6890,3,150)""",
                  (e.isoformat(), e.isoformat(), (e + dt.timedelta(hours=2)).isoformat(), r * 150.0))
    c.commit()
    _quiet(lambda: lab.review(c, "MES", now + dt.timedelta(days=4)))
    c.commit()
    assert any("CONFIRMED" in r["text"] for r in c.execute("SELECT text FROM log WHERE kind='champion'"))
    c.close()
    b = next(x for x in lab.book()["markets"] if x["sym"] == "MES")
    assert b["champion_confirmed"] and next(s for s in b["strategies"] if s["id"] == "MES-901")["confirmed"]


def test_vault_notes():
    """The lab's whole record lands in the vault as notes the owner can read."""
    import korvus_intel as intel
    intel.DB_PATH = os.path.join(os.path.dirname(lab.DB_PATH), "intel.db")
    intel.db_init()
    import korvus_vault as vault
    files = _quiet(lambda: vault.render(dt.datetime(2026, 12, 12, 17, 0, tzinfo=UTC)))
    for k in ("Lab/Strategies.md", "Lab/Log.md", "data/lab.json", "Lab/Trades/2026-11.md"):
        assert k in files, k
    md = files["Lab/Strategies.md"]
    assert "Quantitative: intraday swings, sized by risk" in md and "### MNQ-" in md
    assert "Earlier eras (v1-scalp, v2-swing): 2 strategies, 2 trades" in md and "| Avg R | t-score |" in md
    assert "**Tape test:**" in md and "CONFIRMED" in md
    assert "| Entry (ET) | Market | Strategy |" in files["Lab/Trades/2026-11.md"] and "| Qty |" in files["Lab/Trades/2026-11.md"]
    j = json.loads(files["data/lab.json"])
    assert j["rules"] == lab.RULES and len(j["trades"]) > 60 and "last_tick" not in j
    assert files == _quiet(lambda: vault.render(dt.datetime(2026, 12, 12, 17, 5, tzinfo=UTC)))     # idle sync: nothing to commit
    assert chr(0x2014) not in "".join(files.values()) and chr(0x2013) not in "".join(files.values())


def test_idle_on_the_proxy_feed():
    r = _quiet(lambda: lab.tick(dt.datetime(2026, 12, 1, 15, 0, tzinfo=UTC),
                                lambda s: {"MNQ": {"price": 1}, "_meta": {"provider": "finnhub"}}, lambda s: []))
    assert r["feed_ok"] is False and r["entries"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok ", name)
    print("ALL LAB TESTS PASSED")

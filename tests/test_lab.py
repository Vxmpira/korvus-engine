"""The futures lab under the intraday swing ruling: the first-generation
scalpers are retired on the record, the new bench only takes swing-sized
trades, at most two a day each, never after 3:30 PM ET and never across the
5 PM maintenance hour. No network.  Run: python tests/test_lab.py
"""
import os, sys, json, random, tempfile, builtins, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import korvus_lab as lab
lab.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="lab_test_"), "lab.db")
UTC = dt.timezone.utc


def _quiet(fn):
    p = builtins.print
    builtins.print = lambda *a, **k: None
    try:
        return fn()
    finally:
        builtins.print = p


def _old_bench():
    """A database as the first version left it: a scalper with a trade open."""
    lab.db_init()
    c = lab.db()
    t = dt.datetime(2026, 11, 13, 15, 0, tzinfo=UTC).isoformat()
    c.execute("""INSERT INTO strategies (id,sym,gen,parent,family,params,status,created_utc,why,state)
                 VALUES ('MNQ-001','MNQ',1,NULL,'breakout',?, 'champion', ?, 'first', '{}')""",
              (json.dumps({"target": 0.8, "stop": 0.5, "hold": 15, "session": "all", "lookback": 15}), t))
    c.execute("""INSERT INTO trades (strat,sym,side,entry_utc,entry_px,target_px,stop_px,deadline_utc,status,note)
                 VALUES ('MNQ-001','MNQ','long',?,28290,28300,28280,?, 'open','old')""", (t, t))
    c.execute("INSERT INTO meta (k,v) VALUES ('seq_MNQ','1')")
    c.commit(); c.close()


def test_dials_are_swing_sized():
    assert min(lab.GRID["hold"]) >= 90 and min(lab.GRID["lookback"]) >= 60
    rng = random.Random(1)
    for _ in range(300):
        fam, p = lab.random_params(rng)
        assert p["target"] >= p["stop"], (fam, p)
        assert not (fam == "open_range" and p["session"] == "all")
        assert "hour" in lab.describe(fam, p) and "flat before 5 PM" in lab.describe(fam, p)
        q, _ = lab.mutate(rng, fam, p)
        assert q["target"] >= q["stop"] and not (fam == "open_range" and q["session"] == "all")


def test_sessions():
    et = lambda d, h, m=0: dt.datetime(2026, 11, d, h, m, tzinfo=lab.ET)      # Nov 2026: 16th is a Monday
    assert lab.can_enter(et(16, 10)) and lab.can_enter(et(16, 15, 29)) and not lab.can_enter(et(16, 15, 30))
    assert not lab.can_enter(et(16, 17, 30)) and lab.can_enter(et(16, 18, 0))
    assert lab.must_flat(et(16, 16, 55)) and not lab.must_flat(et(16, 16, 54))
    assert lab.trading_day(et(15, 19)) == lab.trading_day(et(16, 10)) == "2026-11-16"
    assert lab.session_start(et(16, 1), "asia") == et(15, 18) and lab.session_start(et(16, 11), "ny") == et(16, 9, 30)


def test_two_weeks_of_swings():
    _old_bench()
    rng = random.Random(11)
    px = {"MNQ": 28300.0, "MES": 6900.0}
    news, n = [], 0
    t = dt.datetime(2026, 11, 15, 23, 0, tzinfo=UTC)                          # Sunday 6 PM ET
    end = t + dt.timedelta(days=9)
    def run():
        nonlocal t, n
        while t < end:
            for s in px:
                drift = 0.0
                for it in news[-3:]:
                    age = (t - dt.datetime.fromisoformat(it["created_at"])).total_seconds() / 60
                    if 0 <= age < 40:
                        drift += (1 if it["direction"] == "bull" else -1) * (0.9 if s == "MNQ" else 0.25)
                px[s] = round((px[s] + rng.gauss(drift, 4.0 if s == "MNQ" else 1.0)) * 4) / 4
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
    assert old["status"] == "retired" and "no scalping" in old["retired_why"] and old["era"] == lab.OLD_ERA
    ot = dict(c.execute("SELECT * FROM trades WHERE strat='MNQ-001'").fetchone())
    assert ot["status"] == "closed" and ot["reason"] == "rules changed"
    new = [dict(r) for r in c.execute("SELECT * FROM strategies WHERE era=?", (lab.RULES,))]
    assert len([s for s in new if s["sym"] == "MNQ" and s["gen"] == 1]) >= lab.POPULATION and new[0]["id"] != "MNQ-001"
    trades = [dict(r) for r in c.execute("SELECT t.* FROM trades t JOIN strategies s ON s.id=t.strat WHERE s.era=? AND t.status='closed'", (lab.RULES,))]
    c.close()
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
    assert max(per_day.values()) <= lab.MAX_TRADES_DAY, max(per_day.values())
    holds.sort()
    assert holds[len(holds) // 2] >= 45, ("median hold is scalp-sized", holds[len(holds) // 2])
    assert any(tr["reason"] == "stop at entry" for tr in trades), "the protect dial never fired"
    assert {tr["reason"] for tr in trades} >= {"target", "stop"}

    b = lab.book()
    m = next(x for x in b["markets"] if x["sym"] == "MNQ")
    assert b["rules"] == lab.RULES and m["previous"]["strategies"] == 1 and m["previous"]["trades"] == 1
    assert m["all"]["trades"] > 0 and m["all"]["avg_hold_min"] and m["equity"] and m["hour_move"]
    assert m["created"] >= lab.POPULATION and m["retired"] >= 1                # the daily review turned the bench over
    assert all(s["rules"] for s in m["strategies"]) and b["trades"][0]["family"]
    assert any(l["kind"] == "rules" for l in lab.book(log_limit=100000)["log"])
    fams = {s["family"] for mk in b["markets"] for s in mk["strategies"] if s["era"] == lab.RULES and s["trades"]}
    assert len(fams) >= 4, fams
    print(f"   {len(trades)} swing trades, median hold {holds[len(holds) // 2]:.0f} min, families trading: {sorted(fams)}")


def test_vault_notes():
    """The lab's whole record lands in the vault as notes the owner can read."""
    import korvus_intel as intel
    intel.DB_PATH = os.path.join(os.path.dirname(lab.DB_PATH), "intel.db")
    intel.db_init()
    import korvus_vault as vault
    files = _quiet(lambda: vault.render(dt.datetime(2026, 11, 25, 17, 0, tzinfo=UTC)))
    for k in ("Lab/Strategies.md", "Lab/Log.md", "data/lab.json", "Lab/Trades/2026-11.md"):
        assert k in files, k
    md = files["Lab/Strategies.md"]
    assert "Intraday swings only, no scalping" in md and "### MNQ-" in md and "Before the swing ruling (v1-scalp): 1 strategies, 1 trades" in md
    assert "| Entry (ET) | Market | Strategy |" in files["Lab/Trades/2026-11.md"]
    j = json.loads(files["data/lab.json"])
    assert j["rules"] == lab.RULES and len(j["trades"]) > 60 and "last_tick" not in j
    assert files == _quiet(lambda: vault.render(dt.datetime(2026, 11, 25, 17, 5, tzinfo=UTC)))     # idle sync: nothing to commit
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

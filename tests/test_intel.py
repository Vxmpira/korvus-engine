"""Offline end-to-end test for Korvus Intel: universe parse, filing triggers,
paper-bot entries/exits on a simulated day, scoreboard, vault render, and a
real git sync against a local bare repo shaped like blackcrown-brain (with a
concurrent 'agent' commit to prove the rebase path). No network, no API keys.
Run: python tests/test_intel.py
"""
import os, sys, json, shutil, tempfile, subprocess, datetime as dt
from zoneinfo import ZoneInfo
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="intel_test_")
import korvus_intel as intel
intel.DB_PATH = os.path.join(TMP, "intel.db")
import korvus_paperbot as bot
import korvus_vault as vault
bot.EXTENDED = False               # this file tests the regular session; extended hours are in test_paperbot_sessions
ET = ZoneInfo("America/New_York")


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True, text=True).stdout


def test_universe_parse():
    rows = [
        {"symbol": "AAA", "companyName": "Alpha", "price": 99.5, "marketCap": 3e9, "volume": 2e6, "sector": "Tech"},
        {"symbol": "ETFX", "price": 100, "isEtf": True},
        {"symbol": "LOW", "price": 40},
        {"symbol": "BBB", "name": "Beta", "price": 81, "marketCap": 1e9, "volume": 9e6, "exchange": "NYSE"},
        {"symbol": "C.WS", "price": 100},
    ]
    u = intel.parse_screener(rows)
    assert [x["symbol"] for x in u] == ["BBB", "AAA"], u     # band filter, no ETF/warrant, sorted by volume


def seed():
    intel.db_init()
    c = intel.db()
    now = dt.datetime(2026, 11, 10, 8, 0, tzinfo=ET).astimezone(dt.timezone.utc).isoformat()
    for s, p in (("AAA", 99.5), ("BBB", 81.0), ("CCC", 110.0)):
        c.execute("INSERT INTO universe (symbol,name,cik,price,mcap,volume,sector,exchange,asof) VALUES (?,?,?,?,?,?,?,?,?)",
                  (s, s + " Inc", 1000 + len(s), p, 2e9, 1e6, "Tech", "NASDAQ", now))
    rows = [  # accession, sym, impact, dir, conf, ref
        ("0001-26-000001", "AAA", "high", "bull", 80, 99.0),   # trigger, confirms
        ("0001-26-000002", "BBB", "med",  "bull", 75, 82.0),   # trigger, but below reference -> skip
        ("0001-26-000003", "CCC", "high", "bear", 90, 110.0),  # bearish -> never long
        ("0001-26-000004", "AAA", "low",  "bull", 95, 99.0),   # low impact -> no
    ]
    for a, s, imp, d, cf, ref in rows:
        c.execute("""INSERT INTO filings (accession,symbol,cik,company,form,items,filed_utc,seen_utc,url,headline,
                     ref_price,prev_close,summary,read,catalyst,impact,direction,confidence,scored)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                  (a, s, 1, s + " Inc", "8-K", '["2.02"]', now, now, "https://www.sec.gov/x", f"{s}: Results",
                   ref, ref, "Revenue up 20%.", "Read text.", "earnings", imp, d, cf))
    c.commit(); c.close()


def test_filters():
    assert bot.is_trigger({"direction": "bull", "impact": "med", "confidence": 72})
    assert not bot.is_trigger({"direction": "bull", "impact": "med", "confidence": 62})     # the v3 log's weak reads
    assert bot.is_trigger({"direction": "bull", "impact": "high", "confidence": 65})
    assert not bot.is_trigger({"direction": "bull", "impact": "low", "confidence": 99})
    assert bot.grade({"impact": "high", "confidence": 70}, "scalp") and not bot.grade({"impact": "med", "confidence": 99}, "scalp")
    assert bot.side_for({"direction": "bear", "impact": "high", "confidence": 99}) == "short"
    assert bot.side_for({"direction": "neut", "impact": "high", "confidence": 99}) is None
    C = bot.confirm_entry
    assert C({"ref_price": 99}, {"price": 100, "chg_pct": 2})[0]
    assert not C({"ref_price": 99}, {"price": 99.4, "chg_pct": 2})[0]                 # +0.4%: one tick above is not agreement
    assert C({"ref_price": 99}, {"price": 99.4, "chg_pct": 2}, "long", "scalp")[0]     # a scalp needs +0.3%
    assert not C({"ref_price": 99}, {"price": 98, "chg_pct": 2})[0]
    assert not C({"ref_price": 90}, {"price": 100, "chg_pct": 9})[0]                   # already a chase
    assert not C({"ref_price": 99}, {"price": 100, "chg_pct": 2, "open": 101})[0]      # below its own open: no swing
    assert C({"ref_price": 99}, {"price": 100, "chg_pct": 2, "open": 99.5})[0]
    assert not C({}, {"price": 100, "chg_pct": 2})[0]                                  # no reference price, no trade
    assert C({"ref_price": 100}, {"price": 99, "chg_pct": -2}, "short")[0]
    assert not C({"ref_price": 100}, {"price": 101, "chg_pct": -2}, "short")[0]
    assert not C({"ref_price": 100}, {"price": 90, "chg_pct": -9}, "short")[0]
    t, st = bot.levels("short", 100.0)
    assert round(t, 2) == 97.0 and round(st, 2) == 101.5                               # 1.5% stop, 2x target
    # the stop widens with the stock's own range, the size shrinks so the dollars at risk stay the same
    quiet = bot.plan("swing", "long", 100.0, {"price": 100}, "regular")
    wild = bot.plan("swing", "long", 100.0, {"price": 100, "high": 103, "low": 97}, "regular")
    assert quiet["stop_pct"] == 1.5 and quiet["qty"] == 66 and wild["stop_pct"] == 3.0 and wild["qty"] == 33
    assert abs(quiet["risk_usd"] - 99.0) < 1e-6 and abs(wild["risk_usd"] - 99.0) < 1e-6
    assert bot.plan("swing", "long", 100.0, {"price": 100, "high": 110, "low": 90}, "regular")["stop_pct"] == 3.5
    ext = bot.plan("scalp", "long", 100.0, {"price": 100}, "afterhours")
    assert ext["stop_pct"] == 1.8 and ext["qty"] == 27 and round(ext["target_pct"], 2) == 2.7   # wider stop, half risk
    assert bot.plan("scalp", "long", 9000.0, {"price": 9000}, "regular")["qty"] == 0
    f = {"impact": "high", "direction": "bull", "confidence": 80, "catalyst": "dividend", "seen_utc": "2026-11-10T13:00:00+00:00"}
    assert bot.playbook_for(f, dt.datetime(2026, 11, 10, 10, 30, tzinfo=ET), "regular")[0] is None       # routine catalyst
    f["catalyst"] = "earnings"
    assert bot.playbook_for(f, dt.datetime(2026, 11, 10, 8, 10, tzinfo=ET), "premarket") == (None, "waiting for the day swing window (10:00 AM to 2:30 PM ET)")
    assert bot.playbook_for(f, dt.datetime(2026, 11, 10, 10, 30, tzinfo=ET), "regular")[0] == "swing"


def _ticks(day, h, m, q, n=3):
    """n checks one minute apart; returns the last result and every skip seen."""
    r, skipped = None, []
    for i in range(n):
        r = bot.tick(day.replace(hour=h, minute=m + i), q)
        skipped += r["skipped"]
    return r, skipped


def test_simulated_day():
    seed()
    px = {"AAA": 100.0, "BBB": 80.0, "CCC": 111.0}
    q = lambda s: {"price": px[s], "chg_pct": 1.0, "prev_close": px[s] - 1, "open": 0, "high": 0, "low": 0}
    day = dt.datetime(2026, 11, 10, tzinfo=ET)
    r = bot.tick(day.replace(hour=9, minute=45), q)            # the open is still settling: swings wait for 10:00
    assert r["entries"] == 0 and r["watch"][0]["state"] == "waiting", r["watch"]
    r = bot.tick(day.replace(hour=10, minute=0), q)
    assert r["entries"] == 0 and ("AAA", "confirming: +1.01% since the filing, held on 1 of 3 checks") in r["skipped"], r["skipped"]
    r = bot.tick(day.replace(hour=10, minute=1), q)
    assert r["entries"] == 0
    r = bot.tick(day.replace(hour=10, minute=2), q)            # held three checks in a row: the swing is on
    assert r["entries"] == 1, r
    assert ("BBB", "-2.44% since the filing, needs +0.5% the read's way") in r["skipped"], r["skipped"]
    assert ("CCC", "-0.91% since the filing, needs +0.5% the read's way") in r["skipped"], r["skipped"]   # bear read, price not confirming
    c = intel.db(); t = dict(c.execute("SELECT * FROM trades WHERE symbol='AAA'").fetchone()); c.close()
    assert t["playbook"] == "swing" and t["strategy"] == "v4-scalp-and-swing" and t["qty"] == 66 and t["phase"] == "regular"
    assert abs(t["stop_px"] - 100.05 * 0.985) < 1e-3 and abs(t["target_px"] - 100.05 * 1.03) < 1e-3 and abs(t["risk_usd"] - 99.05) < 0.01
    r = bot.tick(day.replace(hour=10, minute=5), q)
    assert r["entries"] == 0                                     # one trade per filing per playbook
    px["AAA"] = 101.6                                            # up one stop distance: stop goes to the entry price
    r = bot.tick(day.replace(hour=10, minute=30), q)
    assert r["exits"] == 0 and any(k == "manage" for k, _, _ in r["log"]), r["log"]
    px["AAA"] = 103.2                                            # +3% target
    r = bot.tick(day.replace(hour=11, minute=0), q)
    assert r["exits"] == 1
    sb = bot.scoreboard()
    assert sb["closed_trades"] == 1 and sb["wins"] == 1 and sb["win_rate_pct"] == 100.0 and not sb["sample_ok"]
    assert sb["exits"] == {"target": 1} and sb["by_playbook"]["swing"]["avg_r"] > 2 and sb["avg_hold_min"] == 58
    assert sb["avg_mfe_pct"] > 3 and sb["expectancy_r"] > 2
    # a swing that worked and then came all the way back leaves at the entry price, not at a loss
    c = intel.db()
    c.execute("UPDATE filings SET direction='bull', ref_price=110 WHERE symbol='CCC'"); c.commit(); c.close()
    r, _ = _ticks(day, 12, 0, q)
    assert r["entries"] == 1
    px["CCC"] = 112.8
    assert bot.tick(day.replace(hour=12, minute=30), q)["exits"] == 0
    px["CCC"] = 110.9
    r = bot.tick(day.replace(hour=12, minute=45), q)
    assert r["exits"] == 1
    c = intel.db(); t = dict(c.execute("SELECT * FROM trades WHERE symbol='CCC'").fetchone()); c.close()
    assert t["exit_reason"] == "breakeven stop" and -20 < t["pnl"] < 0 and t["mfe_pct"] > 1.5, t
    # a swing still open late in the day is flat by 3:55 PM, never carried overnight
    c = intel.db(); c.execute("UPDATE filings SET ref_price=79 WHERE symbol='BBB'"); c.commit(); c.close()
    r, _ = _ticks(day, 13, 0, q)
    assert r["entries"] == 1
    px["BBB"] = 80.3
    assert bot.tick(day.replace(hour=15, minute=30), q)["exits"] == 0
    r = bot.tick(day.replace(hour=15, minute=56), q)
    assert r["exits"] == 1
    sb = bot.scoreboard()
    assert sb["closed_trades"] == 3 and sb["losses"] == 1 and sb["win_rate_pct"] == 66.7, sb
    assert sb["exits"] == {"target": 1, "breakeven stop": 1, "end of day": 1}, sb["exits"]
    assert sb["trading_days"] == 1 and sb["capital_at_work"] == 50000 and sb["risk_per_trade"] == 100, sb
    r = bot.tick(day.replace(hour=14, minute=45), q)             # past 2:30 PM: no new swings
    assert r["entries"] == 0 and not bot.swing_window(day.replace(hour=14, minute=45), "regular")


def test_short_side():
    """Bearish reads go short: profit when price falls, loss when it rises,
    P&L sign mirrored, fills against us, and the switch turns shorts off."""
    c = intel.db()
    now = dt.datetime(2026, 11, 11, 8, 0, tzinfo=ET).astimezone(dt.timezone.utc).isoformat()
    for a, sym, ref in (("0001-26-000010", "SHA", 50.0), ("0001-26-000011", "SHB", 60.0), ("0001-26-000012", "SHC", 40.0)):
        c.execute("INSERT INTO universe (symbol,name,cik,price,asof) VALUES (?,?,?,?,?)", (sym, sym, 9000 + len(a), ref, now))
        c.execute("""INSERT INTO filings (accession,symbol,form,items,filed_utc,seen_utc,url,headline,ref_price,prev_close,
                     summary,read,catalyst,impact,direction,confidence,scored)
                     VALUES (?,?,'8-K','[]',?,?,'u',?,?,?,'s','r','offering','high','bear',80,1)""",
                  (a, sym, now, now, f"{sym}: offering", ref, ref))
    c.commit(); c.close()
    px = {"SHA": 49.5, "SHB": 59.4, "SHC": 36.0}             # SHC already down 10% -> skip
    chg = {"SHA": -1.0, "SHB": -1.0, "SHC": -10.0}
    q = lambda s: {"price": px[s], "chg_pct": chg[s], "prev_close": 0, "open": 0, "high": 0, "low": 0}
    day = dt.datetime(2026, 11, 11, tzinfo=ET)
    r, skipped = _ticks(day, 10, 0, q)
    assert r["entries"] == 2, r
    assert ("SHC", "already down 10.0% on the day") in skipped, skipped
    c = intel.db()
    sha = dict(c.execute("SELECT * FROM trades WHERE symbol='SHA'").fetchone()); c.close()
    assert sha["side"] == "short" and sha["entry_px"] < 49.5          # sold below the quote (slippage)
    assert sha["target_px"] < sha["entry_px"] < sha["stop_px"]
    px["SHA"] = 47.9                                                   # falls ~3.2% -> target
    px["SHB"] = 60.4                                                   # rises ~1.7% -> stop
    r = bot.tick(day.replace(hour=11, minute=0), q)
    assert r["exits"] == 2, r
    c = intel.db()
    a = dict(c.execute("SELECT * FROM trades WHERE symbol='SHA'").fetchone())
    b = dict(c.execute("SELECT * FROM trades WHERE symbol='SHB'").fetchone()); c.close()
    assert a["exit_reason"] == "target" and a["pnl"] > 0 and a["pnl_pct"] > 3.0 and a["exit_px"] > 47.9 and a["r_mult"] > 2, a
    assert b["exit_reason"] == "stop" and b["pnl"] < 0 and b["pnl_pct"] < -1.5 and b["r_mult"] < -1, b
    sb = bot.scoreboard()
    assert sb["by_side"]["short"]["trades"] == 2 and sb["by_side"]["short"]["wins"] == 1, sb["by_side"]
    assert sb["stop_slip_pct"] > 0                                     # the stop filled past its level, and the board says by how much
    bot.ALLOW_SHORT = False
    assert bot.side_for({"direction": "bear", "impact": "high", "confidence": 99}) is None
    bot.ALLOW_SHORT = True


def test_vault_render_and_git():
    files = vault.render(dt.datetime(2026, 11, 10, 17, 0, tzinfo=dt.timezone.utc))
    for k in ("README.md", "Universe.md", "Bot/Strategy.md", "Bot/Scoreboard.md", "data/filings.jsonl",
              "data/trades.jsonl", "data/universe.json", "data/scoreboard.json", "Tickers/AAA.md"):
        assert k in files, k
    assert any(k.startswith("Filings/") for k in files)
    assert any(k.startswith("Bot/Trades/") for k in files)
    assert "Sample too small" in files["Bot/Scoreboard.md"]
    sbj = json.loads(files["data/scoreboard.json"])
    assert sbj["scoreboard"]["closed_trades"] == 5 and sbj["rules"]["risk_usd"] == 100 and sbj["min_sample"] == 30
    assert sbj["rules"]["playbooks"]["swing"]["rr"] == 2.0 and len(sbj["rules"]["lines"]) >= 5
    assert "| swing |" in files["Bot/Scoreboard.md"] and "Catalyst day swing" in files["Bot/Strategy.md"]
    assert "| Playbook |" in files[next(k for k in files if k.startswith("Bot/Trades/"))]
    assert sbj["rules"]["allow_short"] is True and "short" in sbj["scoreboard"]["by_side"]
    assert "| short |" in files["Bot/Scoreboard.md"] and "Bearish read -> **short**" in files["Bot/Strategy.md"]
    assert "[[intel/Tickers/AAA|AAA]]" in files["Universe.md"]
    assert len(files["data/filings.jsonl"].splitlines()) == 7
    allt = "".join(files.values())
    assert chr(0x2014) not in allt, "em-dash in vault output"

    # bare 'GitHub' repo shaped like blackcrown-brain, plus an agent working copy
    bare = os.path.join(TMP, "brain.git"); agent = os.path.join(TMP, "agent")
    sh(f"git init -q --bare -b main {bare}")
    sh(f"git clone -q {bare} {agent}")
    os.makedirs(os.path.join(agent, "bard"))
    open(os.path.join(agent, "bard", "Charter.md"), "w").write("bard\n")
    sh("git add -A && git -c user.name=a -c user.email=a@x commit -qm 'brain sync' && git push -q origin main", agent)

    vault.REPO_URL = bare; vault.TOKEN = ""; vault.VAULT_DIR = os.path.join(TMP, "vault_clone")
    r = vault.sync()
    assert r["pushed"] and r["changed"] == len(files), r
    r = vault.sync()
    assert not r["pushed"] and r["changed"] == 0, r              # nothing new -> no empty commit

    # race: the agents' brain sync pushes AFTER Intel fetched but BEFORE it
    # pushes. Intel's first push is rejected; it must rebase and keep both.
    c = intel.db(); c.execute("UPDATE universe SET price=100.25 WHERE symbol='AAA'"); c.commit(); c.close()
    real_write = vault.write_files
    def racing_write(files, root):
        n = real_write(files, root)
        sh("git pull -q --rebase origin main", agent)
        open(os.path.join(agent, "bard", "Charter.md"), "a").write("more\n")
        sh("git -c user.name=a -c user.email=a@x commit -qam 'brain sync 2' && git push -q origin main", agent)
        return n
    vault.write_files = racing_write
    try:
        r = vault.sync()
    finally:
        vault.write_files = real_write
    assert r["pushed"], r
    sh("git pull -q origin main", agent)
    assert os.path.exists(os.path.join(agent, "intel", "Universe.md"))
    assert open(os.path.join(agent, "bard", "Charter.md")).read() == "bard\nmore\n"
    log = sh("git log --format=%s", agent)
    assert "brain sync 2" in log and log.count("Korvus Intel sync") == 2, log
    changed = sh("git diff --name-only HEAD~1 HEAD", agent).split()
    assert all(p.startswith("intel/") for p in changed), changed   # never touches agent folders


if __name__ == "__main__":
    try:
        for name in ("test_universe_parse", "test_filters", "test_simulated_day", "test_short_side",
                     "test_vault_render_and_git"):
            globals()[name](); print("ok", name)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

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
bot.session_open = lambda now: True
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
        ("0001-26-000002", "BBB", "med",  "bull", 70, 82.0),   # trigger, but below reference -> skip
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
    assert bot.is_trigger({"direction": "bull", "impact": "med", "confidence": 60})
    assert not bot.is_trigger({"direction": "bull", "impact": "low", "confidence": 99})
    assert not bot.is_trigger({"direction": "bear", "impact": "high", "confidence": 99})
    assert bot.confirm_entry({"ref_price": 99}, {"price": 100, "chg_pct": 2})[0]
    assert not bot.confirm_entry({"ref_price": 99}, {"price": 98, "chg_pct": 2})[0]
    assert not bot.confirm_entry({"ref_price": 90}, {"price": 100, "chg_pct": 9})[0]


def test_simulated_day():
    seed()
    px = {"AAA": 100.0, "BBB": 80.0, "CCC": 111.0}
    q = lambda s: {"price": px[s], "chg_pct": 1.0, "prev_close": px[s] - 1, "open": 0, "high": 0, "low": 0}
    day = dt.datetime(2026, 11, 10, tzinfo=ET)
    r = bot.tick(day.replace(hour=9, minute=35), q)            # before entry window
    assert r["entries"] == 0
    r = bot.tick(day.replace(hour=10, minute=0), q)
    assert r["entries"] == 1, r                                  # AAA only
    assert ("BBB", "below reference 82.00") in r["skipped"], r["skipped"]
    r = bot.tick(day.replace(hour=10, minute=5), q)
    assert r["entries"] == 0                                     # one trade per filing / per day
    px["AAA"] = 101.7                                            # +1.65% from ~100.05 fill
    r = bot.tick(day.replace(hour=11, minute=0), q)
    assert r["exits"] == 1
    sb = bot.scoreboard()
    assert sb["closed_trades"] == 1 and sb["wins"] == 1 and sb["win_rate_pct"] == 100.0 and not sb["sample_ok"]
    assert sb["exits"] == {"target": 1}
    # a losing trade on a fresh filing, closed by the time stop
    c = intel.db()
    c.execute("UPDATE filings SET direction='bull', ref_price=110 WHERE symbol='CCC'"); c.commit(); c.close()
    bot.tick(day.replace(hour=12, minute=0), q)
    px["CCC"] = 110.5
    r = bot.tick(day.replace(hour=15, minute=56), q)
    assert r["exits"] == 1
    sb = bot.scoreboard()
    assert sb["closed_trades"] == 2 and sb["losses"] == 1 and sb["win_rate_pct"] == 50.0, sb
    assert sb["max_drawdown_usd"] == 54.97 and sb["trading_days"] == 1 and sb["capital_at_work"] == 50000, sb


def test_vault_render_and_git():
    files = vault.render(dt.datetime(2026, 11, 10, 17, 0, tzinfo=dt.timezone.utc))
    for k in ("README.md", "Universe.md", "Bot/Strategy.md", "Bot/Scoreboard.md", "data/filings.jsonl",
              "data/trades.jsonl", "data/universe.json", "data/scoreboard.json", "Tickers/AAA.md"):
        assert k in files, k
    assert any(k.startswith("Filings/") for k in files)
    assert any(k.startswith("Bot/Trades/") for k in files)
    assert "Sample too small" in files["Bot/Scoreboard.md"]
    sbj = json.loads(files["data/scoreboard.json"])
    assert sbj["scoreboard"]["closed_trades"] == 2 and sbj["rules"]["target_pct"] == 1.5 and sbj["min_sample"] == 30
    assert "[[intel/Tickers/AAA|AAA]]" in files["Universe.md"]
    assert len(files["data/filings.jsonl"].splitlines()) == 4
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
        for name in ("test_universe_parse", "test_filters", "test_simulated_day", "test_vault_render_and_git"):
            globals()[name](); print("ok", name)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

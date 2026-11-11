"""The paper bot in all-sessions mode: trades Sunday 6 PM to Friday 5 PM ET,
rests in the 5 to 6 PM maintenance hour and on the weekend, only acts on a
fresh print outside the regular session, pays wider slippage there, and is
flat before maintenance. No network.  Run: python tests/test_paperbot_sessions.py
"""
import os, sys, tempfile, datetime as dt
from zoneinfo import ZoneInfo
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_SESSION"] = "all"
TMP = tempfile.mkdtemp(prefix="bot_sessions_")
import korvus_intel as intel
intel.DB_PATH = os.path.join(TMP, "intel.db")
import korvus_paperbot as bot
ET = ZoneInfo("America/New_York")
at = lambda d, h, m=0: dt.datetime(2026, 11, d, h, m, tzinfo=ET)       # Nov 2026: 8th = Sunday, 13th = Friday


def test_phases():
    assert bot.SESSION == "all" and bot.STRATEGY == "v3-all-sessions"
    P = bot.session_phase
    assert P(at(8, 17, 59)) == "weekend" and P(at(8, 18, 0)) == "overnight"        # Sunday open
    assert P(at(9, 3, 59)) == "overnight" and P(at(9, 4, 0)) == "premarket"
    assert P(at(9, 9, 30)) == "regular" and P(at(9, 15, 59)) == "regular"
    assert P(at(9, 16, 0)) == "afterhours" and P(at(9, 16, 59)) == "afterhours"
    assert P(at(9, 17, 0)) == "maintenance" and P(at(9, 17, 59)) == "maintenance"
    assert P(at(9, 18, 0)) == "afterhours" and P(at(9, 20, 0)) == "overnight"
    assert P(at(13, 16, 59)) == "afterhours" and P(at(13, 17, 0)) == "weekend"     # Friday close
    assert P(at(14, 12, 0)) == "weekend"
    for d, h, m, ok in ((9, 2, 0, True), (9, 8, 0, True), (9, 12, 0, True), (9, 16, 29, True), (9, 16, 30, False),
                        (9, 17, 30, False), (9, 18, 0, True), (14, 12, 0, False)):
        assert bot.can_enter(at(d, h, m), P(at(d, h, m))) is ok, (d, h, m)
    # a trading day runs 6 PM to 5 PM: Sunday evening already belongs to Monday
    assert bot.session_id(at(8, 19)) == bot.session_id(at(9, 10)) == "2026-11-09"
    assert bot.session_id(at(9, 18, 5)) == "2026-11-10"


def test_freshness_and_extended_quote():
    now = at(9, 2, 0).astimezone(dt.timezone.utc)
    t = now.timestamp()
    assert bot.quote_fresh({"ts": t - 60}, now, "overnight")
    assert not bot.quote_fresh({"ts": t - 16 * 60}, now, "overnight")            # last print 16 min ago: stale
    assert not bot.quote_fresh({}, now, "overnight") and bot.quote_fresh({}, now, "regular")
    snap = {"price": 100.0, "chg_pct": 1.0, "prev_close": 99.0, "ts": 1000.0}
    assert intel.apply_extended(snap, None) == snap
    assert intel.apply_extended(snap, (105.0, 900.0)) == snap                     # older than the quote: ignored
    ext = intel.apply_extended(snap, (101.97, 2000.0))
    assert ext["price"] == 101.97 and ext["ts"] == 2000.0 and abs(ext["chg_pct"] - 3.0) < 1e-6


def seed():
    intel.db_init()
    c = intel.db()
    seen = at(9, 1, 0).astimezone(dt.timezone.utc).isoformat()
    for acc, sym, d, ref in (("a-1", "AAA", "bull", 99.0), ("a-2", "SSS", "bear", 100.0)):
        c.execute("""INSERT INTO filings (accession,symbol,cik,company,form,items,filed_utc,seen_utc,url,headline,
                     ref_price,prev_close,summary,read,catalyst,impact,direction,confidence,scored)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                  (acc, sym, 1, sym + " Inc", "6-K", "[]", seen, seen, "u", "h", ref, ref, "s", "r", "earnings", "high", d, 80))
    c.commit(); c.close()


def test_week():
    seed()
    px, age = {"AAA": 100.0, "SSS": 99.0}, {"AAA": 3600, "SSS": 3600}
    clock = [None]
    q = lambda s: {"price": px[s], "chg_pct": 1.0, "prev_close": 99.0,
                   "ts": clock[0].astimezone(dt.timezone.utc).timestamp() - age[s]}
    def tick(d, h, m=0):
        clock[0] = at(d, h, m)
        return bot.tick(clock[0], q)

    r = tick(9, 2, 0)                                   # overnight, last prints an hour old: no fills
    assert r["phase"] == "overnight" and r["session_open"] and r["entries"] == 0
    assert ("AAA", "no fresh trade in the overnight session") in r["skipped"]
    age["AAA"] = 30
    r = tick(9, 2, 5)                                   # a real overnight print: the long fills, wider slippage
    assert r["entries"] == 1, r
    c = intel.db(); t = dict(c.execute("SELECT * FROM trades WHERE symbol='AAA'").fetchone()); c.close()
    assert abs(t["entry_px"] - 100.0 * (1 + bot.SLIP_EXT)) < 1e-6 and t["strategy"] == "v3-all-sessions"
    px["AAA"] = 103.0; age["AAA"] = 3600
    assert tick(9, 3, 0)["exits"] == 0                  # target is touched only on a stale quote: no exit
    age["AAA"] = 10
    assert tick(9, 7, 0)["exits"] == 1                  # pre-market print confirms it: target
    age["SSS"] = 5
    r = tick(9, 10, 0)                                  # regular session: the short fills at normal slippage
    assert r["entries"] == 1 and r["phase"] == "regular"
    c = intel.db(); t = dict(c.execute("SELECT * FROM trades WHERE symbol='SSS'").fetchone()); c.close()
    assert t["side"] == "short" and abs(t["entry_px"] - 99.0 * (1 - bot.SLIP)) < 1e-6
    assert tick(9, 16, 40)["exits"] == 0                # still open, no new entries in the last half hour
    r = tick(9, 16, 55)                                 # flat before the maintenance hour
    assert r["exits"] == 1 and bot.scoreboard()["exits"] == {"target": 1, "time": 1}, bot.scoreboard()["exits"]
    r = tick(9, 17, 30)
    assert r["phase"] == "maintenance" and not r["session_open"] and r["entries"] == 0
    r = tick(9, 18, 30)                                 # after maintenance: same filings never trade twice
    assert r["session_open"] and r["entries"] == 0
    assert tick(14, 12, 0)["phase"] == "weekend"

    # a position left open across the flat (service was down) closes at the first chance
    c = intel.db()
    c.execute("""INSERT INTO trades (strategy,symbol,accession,side,qty,entry_utc,entry_px,target_px,stop_px,status)
                 VALUES (?,?,?,?,?,?,?,?,?, 'open')""",
              (bot.STRATEGY, "AAA", "old", "long", 10, at(10, 12).astimezone(dt.timezone.utc).isoformat(), 103.0, 110.0, 90.0))
    c.commit(); c.close()
    age["AAA"] = 3600
    assert tick(10, 16, 0)["exits"] == 0                # same trading day: it stays open
    r = tick(10, 19, 0)                                 # next trading day began at 6 PM: closed even on a stale quote
    assert r["exits"] == 1 and bot.scoreboard()["exits"].get("overnight") == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok ", name)
    print("ALL SESSION TESTS PASSED")

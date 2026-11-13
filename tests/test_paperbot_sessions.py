"""The paper bot's clock: which session it is, when each playbook may enter,
that an extended-hours scalp only fills on a fresh print (at half risk, with a
wider stop), that a scalp is out inside 45 minutes, and that nothing is ever
carried overnight. No network.  Run: python tests/test_paperbot_sessions.py
"""
import os, sys, tempfile, datetime as dt
from zoneinfo import ZoneInfo
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="bot_sessions_")
import korvus_intel as intel
intel.DB_PATH = os.path.join(TMP, "intel.db")
import korvus_paperbot as bot
bot.EXTENDED = True
ET = ZoneInfo("America/New_York")
at = lambda d, h, m=0: dt.datetime(2026, 11, d, h, m, tzinfo=ET)       # Nov 2026: 9th = Monday, 14th = Saturday


def test_phases_and_windows():
    P = bot.session_phase
    assert P(at(9, 3, 59)) == "closed" and P(at(9, 4, 0)) == "premarket"
    assert P(at(9, 9, 30)) == "regular" and P(at(9, 15, 59)) == "regular"
    assert P(at(9, 16, 0)) == "afterhours" and P(at(9, 19, 59)) == "afterhours" and P(at(9, 20, 0)) == "closed"
    assert P(at(14, 12, 0)) == "weekend" and P(at(15, 19, 0)) == "weekend"          # Sunday evening is not a stock session
    S = lambda d, h, m: bot.scalp_window(at(d, h, m), P(at(d, h, m)))
    W = lambda d, h, m: bot.swing_window(at(d, h, m), P(at(d, h, m)))
    for h, m, ok in ((6, 59, False), (7, 0, True), (9, 24, True), (9, 25, False), (9, 39, False), (9, 40, True),
                     (15, 39, True), (15, 40, False), (16, 4, False), (16, 5, True), (18, 59, True), (19, 0, False)):
        assert S(9, h, m) is ok, (h, m)
    for h, m, ok in ((9, 59, False), (10, 0, True), (14, 29, True), (14, 30, False), (16, 30, False)):
        assert W(9, h, m) is ok, (h, m)
    bot.EXTENDED = False
    assert not S(9, 8, 0) and S(9, 11, 0)                                           # BOT_EXTENDED off: regular session only
    bot.EXTENDED = True
    assert bot.session_id(at(9, 19)) == "2026-11-09" and bot.session_id(at(10, 4)) == "2026-11-10"


def test_freshness_and_extended_quote():
    now = at(9, 18, 0).astimezone(dt.timezone.utc)
    t = now.timestamp()
    assert bot.quote_fresh({"ts": t - 60}, now, "afterhours")
    assert not bot.quote_fresh({"ts": t - 16 * 60}, now, "afterhours")           # last print 16 min ago: stale
    assert not bot.quote_fresh({"ts": t - 6 * 60}, now, "afterhours", bot.ENTRY_QUOTE_AGE)   # entries need 5 minutes
    assert not bot.quote_fresh({}, now, "afterhours") and bot.quote_fresh({}, now, "regular")
    snap = {"price": 100.0, "chg_pct": 1.0, "prev_close": 99.0, "ts": 1000.0}
    assert intel.apply_extended(snap, None) == snap
    assert intel.apply_extended(snap, (105.0, 900.0)) == snap                     # older than the quote: ignored
    ext = intel.apply_extended(snap, (101.97, 2000.0))
    assert ext["price"] == 101.97 and ext["ts"] == 2000.0 and abs(ext["chg_pct"] - 3.0) < 1e-6


def seed():
    intel.db_init()
    c = intel.db()
    seen = at(9, 16, 10).astimezone(dt.timezone.utc).isoformat()                 # an earnings release just after the close
    c.execute("""INSERT INTO filings (accession,symbol,cik,company,form,items,filed_utc,seen_utc,url,headline,
                 ref_price,prev_close,summary,read,catalyst,impact,direction,confidence,scored)
                 VALUES ('a-1','AAA',1,'AAA Inc','8-K','[]',?,?,'u','h',100.0,100.0,'s','r','earnings','high','bull',80,1)""", (seen, seen))
    c.commit(); c.close()


def test_after_hours_scalp_then_next_day():
    seed()
    px, age = {"AAA": 100.5}, {"AAA": 3600}
    clock = [None]
    q = lambda s: {"price": px[s], "chg_pct": 0.5, "prev_close": 100.0,
                   "ts": clock[0].astimezone(dt.timezone.utc).timestamp() - age[s]}
    def tick(d, h, m=0):
        clock[0] = at(d, h, m)
        return bot.tick(clock[0], q)

    r = tick(9, 16, 15)                                 # the last print is an hour old: not a market, no fill
    assert r["phase"] == "afterhours" and r["entries"] == 0
    assert ("AAA", "no trade printed in the last 5 minutes (after-hours)") in r["skipped"], r["skipped"]
    age["AAA"] = 60
    r = tick(9, 16, 16)                                 # fresh print, +0.5%: first of two checks
    assert r["entries"] == 0 and r["watch"][0]["state"] == "confirming" and r["watch"][0]["playbook"] == "scalp"
    r = tick(9, 16, 17)                                 # held: the scalp fills, wider slippage, half risk, wider stop
    assert r["entries"] == 1, r
    c = intel.db(); t = dict(c.execute("SELECT * FROM trades WHERE symbol='AAA'").fetchone()); c.close()
    assert t["playbook"] == "scalp" and t["phase"] == "afterhours" and t["qty"] == 27 and t["stop_pct"] == 1.8
    assert abs(t["entry_px"] - 100.5 * (1 + bot.SLIP_EXT)) < 1e-3 and t["risk_usd"] < 51
    px["AAA"] = 104.0; age["AAA"] = 3600
    assert tick(9, 16, 30)["exits"] == 0                # target touched only on a stale quote: no exit
    px["AAA"] = 101.0; age["AAA"] = 30
    assert tick(9, 16, 50)["exits"] == 0                # between stop and target, inside the 45 minutes
    r = tick(9, 17, 3)                                  # 46 minutes in: a scalp does not linger
    assert r["exits"] == 1 and bot.scoreboard()["exits"] == {"time limit": 1}
    assert bot.scoreboard()["by_phase"]["afterhours"]["trades"] == 1 and bot.scoreboard()["by_playbook"]["scalp"]["wins"] == 1
    r = tick(9, 17, 10)
    assert r["entries"] == 0 and r["watch"][0]["state"] == "waiting", r["watch"]   # no longer fresh: now a swing candidate for tomorrow
    assert tick(9, 21, 0)["phase"] == "closed" and tick(14, 12, 0)["phase"] == "weekend"

    # the same catalyst is a swing candidate at the next session, as its own trade
    age["AAA"] = 20; px["AAA"] = 101.2
    assert tick(10, 9, 45)["entries"] == 0
    tick(10, 10, 0); tick(10, 10, 1)
    r = tick(10, 10, 2)
    assert r["entries"] == 1
    c = intel.db(); t = dict(c.execute("SELECT * FROM trades WHERE symbol='AAA' AND status='open'").fetchone()); c.close()
    assert t["playbook"] == "swing" and t["phase"] == "regular" and abs(t["entry_px"] - 101.2 * (1 + bot.SLIP)) < 1e-3
    assert tick(10, 15, 54)["exits"] == 0
    r = tick(10, 15, 55)                                # flat before the close
    assert r["exits"] == 1 and bot.scoreboard()["exits"].get("end of day") == 1

    # a position left open past the flat (service was down) closes at the first chance, even on a stale quote
    c = intel.db()
    for sym, ent in (("LATE", at(11, 12)), ("OLD", at(11, 14))):
        c.execute("""INSERT INTO trades (strategy,symbol,accession,side,qty,entry_utc,entry_px,target_px,stop_px,status,playbook,phase,risk_usd,init_stop_px,stop_pct)
                     VALUES (?,?,?,?,?,?,?,?,?, 'open','swing','regular',100,90.0,1.5)""",
                  (bot.STRATEGY, sym, "x-" + sym, "long", 10, ent.astimezone(dt.timezone.utc).isoformat(), 103.0, 110.0, 90.0))
    c.commit(); c.close()
    px.update(LATE=103.5, OLD=103.5); age.update(LATE=3600, OLD=3600)
    r = tick(11, 16, 30)                                # after the close the same day
    assert r["exits"] == 2 and bot.scoreboard()["exits"].get("end of day") == 3
    c = intel.db()
    c.execute("UPDATE trades SET status='open', exit_utc=NULL WHERE symbol='OLD'"); c.commit(); c.close()
    r = tick(12, 8, 0)                                  # the next morning
    assert r["exits"] == 1 and bot.scoreboard()["exits"].get("overnight") == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok ", name)
    print("ALL SESSION TESTS PASSED")

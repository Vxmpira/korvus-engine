"""Offline tests for the second-crawler upgrade: index funds, 5-year chart,
live quotes, the EDGAR feed hook (announce once), the analyst agent (safe
briefs, rule fallback, rewrite only on change), the vault notes, and the
admin status. No network, no API keys.  Run: python tests/test_compass_v2.py
"""
import os, sys, tempfile, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
TMP = tempfile.mkdtemp(prefix="compass2_")
os.environ.pop("ANTHROPIC_API_KEY", None)
import korvus_compass as c
from test_compass import facts, _series
c.DB_PATH = os.path.join(TMP, "compass.db")
c.KORVUS_DB = os.path.join(TMP, "none.db")
c.INTEL_DB = os.path.join(TMP, "none2.db")
import korvus_compass_agent as agent
import korvus_compass_vault as cv

UNI = [{"symbol": "GOOD", "name": "Good Co", "sector": "Information Technology", "industry": "Software",
        "cik": 111, "in_sp": 1, "in_ndx": 1},
       {"symbol": "MEH", "name": "Meh Inc", "sector": "Industrials", "industry": "Machines", "cik": 222,
        "in_sp": 1, "in_ndx": 0}]


def seed():
    c.db_init()
    c.fetch_universe = lambda: (UNI, "test")
    c.refresh_universe(verbose=False)
    long = [((dt.date(2019, 1, 1) + dt.timedelta(days=i)).isoformat(), 50 + 0.1 * i) for i in range(1500)]
    st = c.history_stats(long)
    for sym in ("GOOD", "SPY"):
        c.save_fields(sym, {k: st[k] for k in st})
    c.save_fields("GOOD", {"fund": c.parse_companyfacts(facts()), "fund_accn": "acc-1",
                           "metrics": {"marketCapitalization": 0.02, "peTTM": 18},
                           "analysts": {"score": 4.4, "label": "Strong buy", "analysts": 30, "buy_pct": 0.9},
                           "filings": [{"form": "10-K", "label": "Annual report", "filed": "2026-02-01",
                                        "accn": "acc-1", "items": "", "url": "u"}]})
    c.rescore(emit=False)
    return st


def test_funds_and_5y():
    st = seed()
    assert st["chart5"] and len(st["chart5"]["c"]) == 252 and st["chart5"]["m200"][-1] and st["ret_5y"] > 0
    assert c.history_stats(_series(400, 100, 0.5))["chart5"] is not None       # 80 weekly points
    assert c.history_stats(_series(200, 100, 0.5))["chart5"] is None
    assert c.active_symbols() == ["GOOD", "MEH"] and set(c.active_symbols(kind="etf")) == set(c.ETFS)
    d = c.api_list()
    assert [x["s"] for x in d["etfs"]] == list(c.ETFS) and [x["s"] for x in d["etfs"] if x["g"] == "us"] == ["SPY", "QQQ", "DIA", "IWM"] and {x["s"] for x in d["stocks"]} == {"GOOD", "MEH"}
    spy = c.api_stock("SPY")
    assert spy["kind"] == "etf" and spy["tier"] == "Index fund" and spy["score"] is None and spy["read"] == "bull"
    assert "ES futures" in spy["note"] and spy["reads"] == [] and spy["chart5"]["c"]
    # a universe refresh never drops the funds and never announces them
    c.refresh_universe(verbose=False)
    assert set(c.active_symbols(kind="etf")) == set(c.ETFS) and c.api_events() == []


SCREEN = [
    {"symbol": "TSM", "companyName": "Taiwan Semiconductor Manufacturing", "marketCap": 9e11, "country": "TW", "sector": "Technology"},
    {"symbol": "AAPL", "companyName": "Apple Inc.", "marketCap": 3e12, "country": "US"},
    {"symbol": "NVO", "companyName": "Novo Nordisk A/S", "marketCap": 4e11, "country": "DK"},
    {"symbol": "TINY", "companyName": "Tiny Foreign Co", "marketCap": 2e9, "country": "GB"},
    {"symbol": "RY", "companyName": "Royal Bank of Canada", "marketCap": 2e11, "country": "CA"},
    {"symbol": "RYX", "companyName": "Royal Bank of Canada Pref", "marketCap": 1e11, "country": "CA"},
    {"symbol": "EWX", "companyName": "Some Fund", "marketCap": 5e10, "country": "JP", "isEtf": True},
    {"symbol": "GOOD", "companyName": "Good Co plc", "marketCap": 5e10, "country": "IE"},
]


def test_global():
    g = c.parse_global(SCREEN)
    assert [x["symbol"] for x in g] == ["TSM", "NVO", "RY", "GOOD"], g          # biggest first; US, small, funds, dup class out
    assert c.parse_global(SCREEN, limit=2)[-1]["symbol"] == "NVO" and c.parse_global([]) == []
    m = {x["symbol"]: x for x in c.merge_universe(
        [{"symbol": "GOOD", "name": "Good Co", "sector": "Tech", "industry": "", "cik": 111}], [], {"TSM": 1046179}, [], g)}
    assert m["TSM"]["in_global"] == 1 and m["TSM"]["country"] == "TW" and m["TSM"]["cik"] == 1046179
    assert m["GOOD"]["in_sp"] == 1 and m["GOOD"]["in_global"] == 0 and m["GOOD"]["country"] == "IE"   # index member keeps its seat
    assert m["NVO"]["cik"] is None
    # arriving on a live board: one big batch is silent, the stocks carry their country
    many = [dict(x, symbol=f"F{i:03d}", companyName=f"Foreign {i}", country="JP") for i, x in enumerate([SCREEN[0]] * 30)]
    rows = c.merge_universe(UNI, UNI[:1], {}, [], c.parse_global(many))
    c.fetch_universe = lambda: (rows, "test")
    before = len(c.api_events())
    assert c.refresh_universe(verbose=False) == 32 and len(c.api_events()) == before
    d = c.api_list()
    jp = [x for x in d["stocks"] if x["co"] == "JP"]
    assert len(jp) == 30 and jp[0]["gl"] == 1 and jp[0]["cn"] == "Japan"
    assert [x for x in d["stocks"] if x["s"] == "GOOD"][0]["co"] == "US"
    st = c.admin_status()["counts"]
    assert st["international"] == 30 and st["countries"] == 2
    assert "HOME COUNTRY: Japan (a foreign company" in agent.data_sheet(dict(c.load_stock("F001"), parts=[]))
    c.fetch_universe = lambda: (UNI, "test")                                    # back to the small board for the rest
    c.refresh_universe(verbose=False)
    assert c.active_symbols() == ["GOOD", "MEH"]


def test_live_quotes():
    q = c.parse_quotes([{"symbol": "GOOD", "price": 201.5, "changePercentage": 1.25},
                        {"symbol": "BRK.B", "price": 500, "changesPercentage": -0.5}, {"symbol": "X"}, "junk" and {}])
    assert q == {"GOOD": (201.5, 0.0125), "BRK-B": (500.0, -0.005)}
    c.fetch_quotes = lambda syms: {"GOOD": (201.5, 0.0125), "SPY": (600.0, 0.002)}
    assert c.job_live() == 2
    c.LIVE = False
    assert "lp" not in c.api_list()["stocks"][0] and c.api_list()["live"] is False     # switch off: never shown
    c.LIVE = True
    d = c.api_list()
    g = [x for x in d["stocks"] if x["s"] == "GOOD"][0]
    assert g["lp"] == 201.5 and g["lc"] == 0.0125 and d["live"] is True and d["etfs"][0]["lp"] == 600.0
    assert c.api_stock("GOOD")["live"]["lp"] == 201.5
    c.save_fields("GOOD", {"live_utc": c.iso(c.utcnow() - dt.timedelta(hours=2))})      # stale quote: hidden
    assert "lp" not in [x for x in c.api_list()["stocks"] if x["s"] == "GOOD"][0]
    c.LIVE = False


def test_feed_hook():
    calls = []
    c.job_filings = lambda **kw: calls.append(kw.get("symbols")) or 0
    entry = lambda acc, form="8-K", cik=111: {"form": form, "company": "Good Co", "cik": cik, "url": "http://sec/x",
                                              "accession": acc, "items": ["2.02"], "updated": ""}
    feed = {"8-K": [entry("a-1"), entry("z-9", cik=999), entry("f-4", form="4")]}
    assert c.job_feed(verbose=False, fetch=lambda f: feed.get(f, [])) == 0      # first poll only learns
    assert c.api_events() == [] and calls == []
    feed["8-K"].insert(0, entry("a-2"))
    feed["10-Q"] = [entry("q-1", form="10-Q", cik=222)]
    assert c.job_feed(verbose=False, fetch=lambda f: feed.get(f, [])) == 2
    ev = c.api_events()
    assert {e["symbol"] for e in ev} == {"GOOD", "MEH"} and all(e["kind"] == "filing" for e in ev)
    assert any("filed a 8-K: Material event" in e["title"] and "Items 2.02" in e["detail"] for e in ev)
    assert calls == [["GOOD", "MEH"]]                                            # numbers re-read right away
    assert c.job_feed(verbose=False, fetch=lambda f: feed.get(f, [])) == 0      # same feed again: silent
    assert len(c.api_events()) == 2
    conn = c.db()
    assert c.first_seen("a-2", "GOOD", conn) is False and c.first_seen("new", "GOOD", conn) is True
    assert c.first_seen("", "GOOD", conn) is False
    conn.commit(); conn.close()
    assert c.match_feed([entry("x", cik=111)], {111: ["GOOG", "GOOGL"]}) == [("GOOG", entry("x")), ("GOOGL", entry("x"))]


GOOD_BRIEF = {"headline": "Fast grower with net cash \u2014 priced fairly", "summary": "Revenue is growing. Profit is solid.",
              "bull": ["Revenue up 33%", "Net cash of $500", "a", "b"], "bear": ["Trades at 18x earnings"],
              "watch": "Next quarter revenue.", "fit": "Tends to suit growth-minded holders."}


def test_agent():
    assert agent.clean_brief(None) is None and agent.clean_brief({"headline": "x"}) is None
    b = agent.clean_brief(GOOD_BRIEF)
    assert "\u2014" not in b["headline"] and len(b["bull"]) == 3
    for bad in ("You should buy this now.", "We see a price target of $300.", "This stock will soar.", "It is a buy."):
        assert agent.clean_brief(dict(GOOD_BRIEF, summary=bad)) is None, bad
    s = c.load_stock("GOOD")
    sheet = agent.data_sheet(s)
    assert "COMPASS SCORE" in sheet and "revenue $2.0K" in sheet and "ANALYSTS: 30 covering" in sheet
    rb = agent.rule_brief(s)
    assert rb["by"] == "rules" and rb["headline"].startswith("Top rated") and rb["bull"] and rb["bear"] and rb["fit"]
    assert agent.rule_brief(c.load_stock("MEH"))["headline"].startswith("MEH: the crawler is still gathering")
    assert agent.write_brief(s, [("haiku", lambda sheet: GOOD_BRIEF)])["by"] == "haiku"
    assert agent.write_brief(s, [("crown", lambda sheet: 1 / 0), ("haiku", lambda sheet: GOOD_BRIEF)])["by"] == "haiku"
    assert agent.write_brief(s, [("haiku", lambda sheet: dict(GOOD_BRIEF, watch="buy now"))])["by"] == "rules"
    assert agent.write_brief(s, [])["by"] == "rules"


def test_brief_jobs():
    agent.fill_rule_briefs()
    assert agent.fill_rule_briefs() == 0                                         # every page filled at once
    assert c.api_stock("MEH")["brief"]["headline"]
    assert c.api_stock("GOOD")["brief"]["by"] == "rules"
    n = []
    ask = [("haiku", lambda sheet: n.append(1) or GOOD_BRIEF)]
    assert agent.job_briefs(askers=ask, verbose=False) == 1                       # MEH is still Building: skipped
    assert c.api_stock("GOOD")["brief"]["by"] == "haiku" and len(n) == 1
    assert c.api_list()["stocks"][0]["hl"] or c.api_list()["stocks"][1]["hl"]
    assert agent.job_briefs(askers=ask, verbose=False) == 0 and len(n) == 1       # nothing changed: no rewrite
    c.save_fields("GOOD", {"analysts": {"score": 3.0, "label": "Hold", "analysts": 30, "buy_pct": 0.4}})
    assert agent.job_briefs(askers=ask, verbose=False) == 0                       # changed, but written < 24h ago
    c.save_fields("GOOD", {"fund_accn": "acc-2"})
    assert agent.job_briefs(askers=ask, verbose=False) == 1 and len(n) == 2       # a new report always rewrites
    assert agent.job_briefs(askers=ask, verbose=False, symbols=["GOOD"]) == 0
    assert "rule-written" in agent.describe()


def test_vault_and_admin():
    files = cv.render()
    assert {"README.md", "Board.md", "Updates.md", "data/board.json", "Stocks/GOOD.md"} <= set(files)
    assert "Stocks/SPY.md" not in files and "| SPY | S&P 500 |" in files["Board.md"]
    g = files["Stocks/GOOD.md"]
    assert "## Fast grower with net cash, priced fairly" in g and "What to be careful about" in g and "Research, not advice" in g
    assert "[[compass/Stocks/GOOD|GOOD]]" in files["Board.md"] and "[[intel/README|Korvus Intel]]" in files["README.md"]
    assert cv.render() == files                                                   # idle render: no diff, no commit
    assert cv.lookup()["GOOD"]["tier"] == "Top rated"
    assert "\u2014" not in "".join(files.values())
    st = c.admin_status()
    k = st["counts"]
    assert k["stocks"] == 2 and k["international"] == 0 and k["funds"] == len(c.ETFS) == 12 and k["scored"] == 1 and k["briefs"] == 2 and k["briefs_by"]["haiku"] == 1
    assert k["filings_24h"] == 2 and st["running"] is False and st["top"][0]["symbol"] == "GOOD" and len(st["events"]) == 2
    c.meta_set("heartbeat", c.iso())
    assert c.admin_status()["running"] is True


def test_status_never_wiped():
    """One job's heartbeat must never erase another's, even when the store is
    busy or the heartbeat fails outright (the phone showed every job but the
    filing feed as "pending")."""
    import sqlite3, threading
    seed()
    conn = c.db()
    conn.execute("DELETE FROM meta WHERE k LIKE 'status:%'")
    # a board written by the old single-blob layout still shows up
    conn.execute("INSERT INTO meta(k,v) VALUES('status', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 ('{"prices":{"ok":true,"last":"old","count":7},"universe":{"ok":true,"count":2}}',))
    conn.commit(); conn.close()
    c.status_set("feed", ok=True, last="now")
    st = c.api_list()["status"]
    assert st["prices"]["count"] == 7 and st["universe"]["count"] == 2 and st["feed"]["last"] == "now", st
    # an update merges into the job's own row and keeps what the old layout knew
    c.status_set("prices", last="new")
    st = c.api_list()["status"]
    assert st["prices"] == {"ok": True, "last": "new", "count": 7} and st["feed"]["ok"] is True, st
    # a heartbeat that cannot reach the store is skipped, never written as a wipe
    real = c.db
    def broken():
        raise sqlite3.OperationalError("database is locked")
    c.db = broken
    try:
        c.status_set("scores", ok=True)
    finally:
        c.db = real
    st = c.api_list()["status"]
    assert "scores" not in st and st["prices"]["last"] == "new" and st["feed"]["last"] == "now", st
    # many jobs reporting at once all land
    jobs = ["prices", "filings", "analysts", "scores", "agent", "feed", "live", "universe"]
    ts = [threading.Thread(target=lambda j=j: [c.status_set(j, ok=True, n=i) for i in range(15)]) for j in jobs]
    [t.start() for t in ts]; [t.join() for t in ts]
    st = c.api_list()["status"]
    assert all(st[j]["n"] == 14 for j in jobs), st
    assert st["prices"]["count"] == 7


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("ALL COMPASS V2 TESTS PASSED")

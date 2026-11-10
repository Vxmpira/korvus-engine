"""Offline tests for Korvus Compass: universe parse, trend math, SEC XBRL
parsing (incl. derived Q4 and restatements), scoring, change events, and the
read API. No network, no API keys.  Run: python tests/test_compass.py
"""
import os, sys, tempfile, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="compass_test_")
import korvus_compass as c
c.DB_PATH = os.path.join(TMP, "compass.db")
c.KORVUS_DB = os.path.join(TMP, "none.db")
c.INTEL_DB = os.path.join(TMP, "none2.db")

WIKI = """<table class="wikitable"><tr><th>x</th></tr><tr><td>skip</td></tr></table>
<table class="wikitable sortable" id="constituents"><tbody>
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th><th>HQ</th><th>Date added</th><th>CIK</th><th>Founded</th></tr>
<tr><td><a href="#">MMM</a></td><td>3M</td><td>Industrials</td><td>Conglomerates</td><td>MN</td><td>1957</td><td>0000066740</td><td>1902</td></tr>
<tr><td>BRK.B</td><td>Berkshire Hathaway<sup>[4]</sup></td><td>Financials</td><td>Multi-Sector</td><td>NE</td><td>2010</td><td>0001067983</td><td>1839</td></tr>
</tbody></table>"""
NDX = """<table id="constituents"><tr><th>Ticker</th><th>Company</th><th>ISIN</th><th>ICB Industry[2]</th><th>ICB Subsector[2]</th></tr>
<tr><td>MMM</td><td>3M Co</td><td>US1</td><td>Tech</td><td>Soft</td></tr>
<tr><td>ASML</td><td>ASML Holding</td><td>NL1</td><td>Technology</td><td>Semis</td></tr></table>"""


def test_universe():
    sp = c.parse_wiki_constituents(WIKI)
    assert [x["symbol"] for x in sp] == ["MMM", "BRK-B"], sp
    assert sp[1]["cik"] == 1067983 and sp[1]["name"] == "Berkshire Hathaway"
    nd = c.parse_wiki_constituents(NDX)
    assert [x["symbol"] for x in nd] == ["MMM", "ASML"] and nd[1]["sector"] == "Technology"
    u = c.merge_universe(sp, nd, {"ASML": 937966}, ["zzz"])
    by = {x["symbol"]: x for x in u}
    assert by["MMM"]["in_sp"] == 1 and by["MMM"]["in_ndx"] == 1 and by["MMM"]["sector"] == "Industrials"
    assert by["ASML"]["cik"] == 937966 and by["ASML"]["in_sp"] == 0 and "ZZZ" in by
    assert c.parse_wiki_constituents("<html>nothing</html>") == []


def _series(n, start, step):
    d0 = dt.date(2024, 1, 1)
    return [((d0 + dt.timedelta(days=i)).isoformat(), start + step * i) for i in range(n)]


def test_trend():
    up = c.history_stats(_series(400, 100, 0.5))
    assert up["trend"] == "Strong uptrend" and up["trend_tone"] == "bull"
    assert up["pos52"] == 1.0 and up["drawdown"] == 0 and up["ret_1y"] > 0
    assert len(up["chart"]["c"]) == 252 and up["chart"]["m200"][-1] and len(up["spark"]) > 40
    dn = c.history_stats(_series(400, 400, -0.5))
    assert dn["trend"] == "Downtrend" and dn["trend_tone"] == "bear" and dn["pos52"] == 0
    assert c.trend_label(105, 110, 100, 0.01) == ("Pullback in uptrend", "neut")
    assert c.trend_label(105, 95, 100, 0.01) == ("Turning up", "neut")
    assert c.trend_label(95, 110, 100, -0.01) == ("Losing steam", "neut")
    assert c.history_stats([("2024-01-01", 5)]) == {}
    short = c.history_stats(_series(60, 10, 0.1))
    assert short["ma200"] is None and short["trend"] == "Short-term uptrend"


def dur(s, e, v, filed="2026-01-30", form="10-K"):
    return {"start": s, "end": e, "val": v, "filed": filed, "form": form}


def inst(e, v, filed="2026-01-30"):
    return {"end": e, "val": v, "filed": filed, "form": "10-K"}


def facts():
    rev = [dur("2021-01-01", "2021-12-31", 800), dur("2022-01-01", "2022-12-31", 1000),
           dur("2023-01-01", "2023-12-31", 1200), dur("2024-01-01", "2024-12-31", 1500),
           dur("2025-01-01", "2025-12-31", 1900, "2026-01-01"),
           dur("2025-01-01", "2025-12-31", 2000, "2026-02-01"),          # restated, later filing wins
           dur("2024-10-01", "2024-12-31", 420),
           dur("2025-01-01", "2025-03-31", 450), dur("2025-04-01", "2025-06-30", 480),
           dur("2025-07-01", "2025-09-30", 500), dur("2025-01-01", "2025-09-30", 1430)]  # YTD ignored
    ni = [dur("2024-01-01", "2024-12-31", 300), dur("2025-01-01", "2025-12-31", 400)]
    return {"cik": 1, "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [dur("2015-01-01", "2015-12-31", 5)]}},   # stale tag loses
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rev}},
        "NetIncomeLoss": {"units": {"USD": ni}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [dur("2024-01-01", "2024-12-31", 3.0),
                                                              dur("2025-01-01", "2025-12-31", 4.0)]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [dur("2025-01-01", "2025-12-31", 600)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [dur("2025-01-01", "2025-12-31", 100)]}},
        "PaymentsOfDividends": {"units": {"USD": [dur("2025-01-01", "2025-12-31", 50)]}},
        "PaymentsForRepurchaseOfCommonStock": {"units": {"USD": [dur("2025-01-01", "2025-12-31", 150)]}},
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
            dur("2022-01-01", "2022-12-31", 110), dur("2023-01-01", "2023-12-31", 106),
            dur("2024-01-01", "2024-12-31", 103), dur("2025-01-01", "2025-12-31", 100)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [inst("2025-12-31", 900)]}},
        "LongTermDebtNoncurrent": {"units": {"USD": [inst("2025-12-31", 300)]}},
        "LongTermDebtCurrent": {"units": {"USD": [inst("2025-12-31", 100)]}},
        "StockholdersEquity": {"units": {"USD": [inst("2025-12-31", 2000)]}},
    }}}


def test_companyfacts():
    f = c.parse_companyfacts(facts())
    assert f["revenue"] == 2000 and f["fy_end"] == "2025-12-31" and f["currency"] == "USD"
    assert abs(f["revenue_yoy"] - (2000 / 1500 - 1)) < 1e-3
    assert abs(f["revenue_cagr3"] - ((2000 / 1000) ** (1 / 3) - 1)) < 1e-3
    assert f["net_margin"] == 0.2 and f["fcf"] == 500 and f["fcf_margin"] == 0.25
    assert f["debt"] == 400 and f["net_cash"] == 500 and f["debt_to_equity"] == 0.2 and f["roe"] == 0.2
    assert abs(f["shares_change_3y"] - (100 / 110 - 1)) < 1e-3
    # Q4 derived: 2000 - (450+480+500) = 570, vs 420 a year earlier
    assert f["q_end"] == "2025-12-31" and f["q_revenue"] == 570, (f["q_end"], f["q_revenue"])
    assert abs(f["q_revenue_yoy"] - (570 / 420 - 1)) < 1e-3
    assert f["revenue_ttm"] == 2000
    assert f["history"]["revenue"][-1] == ["2025-12-31", 2000.0] and len(f["history"]["revenue"]) == 5
    assert c.parse_companyfacts({}) == {} and c.parse_companyfacts({"facts": {"dei": {}}}) == {}
    ifrs = {"facts": {"ifrs-full": {"Revenue": {"units": {"EUR": [dur("2024-01-01", "2024-12-31", 10),
                                                                 dur("2025-01-01", "2025-12-31", 12)]}}}}}
    g = c.parse_companyfacts(ifrs)
    assert g["currency"] == "EUR" and g["revenue"] == 12


def test_submissions():
    j = {"cik": "320193", "filings": {"recent": {
        "form": ["4", "8-K", "10-Q", "S-8"], "accessionNumber": ["a-1", "0000320193-26-000010", "0000320193-26-000009", "x"],
        "primaryDocument": ["f4.xml", "ex.htm", "q.htm", "s.htm"],
        "filingDate": ["2026-11-01", "2026-10-30", "2026-08-01", "2026-07-01"], "items": ["", "2.02,9.01", "", ""]}}}
    fl = c.parse_submissions(j)
    assert [x["form"] for x in fl] == ["8-K", "10-Q"] and fl[0]["label"] == "Material event"
    assert fl[0]["url"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/ex.htm"
    assert c.parse_submissions({}) == []


def test_analysts():
    a = c.analyst_summary([{"period": "2026-11-01", "strongBuy": 10, "buy": 20, "hold": 8, "sell": 2, "strongSell": 0},
                           {"period": "2026-10-01", "strongBuy": 8, "buy": 18, "hold": 12, "sell": 2, "strongSell": 0}])
    assert a["analysts"] == 40 and a["label"] == "Buy" and a["buy_pct"] == 0.75 and a["trend"] > 0
    assert c.analyst_summary([]) == {} and c.analyst_summary([{"period": "x"}]) == {}


def good():
    s = dict(c.history_stats(_series(400, 100, 0.5)), symbol="GOOD", sector="Information Technology")
    s["fund"] = c.parse_companyfacts(facts())
    s["metrics"] = {"marketCapitalization": 0.02, "peTTM": 18}     # $20k cap in Finnhub's millions
    s["analysts"] = {"score": 4.4, "label": "Strong buy", "analysts": 30, "buy_pct": 0.9}
    return s


def test_score():
    r = c.score_stock(good())
    assert r["score"] >= 75 and r["tier"] == "Top rated" and r["read"] == "bull", r
    assert r["coverage"] == 0.95 and [p for p in r["parts"] if p["key"] == "catalysts"][0]["value"] is None
    assert abs(r["val"]["fcf_yield"] - 500 / 20000) < 1e-6 and r["val"]["pe"] == 18
    bad = dict(c.history_stats(_series(400, 400, -0.5)), symbol="BAD", sector="Industrials")
    bad["fund"] = {"currency": "USD", "revenue": 100, "revenue_cagr3": -0.08, "revenue_yoy": -0.15,
                   "net_margin": -0.2, "fcf": -30, "fcf_margin": -0.3, "roe": -0.1, "debt_to_equity": 3.0,
                   "shares_change_3y": 0.2, "eps": -1, "shares_diluted": 10}
    bad["analysts"] = {"score": 2.4, "label": "Sell", "analysts": 9, "buy_pct": 0.1}
    bad["_reads"] = [{"dir": "bear", "impact": "high"}]
    r2 = c.score_stock(bad)
    assert r2["score"] < 20 and r2["tier"] == "Weak" and r2["read"] == "bear", r2
    r3 = c.score_stock({"symbol": "NEW"})
    assert r3["score"] is None and r3["tier"] == "Building" and r3["read"] == "neut"
    # banks skip balance-sheet and cash-flow tests instead of being punished by them
    bank = dict(good(), sector="Financials")
    keys = {p["key"]: p["value"] for p in c.score_stock(bank)["parts"]}
    assert keys["balance"] is None and keys["quality"] is not None
    # a foreign-currency filer never has its EPS divided into a USD price
    adr = dict(good(), metrics={})
    adr["fund"] = dict(adr["fund"], currency="TWD")
    v = c.valuation(adr)
    assert v["pe"] is None and v["fcf_yield"] is None and v["mcap"] is None
    assert c.catalyst_tone([]) is None
    assert c.catalyst_tone([{"dir": "bull", "impact": "high"}, {"dir": "bear", "impact": "low"}]) == 0.5


def test_events():
    old = {"symbol": "X", "trend": "Uptrend", "ma50": 99, "ma200": 100, "price": 100, "hi52": 110, "lo52": 80,
           "tier": "Solid", "score": 55, "analysts": {"label": "Hold"}}
    new = {"symbol": "X", "trend": "Strong uptrend", "trend_tone": "bull", "ma50": 101, "ma200": 100,
           "price": 111, "tier": "Strong", "score": 66, "analysts": {"label": "Buy", "analysts": 12}}
    kinds = [e[0] for e in c.diff_events(old, new)]
    assert kinds == ["trend", "cross", "high", "tier", "analysts"], kinds
    assert all(e[1] == "bull" for e in c.diff_events(old, new))
    assert c.diff_events({}, new) == [] and c.diff_events(old, old) == []
    dn = c.diff_events(dict(old, ma50=101), dict(old, ma50=99, price=79))
    assert [e[0] for e in dn] == ["cross", "low"] and dn[0][1] == "bear"
    k, tone, title, detail = c.fund_event("X", c.parse_companyfacts(facts()), {"form": "10-K", "filed": "2026-02-01"})
    assert tone == "bull" and "$570" in title and "+36% YoY" in title, title


def test_db_and_api():
    c.db_init()
    c.fetch_universe = lambda: ([{"symbol": "GOOD", "name": "Good Co", "sector": "Information Technology",
                                  "industry": "Software", "cik": 1, "in_sp": 1, "in_ndx": 1},
                                 {"symbol": "OLD", "name": "Old Co", "sector": "Energy", "industry": "", "cik": 2,
                                  "in_sp": 1, "in_ndx": 0}], "test")
    assert c.refresh_universe(verbose=False) == 2
    assert c.api_events() == []                       # first fill is quiet
    g = good()
    c.save_fields("GOOD", {k: g[k] for k in ("price", "price_date", "chg_1d", "ret_3m", "ret_1y", "ma50", "ma200",
                                              "ma50_slope", "hi52", "lo52", "pos52", "drawdown", "trend",
                                              "trend_tone", "chart", "spark", "fund", "metrics", "analysts")})
    assert c.rescore(emit=True) == 2
    d = c.api_list()
    by = {x["s"]: x for x in d["stocks"]}
    assert by["GOOD"]["ti"] == "Top rated" and by["GOOD"]["rd"] == "bull" and by["OLD"]["ti"] == "Building"
    assert by["GOOD"]["pt"]["trend"] >= 90 and d["building"] is False and "not investment advice" in d["disclaimer"]
    s = c.api_stock("good")
    assert s["symbol"] == "GOOD" and s["chart"]["c"] and s["val"]["pe"] == 18 and len(s["parts"]) == 8
    assert c.api_stock("NOPE") is None
    # a tier change on the next pass becomes an update; a dropped name goes inactive
    c.save_fields("GOOD", {"analysts": {"score": 1.2, "label": "Strong sell", "analysts": 30, "buy_pct": 0},
                           "fund": dict(g["fund"], net_margin=-0.3, fcf=-10, fcf_margin=-0.2, roe=-0.2,
                                        revenue_cagr3=-0.1, q_revenue_yoy=-0.2, eps_yoy=-0.5)})
    c.rescore()
    ev = c.api_events(symbol="GOOD")
    assert ev and ev[0]["kind"] == "tier" and ev[0]["tone"] == "bear", ev
    c.fetch_universe = lambda: ([{"symbol": "GOOD", "name": "Good Co", "sector": "", "industry": "", "cik": 1,
                                  "in_sp": 1, "in_ndx": 1},
                                 {"symbol": "NEWB", "name": "Newbie", "sector": "", "industry": "", "cik": 3,
                                  "in_sp": 1, "in_ndx": 0}], "test")
    c.refresh_universe(verbose=False)
    assert c.active_symbols() == ["GOOD", "NEWB"]
    assert c.load_stock("GOOD")["sector"] == "Information Technology"      # blanks never erase
    assert any(e["kind"] == "index" and e["symbol"] == "NEWB" for e in c.api_events())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL COMPASS TESTS PASSED")

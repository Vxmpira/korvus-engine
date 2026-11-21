"""A deploy takes effect at once (the saved calendar from older code is not
trusted as fresh), and a Forex Factory outage does not freeze the actuals.
Run: python tests/test_calendar_fresh.py"""
import os, sys, json, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import korvus_calendar as k

WEEK = [{"title": "Philly Fed Manufacturing Index", "country": "USD", "impact": "Medium",
         "date": "2020-11-19T08:30:00-05:00", "forecast": "20.6", "previous": "20.9", "actual": ""}]
FMP = [{"title": "Philadelphia Fed Manufacturing Index (Nov)", "country": "USD",
        "date": "2020-11-19T13:30:00+00:00", "actual": 6.3, "forecast": 20.6, "previous": 20.9}]


def _write(sig):
    f = os.path.join(tempfile.mkdtemp(prefix="cal_"), "calendar_cache.json")
    json.dump({"data": WEEK, "ts": time.time(), "ttl": 300, "provider": "merge", "sig": sig}, open(f, "w"))
    k._CACHE_FILE = f
    k._cache.update({"data": [], "ts": 0.0, "ttl": 300, "provider": ""})
    k._load_disk_cache()


def test_old_build_cache_is_expired_but_kept():
    _write("someoldbuild")
    assert k._cache["data"] == WEEK and k._cache["ts"] == 0.0      # fallback kept, not trusted as fresh
    _write(k._SIG)
    assert k._cache["ts"] > 0                                      # same build: normal TTL


def test_ff_outage_still_fills_actuals():
    _write(k._SIG)
    k._sub.update({"ff": {"data": None, "ts": 0.0}, "fmp": {"data": None, "ts": 0.0}, "te": {"data": None, "ts": 0.0}})
    real = (k._forexfactory, k._fmp, k._tradingeconomics)
    k._forexfactory, k._fmp, k._tradingeconomics = (lambda: []), (lambda: FMP), (lambda: [])
    try:
        out = k._merge()
    finally:
        k._forexfactory, k._fmp, k._tradingeconomics = real
    assert out[0]["title"] == "Philly Fed Manufacturing Index" and out[0]["actual"] == "6.3", out
    assert WEEK[0]["actual"] == ""                                 # the saved copy is not mutated


def test_auto_fills_blanks_when_a_provider_key_is_set():
    """The page and the `usd` check must use the same mode."""
    calls = []
    real = (k._merge, k._forexfactory, k.CALENDAR_PROVIDER, k.FMP_KEY, k.TE_KEY, k._save_disk_cache)
    k._merge = lambda: (calls.append("merge") or [dict(WEEK[0])])
    k._forexfactory = lambda: (calls.append("ff") or [dict(WEEK[0])])
    k._save_disk_cache = lambda: None
    try:
        k.CALENDAR_PROVIDER, k.FMP_KEY, k.TE_KEY = "auto", "key", ""
        k.get_calendar(force_refresh=True)
        k.FMP_KEY = ""
        k.get_calendar(force_refresh=True)
    finally:
        k._merge, k._forexfactory, k.CALENDAR_PROVIDER, k.FMP_KEY, k.TE_KEY, k._save_disk_cache = real
    assert calls == ["merge", "ff"], calls


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_") and callable(fn):
            fn(); print("ok ", n)
    print("ALL CALENDAR FRESH TESTS PASSED")

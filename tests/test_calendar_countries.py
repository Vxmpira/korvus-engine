"""Non-USD actuals: Forex Factory names the euro-area country in the title, the
provider puts it in a field. Also near-name matching must try every plausible
row, and "core retail sales" must match in any period.
Run: python tests/test_calendar_countries.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import korvus_calendar as k

T = "2026-11-23T08:30:00+00:00"
def row(title, cc, ccy, act, fc, pv, t=T):
    return {"title": title, "country": ccy, "cc": cc, "date": t, "actual": act, "forecast": fc, "previous": pv}
FMP = [
    row("HCOB Manufacturing PMI Flash (Nov)", "FR", "EUR", 48.1, 48.5, 48.8),      # listed first on purpose
    row("HCOB Manufacturing PMI Flash (Nov)", "DE", "EUR", 53.6, 54.1, 54.2),
    row("HCOB Manufacturing PMI Flash (Nov)", "EU", "EUR", 50.9, 51.0, 51.2),
    row("Retail Sales Ex Autos QoQ (Q3)", "NZ", "NZD", 1.1, 0.8, 0.7, "2026-11-22T21:45:00+00:00"),
    row("Retail Sales QoQ (Q3)", "NZ", "NZD", 0.9, 0.6, 0.5, "2026-11-22T21:45:00+00:00"),
    row("Something Sales Index", "GB", "GBP", 9.0, 1.0, 1.0),                       # shares a word, wrong numbers
    row("Shop Sales Volume", "GB", "GBP", 0.4, 0.2, 0.1),
]
def ff(title, ccy, fc, pv, d="2026-11-23T03:30:00-05:00"):
    return {"title": title, "country": ccy, "date": d, "forecast": fc, "previous": pv, "impact": "Medium"}


def test_euro_area_countries():
    idx = k._index_rows(FMP)
    assert k._lookup_actual(ff("German Flash Manufacturing PMI", "EUR", "54.1", "54.2"), FMP, idx) == "53.6"
    assert k._lookup_actual(ff("French Flash Manufacturing PMI", "EUR", "48.5", "48.8"), FMP, idx) == "48.1"
    assert k._lookup_actual(ff("Flash Manufacturing PMI", "EUR", "51.0", "51.2"), FMP, idx) == "50.9"
    # a country the provider has no row for stays blank, it never borrows a neighbour's number
    assert k._lookup_actual(ff("Italian Flash Manufacturing PMI", "EUR", "49.0", "49.3"), FMP, idx) is None
    assert k._nation("German ifo Business Climate") == ("DE", "ifo Business Climate")
    assert k._nation("Retail Sales m/m") == (None, "Retail Sales m/m")


def test_core_retail_any_period():
    idx = k._index_rows(FMP)
    e = ff("Core Retail Sales q/q", "NZD", "0.8%", "0.7%", "2026-11-22T16:45:00-05:00")
    assert k._lookup_actual(e, FMP, idx) == "1.1%"
    assert k._lookup_actual(dict(e, title="Retail Sales q/q", forecast="0.6%", previous="0.5%"), FMP, idx) == "0.9%"


def test_near_names_try_every_row():
    idx = k._index_rows(FMP)
    # "Something Sales Index" overlaps too but its numbers disagree; the second row is the release
    assert k._lookup_actual(ff("Retail Sales Volume", "GBP", "0.2%", "0.1%"), FMP, idx) == "0.4%"


def test_rows_without_country_field_still_work():
    rows = [dict(r) for r in FMP]
    for r in rows: r.pop("cc")
    idx = k._index_rows(rows)
    assert k._lookup_actual(ff("Core Retail Sales q/q", "NZD", "0.8%", "0.7%", "2026-11-22T16:45:00-05:00"), rows, idx) == "1.1%"


def test_fmp_week_starts_sunday():
    import datetime as dt
    # Mon 23 Nov 2026: the week opened Sun 22 Nov (NZD retail sales, 21:45 UTC)
    assert k._fmp_window(dt.date(2026, 11, 23)) == (dt.date(2026, 11, 22), dt.date(2026, 11, 29))
    assert k._fmp_window(dt.date(2026, 11, 28)) == (dt.date(2026, 11, 22), dt.date(2026, 11, 29))   # Saturday
    assert k._fmp_window(dt.date(2026, 11, 22)) == (dt.date(2026, 11, 22), dt.date(2026, 11, 29))   # Sunday itself
    assert k._fmp_window(dt.date(2026, 11, 29)) == (dt.date(2026, 11, 29), dt.date(2026, 12, 6))


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_") and callable(fn):
            fn(); print("ok ", n)
    print("ALL CALENDAR COUNTRY TESTS PASSED")

"""Actuals that used to stay blank: Philly Fed and Core Retail Sales (names the
two feeds spell differently), plus the name-free fallback that matches a
release by currency + time + forecast + previous, and the missing-actual log.
Run: python tests/test_calendar_gaps.py"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import korvus_calendar as k

T = "2026-11-19T13:30:00+00:00"
FMP = [
    {"title": "Philadelphia Fed Manufacturing Index (Nov)", "country": "USD", "date": T, "actual": 18.4, "forecast": 20.6, "previous": 20.9},
    {"title": "Retail Sales Ex Autos MoM (Oct)", "country": "USD", "date": T, "actual": 0.3, "forecast": 0.4, "previous": 0.9},
    {"title": "Retail Sales MoM (Oct)", "country": "USD", "date": T, "actual": 0.2, "forecast": 0.1, "previous": 0.8},
    # a release under a name nobody has an alias for
    {"title": "Some Brand New Survey Headline", "country": "USD", "date": T, "actual": 51.2, "forecast": 50.0, "previous": 49.5},
    # two different series that happen to share forecast/previous but not actual
    {"title": "Twin A", "country": "USD", "date": T, "actual": 1.1, "forecast": 7.0, "previous": 6.0},
    {"title": "Twin B", "country": "USD", "date": T, "actual": 2.2, "forecast": 7.0, "previous": 6.0},
    {"title": "Other Hour", "country": "USD", "date": "2026-11-19T15:00:00+00:00", "actual": 9.9, "forecast": 3.0, "previous": 2.0},
    {"title": "Other Currency", "country": "EUR", "date": T, "actual": 9.9, "forecast": 4.0, "previous": 5.0},
]


def ff(title, fc, pv, date="2026-11-19T08:30:00-05:00", ccy="USD", impact="High"):
    return {"title": title, "country": ccy, "date": date, "forecast": fc, "previous": pv, "impact": impact}


def test_named_gaps():
    idx = k._index_rows(FMP)
    assert k._lookup_actual(ff("Philly Fed Manufacturing Index", "20.6", "20.9"), FMP, idx) == "18.4"
    assert k._lookup_actual(ff("Core Retail Sales m/m", "0.4%", "0.9%"), FMP, idx) == "0.3%"
    assert k._lookup_actual(ff("Retail Sales m/m", "0.1%", "0.8%"), FMP, idx) == "0.2%"   # unchanged


def test_match_by_numbers():
    idx = k._index_rows(FMP)
    # no shared words at all: matched on currency + minute + forecast + previous
    assert k._lookup_actual(ff("Totally Different Label", "50.0", "49.5"), FMP, idx) == "51.2"
    # only one of the two numbers agrees: not enough
    assert k._lookup_actual(ff("Totally Different Label", "50.0", "11.1"), FMP, idx) is None
    # two rows share the pair but disagree on the actual: never guess
    assert k._lookup_actual(ff("Unknown Thing", "7.0", "6.0"), FMP, idx) is None
    # right numbers, wrong hour or wrong currency: no
    assert k._lookup_actual(ff("Unknown Thing", "3.0", "2.0"), FMP, idx) is None
    assert k._lookup_actual(ff("Unknown Thing", "4.0", "5.0"), FMP, idx) is None
    # FF has no forecast: nothing solid to match on
    assert k._lookup_actual(ff("Unknown Thing", "", "49.5"), FMP, idx) is None


def test_revised_reading_fills():
    """Seen live 2026-11-20: FMP 'Michigan 1 Year Inflation Expectations' = 3.7, FF
    'Revised UoM Inflation Expectations' has no forecast and previous 4.3% (the
    prelim), while FMP's previous is last month's final."""
    t = "2026-11-20T15:00:00+00:00"
    rows = [{"title": "Michigan 1 Year Inflation Expectations (Nov)", "country": "USD", "date": t, "actual": 3.7, "forecast": None, "previous": 4.6},
            {"title": "Michigan 5 Year Inflation Expectations (Nov)", "country": "USD", "date": t, "actual": 3.3, "forecast": None, "previous": 3.9}]
    idx = k._index_rows(rows)
    e = ff("Revised UoM Inflation Expectations", "", "4.3%", date="2026-11-20T10:00:00-05:00", impact="Medium")
    assert k._lookup_actual(e, rows, idx) == "3.7%"
    # the same clash on a non-revised release, or at another time, is still rejected
    assert k._lookup_actual(ff("UoM Inflation Expectations", "", "4.3%", date="2026-11-20T10:00:00-05:00"), rows, idx) is None
    assert k._lookup_actual(dict(e, date="2026-11-20T12:00:00-05:00"), rows, idx) is None


def test_missing_is_reported():
    now = dt.datetime.now(dt.timezone.utc)
    old = (now - dt.timedelta(hours=3)).isoformat()
    soon = (now + dt.timedelta(hours=3)).isoformat()
    evs = [ff("Released No Actual", "1.0", "2.0", date=old),
           ff("Not Out Yet", "1.0", "2.0", date=soon),
           ff("Fed Chair Speaks", "", "", date=old),
           ff("Low Impact", "1.0", "2.0", date=old, impact="Low"),
           dict(ff("Has Actual", "1.0", "2.0", date=old), actual="1.5")]
    out = k._note_missing(evs)
    assert [m["title"] for m in out] == ["Released No Actual"], out
    assert k.MISSING_ACTUALS == out


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_") and callable(fn):
            fn(); print("ok ", n)
    print("ALL CALENDAR GAP TESTS PASSED")

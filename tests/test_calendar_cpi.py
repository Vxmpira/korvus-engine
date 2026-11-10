"""CPI rows get their Actual: Forex Factory says "CPI", FMP says "Inflation Rate".
Run: python tests/test_calendar_cpi.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import korvus_calendar as k

FMP = [
    {"title": "Core Inflation Rate MoM (Oct)", "country": "USD", "date": "2026-11-10T13:30:00+00:00", "actual": 0.3, "forecast": 0.3, "previous": 0.2},
    {"title": "Inflation Rate MoM (Oct)", "country": "USD", "date": "2026-11-10T13:30:00+00:00", "actual": 0.4, "forecast": 0.3, "previous": 0.3},
    {"title": "Inflation Rate MoM (Oct)", "country": "USD", "date": "2026-11-11T02:45:00+00:00", "actual": None},
    {"title": "Inflation Rate YoY (Oct)", "country": "USD", "date": "2026-11-10T13:30:00+00:00", "actual": None},
    {"title": "Core CPI (Oct)", "country": "USD", "date": "2026-11-10T13:30:00+00:00", "actual": 339.9},
    {"title": "CPI (Oct)", "country": "USD", "date": "2026-11-10T13:30:00+00:00", "actual": 337.51},
]


def ff(title, fc, pv):
    return {"title": title, "country": "USD", "date": "2026-11-10T08:30:00-05:00", "forecast": fc, "previous": pv}


def test_cpi():
    assert k._canon("Core CPI m/m") == "core inflation rate mom" == k._canon("Core Inflation Rate MoM (Oct)")
    assert k._canon("CPI y/y") == "inflation rate yoy" and k._canon("Core CPI y/y") == "core inflation rate yoy"
    idx = k._index_rows(FMP)
    assert k._lookup_actual(ff("Core CPI m/m", "0.3%", "0.2%"), FMP, idx) == "0.3%"
    assert k._lookup_actual(ff("CPI m/m", "0.3%", "0.3%"), FMP, idx) == "0.4%"
    # no actual published yet: stays blank, and the index level (337.51) never leaks in
    assert k._lookup_actual(ff("CPI y/y", "3.0%", "3.0%"), FMP, idx) is None


if __name__ == "__main__":
    test_cpi(); print("ok  test_cpi"); print("ALL CALENDAR CPI TESTS PASSED")

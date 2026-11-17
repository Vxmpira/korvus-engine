"""The signup trap swallows scripted submits and lets a person through when a
browser or password manager filled the hidden field with their own details."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import korvus_server as ks  # noqa: E402


def check(form):
    ks._rl_hits.clear()
    with ks.app.test_request_context("/signup", method="POST", data=form):
        return ks._signup_bot_check()


def stamp(seconds_ago):
    return str(int((time.time() - seconds_ago) * 1000))


BASE = {"username": "trader1", "email": "t@example.com", "password": "longenough1"}


def test_clean_human_submit_passes():
    assert check({**BASE, "form_ts": stamp(20)}) is None


def test_no_script_is_refused_with_a_visible_error():
    assert check(dict(BASE))  # non-empty message


def test_trap_with_no_script_is_swallowed():
    assert check({**BASE, "hp_field": "x"}) == ""


def test_trap_sent_instantly_is_swallowed():
    assert check({**BASE, "hp_field": "trader1", "form_ts": stamp(1)}) == ""


def test_trap_with_junk_at_human_pace_is_swallowed():
    assert check({**BASE, "hp_field": "http://spam.example", "form_ts": stamp(20)}) == ""


def test_trap_autofilled_with_own_username_or_email_passes():
    assert check({**BASE, "hp_field": "Trader1", "form_ts": stamp(20)}) is None
    assert check({**BASE, "hp_field": "t@example.com", "form_ts": stamp(20)}) is None
    assert check({**BASE, "hp_field": "longenough1", "form_ts": stamp(20)}) is None


def test_trap_causes_are_counted(monkeypatch):
    seen = []
    monkeypatch.setattr(ks.traffic, "record", lambda kind, key: seen.append((kind, key)))
    check({**BASE, "hp_field": "x"})
    check({**BASE, "hp_field": "trader1", "form_ts": stamp(1)})
    check({**BASE, "hp_field": "junk", "form_ts": stamp(20)})
    check({**BASE, "hp_field": "trader1", "form_ts": stamp(20)})
    assert [k for _, k in seen] == ["hp_no_script", "hp_instant", "hp_junk", "hp_autofill"]

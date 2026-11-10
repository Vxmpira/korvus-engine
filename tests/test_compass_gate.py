"""Compass is Operator-only: the page redirects free members to /upgrade and
every /api/compass/* route returns 403 for them. Promo Studio is gone.
Run: python tests/test_compass_gate.py
"""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="gate_")
os.environ["ADMIN_USERNAMES"] = "boss"
import korvus_auth as auth
auth.DB_PATH = os.path.join(TMP, "users.db")
import korvus_server as srv
import korvus_compass as compass
compass.DB_PATH = os.path.join(TMP, "compass.db")
srv.app.config["TESTING"] = True


class U:
    is_authenticated = True
    def __init__(self, name, tier): self.username, self.tier, self.id = name, tier, 1


def as_user(user):
    """Test client whose requests run as `user` (None = logged out)."""
    import flask_login.utils as flu
    srv.app.login_manager._request_callback = None
    srv.app.login_manager._user_callback = lambda uid: user
    c = srv.app.test_client()
    if user:
        with c.session_transaction() as s:
            s["_user_id"] = "1"; s["_fresh"] = True
    return c


def test_gate():
    out = as_user(None)
    assert out.get("/compass").status_code in (302, 401)
    assert out.get("/api/compass/list").status_code == 401
    free = as_user(U("joe", "free"))
    r = free.get("/compass")
    assert r.status_code == 302 and r.headers["Location"].endswith("/upgrade?from=compass"), r.headers
    for path in ("/api/compass/list", "/api/compass/stock/AAPL", "/api/compass/events"):
        r = free.get(path)
        assert r.status_code == 403 and r.get_json()["error"] == "operator_required", path
    pro = as_user(U("ann", "pro"))
    assert pro.get("/compass").status_code == 200
    r = pro.get("/api/compass/list")
    assert r.status_code == 200 and "stocks" in r.get_json() and "disclaimer" in r.get_json()
    assert pro.get("/api/compass/stock/NOPE").status_code == 404
    assert pro.get("/api/compass/events").get_json() == {"events": []}
    assert as_user(U("boss", "free")).get("/compass").status_code == 200      # owner
    # owner previewing as free is locked out like any free member
    boss = as_user(U("boss", "free"))
    with boss.session_transaction() as s:
        s["korvus_free_preview"] = True
    assert boss.get("/api/compass/list").status_code == 403


def test_promo_gone():
    rules = [str(r) for r in srv.app.url_map.iter_rules()]
    assert not [r for r in rules if "agent" in r or "promo" in r], rules
    assert as_user(U("boss", "pro")).get("/agent").status_code != 200   # unknown paths bounce home
    for f in ("agent.html", "korvus_promo_api.py", "korvus_promo_studio.html", "korvus_fill_api.py"):
        assert not os.path.exists(os.path.join(ROOT, f)), f
    dash = open(os.path.join(ROOT, "korvus_dashboard.html"), encoding="utf-8").read()
    assert "promoBtn" not in dash and "/agent" not in dash and "compassBtn" in dash and "/upgrade?from=compass" in dash


if __name__ == "__main__":
    test_gate(); print("ok  test_gate")
    test_promo_gone(); print("ok  test_promo_gone")
    print("ALL GATE TESTS PASSED")

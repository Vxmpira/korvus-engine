"""The shared Korvus look: every page links the one stylesheet, the server
serves it (and the article one) as CSS without a login, article pages under
/p/ get it added when served, and no page still asks for the retired fonts.
Run: python tests/test_theme.py
"""
import os, re, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="theme_")
import korvus_auth as auth
auth.DB_PATH = os.path.join(TMP, "users.db")
import korvus_server as srv
srv.app.config["TESTING"] = True

PAGES = {"korvus_login.html": "auth", "korvus_signup.html": "auth", "korvus_forgot.html": "auth",
         "korvus_reset.html": "auth", "korvus_account.html": "account", "korvus_upgrade.html": "upgrade",
         "korvus_lodestone.html": "lodestone", "korvus_legal.html": "legal", "korvus_dashboard.html": "terminal",
         "korvus_compass.html": "compass", "korvus_forex_calendar.html": "forex", "admin.html": "admin"}


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_every_page_uses_the_shared_look():
    for name, key in PAGES.items():
        s = read(name)
        assert s.count("/korvus-theme.css") == 1, name
        # the link must come after the page's own styles so the shared look wins
        assert s.index("/korvus-theme.css") > s.rindex("</style>"), name
        assert f'<html lang="en" data-k="{key}"' in s, name
        assert "Fraunces" in s and "Hanken+Grotesk" in s, name
        assert "Bricolage" not in s and "IBM+Plex" not in s and "IBM Plex" not in s, name


def test_stylesheets_are_public_css():
    c = srv.app.test_client()                      # logged out
    for path in ("/korvus-theme.css", "/korvus-article.css"):
        r = c.get(path)
        assert r.status_code == 200 and r.mimetype == "text/css", path
        assert b"Fraunces" in r.data and b"#F43F8F" in r.data, path


def test_theme_covers_every_page_key():
    css = read("korvus_theme.css")
    for key in set(PAGES.values()):
        assert f'html[data-k="{key}"]' in css, key
    # the generated candle backdrop is kept, not hidden
    hidden = re.findall(r"([^{}]+)\{[^{}]*display:none[^{}]*\}", css)
    assert not any("#candles" in h or ".chartscape" in h for h in hidden)


def test_articles_get_the_look_once():
    c = srv.app.test_client()
    names = sorted(f[:-5] for f in os.listdir(os.path.join(ROOT, "pages")) if f.endswith(".html"))
    assert names
    for n in names[:6]:
        r = c.get("/p/" + n)
        assert r.status_code == 200, n
        body = r.get_data(as_text=True)
        assert body.count("/korvus-article.css") == 1, n
        assert body.index("/korvus-article.css") < body.index("</head>"), n
    assert c.get("/p/no-such-page").status_code == 404
    assert c.get("/p/Bad_Name").status_code == 404


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("ALL THEME TESTS PASSED")

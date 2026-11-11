"""Asia and London desks on the terminal: every regional feed is tagged, and
/api/news?region= narrows to that session's sources before the page limit.
No network.  Run: python tests/test_news_region.py"""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="region_")
import korvus_engine as eng
eng.DB_PATH = os.path.join(TMP, "korvus.db")
import korvus_server as srv
srv.DB_PATH = eng.DB_PATH
srv.app.config["TESTING"] = True


class U:
    is_authenticated = True
    username, tier, id = "ann", "pro", 1


def client():
    srv.app.login_manager._request_callback = None
    srv.app.login_manager._user_callback = lambda uid: U()
    c = srv.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"; s["_fresh"] = True
    return c


def test_feeds_tagged():
    names = [f[0] for f in eng.RSS_FEEDS]
    assert len(names) == len(set(names))
    assert set(eng.FEED_REGION) <= set(names)                       # every tagged source is a real feed
    by = lambda r: {n for n, x in eng.FEED_REGION.items() if x == r}
    assert len(by("asia")) >= 4 and len(by("london")) >= 6 and {"Bank of Japan"} <= by("asia") and {"Bank of England", "ECB"} <= by("london")
    assert all(u.startswith("https://") for f in eng.RSS_FEEDS for u in (f[1] if isinstance(f[1], tuple) else (f[1],)))
    assert srv._feed_region("Nikkei Asia") == "asia" and srv._feed_region("CNBC Markets") == "" and srv._feed_region(None) == ""


def test_region_query():
    eng.db_init()
    conn = eng.db_connect()
    def add(i, src):
        eng.insert_raw_item(conn, {"id": f"id{i}", "source": "wire", "source_name": src, "url": "u", "headline": f"h{i}",
                                   "raw_text": "t", "category": "general", "published_at": ""})
        conn.execute("UPDATE items SET processed=1, created_at=? WHERE id=?", (f"2026-11-10T{i // 60:02d}:{i % 60:02d}:00", f"id{i}"))
    for i in range(5):
        add(i, "Nikkei Asia")                                       # oldest: five Asia items
    for i in range(5, 8):
        add(i, "City AM")
    for i in range(8, 78):
        add(i, "CNBC Markets")                                      # 70 newer US items push them off the first page
    conn.commit(); conn.close()
    c = client()
    allv = c.get("/api/news").get_json()["items"]
    assert len(allv) == 60 and all(x["region"] == "" for x in allv)
    asia = c.get("/api/news?region=asia").get_json()["items"]
    assert len(asia) == 5 and {x["region"] for x in asia} == {"asia"} and {x["source_name"] for x in asia} == {"Nikkei Asia"}
    lon = c.get("/api/news?region=london").get_json()["items"]
    assert len(lon) == 3 and {x["region"] for x in lon} == {"london"}
    assert len(c.get("/api/news?region=mars").get_json()["items"]) == 60     # unknown region: ignored
    # the SEC chip asks the server for filings only, so busy wires can never crowd them off the page
    conn = eng.db_connect()
    eng.insert_raw_item(conn, {"id": "f1", "source": "filing", "source_name": "SEC 8-K \u00b7 AAPL", "url": "u",
                               "headline": "Apple 8-K", "raw_text": "t", "category": "general", "published_at": ""})
    conn.execute("UPDATE items SET processed=1, created_at='2026-11-09T00:00:00' WHERE id='f1'")
    conn.commit(); conn.close()
    assert not [x for x in c.get("/api/news").get_json()["items"] if x["source"] == "filing"]   # older than the latest 60
    sec = c.get("/api/news?only=filing").get_json()["items"]
    assert len(sec) == 1 and sec[0]["source"] == "filing"


def test_feed_formats():
    """RSS 2.0, namespaced RSS 1.0 and Atom all parse (Nikkei returned 0 items before)."""
    import types
    feeds = {
        "rss2": b"<rss><channel><item><title>A</title><link>http://a</link><description>d</description><pubDate>p</pubDate></item></channel></rss>",
        "rss1": b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>B</title><link>http://b</link><dc:date>2026</dc:date></item></rdf:RDF>',
        "atom": b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>C</title><link href="http://c"/><summary>s</summary><updated>u</updated></entry></feed>',
    }
    old_feeds, old_get = eng.RSS_FEEDS, eng.requests.get
    eng.RSS_FEEDS = [(k, "https://x/" + k, "general") for k in feeds]
    eng.requests.get = lambda u, **kw: types.SimpleNamespace(status_code=200, content=feeds[u.rsplit("/", 1)[-1]])
    try:
        out = eng.fetch_rss()
    finally:
        eng.RSS_FEEDS, eng.requests.get = old_feeds, old_get
    assert [(x["headline"], x["url"]) for x in out] == [("A", "http://a"), ("B", "http://b"), ("C", "http://c")], out
    assert out[1]["published_at"] == "2026" and out[2]["raw_text"] == "s"


if __name__ == "__main__":
    test_feeds_tagged(); print("ok  test_feeds_tagged")
    test_region_query(); print("ok  test_region_query")
    test_feed_formats(); print("ok  test_feed_formats")
    print("ALL NEWS REGION TESTS PASSED")

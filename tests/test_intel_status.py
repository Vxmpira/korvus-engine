"""Live-status path: a filings poll on EDGAR fixtures records stage stats and
events, the flusher writes intel_status.json, and /api/feed/intel-status
serves it only with the shared key. Offline. Run: python tests/test_intel_status.py"""
import os, sys, json, shutil, tempfile, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
TMP = tempfile.mkdtemp(prefix="intel_status_")
import korvus_intel as intel, korvus_intel_status as status, korvus_edgar as edgar
import test_edgar as F
intel.DB_PATH = os.path.join(TMP, "korvus_intel.db")
status.STATUS_PATH = os.path.join(TMP, "intel_status.json")


def test_poll_records_status():
    intel.db_init()
    c = intel.db()
    c.execute("INSERT INTO universe (symbol,name,cik,price,asof) VALUES ('NVDA','NVIDIA',1045810,99,?)",
              (intel.utcnow().isoformat(),))
    c.commit(); c.close()
    def fake_get(url, timeout=15):
        if "getcurrent" in url:
            return F.FEED if "type=8-K" in url else ""
        if url.endswith("-index.htm"):
            return F.INDEX
        return F.EX991
    edgar._get = fake_get
    edgar.time.sleep = lambda s: None
    edgar._fresh = lambda e: True
    intel.quote = lambda s: {"price": 100.0, "prev_close": 99.0, "chg_pct": 1, "open": 0, "high": 0, "low": 0}
    intel.score_filing = lambda *a, **k: {"summary": "s", "read": "r", "catalyst": "earnings",
                                           "impact": "high", "direction": "bull", "confidence": 80}
    n = intel.poll_filings(verbose=False)
    assert n == 1, n
    snap = status.snapshot()
    e = snap["stages"]["edgar"]
    assert e["polls_today"] == 1 and e["scanned_today"] == 3 and e["new_today"] == 1, e
    assert e["feeds_ok"] == 1 and e["feeds"] == 2 and "6-K" in e["last_error"], e   # 6-K feed empty -> flagged by name
    assert snap["stages"]["scoring"]["scored_today"] == 1
    kinds = [(x["stage"], x["level"]) for x in snap["events"]]
    assert ("edgar", "info") in kinds and ("scoring", "info") in kinds and ("edgar", "error") in kinds, kinds
    assert intel.poll_filings(verbose=False) == 0                         # already seen
    assert status.snapshot()["stages"]["edgar"]["polls_today"] == 2
    status.flush(force=True)
    assert json.load(open(status.STATUS_PATH))["stages"]["edgar"]["new_today"] == 1


def test_feed_retry():
    calls = []
    def flaky(url, timeout=15):
        calls.append(timeout)
        if len(calls) == 1:
            edgar._tls.err = "timed out after 30s"
            return None
        return "<feed/>"
    old, edgar._get = edgar._get, flaky
    try:
        assert edgar.fetch_feed("6-K") == "<feed/>" and calls == [edgar.FEED_TIMEOUT] * 2, calls
    finally:
        edgar._get = old


def test_day_rollover():
    status._state["day_et"] = "2000-01-01"
    status.record("edgar", inc_polls_today=1)
    assert status.snapshot()["stages"]["edgar"]["polls_today"] == 1       # reset then +1


def test_endpoint_key():
    import korvus_server as srv
    srv.HERE = TMP
    os.environ["INTEL_STATUS_KEY"] = "k" * 32
    cl = srv.app.test_client()
    assert cl.get("/api/feed/intel-status").status_code == 403
    assert cl.get("/api/feed/intel-status", headers={"X-Intel-Key": "wrong"}).status_code == 403
    r = cl.get("/api/feed/intel-status", headers={"X-Intel-Key": "k" * 32})
    d = r.get_json()
    assert r.status_code == 200 and d["running"] is True and d["counts"]["universe"] == 1, d
    assert d["counts"]["filings_total"] == 1 and len(d["events"]) >= 3


def _submissions(acc, form, accepted, items="2.02,9.01"):
    return json.dumps({"cik": "1045810", "name": "NVIDIA CORP", "filings": {"recent": {
        "accessionNumber": [acc, "0001045810-25-000001", "0001045810-26-000060"],
        "form": [form, "8-K", "10-Q"],
        "acceptanceDateTime": [accepted, "2025-01-02T16:00:00.000Z", accepted],
        "filingDate": [accepted[:10], "2025-01-02", accepted[:10]],
        "items": [items, "8.01", ""]}}})


def test_recent_filings():
    """A company's own filing list: only the watched forms, only since the gap
    (with slack for EDGAR's Eastern-time stamps), items split, index URL built."""
    now = dt.datetime.now(dt.timezone.utc)
    stamp = (now - dt.timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old, edgar._get = edgar._get, lambda url, timeout=15: _submissions("0001045810-26-000073", "8-K", stamp)
    try:
        got = edgar.recent_filings(1045810, now - dt.timedelta(hours=1))
    finally:
        edgar._get = old
    assert [e["accession"] for e in got] == ["0001045810-26-000073"], got        # the old 8-K and the 10-Q are not watched
    e = got[0]
    assert e["items"] == ["2.02", "9.01"] and e["cik"] == 1045810 and e["company"] == "NVIDIA CORP"
    assert e["url"] == "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/0001045810-26-000073-index.htm"
    edgar._get = lambda url, timeout=15: None
    try:
        assert edgar.recent_filings(1045810, now) is None
    finally:
        edgar._get = old


def test_outage_catch_up():
    """The live feed goes down: the gap opens, every watched company's own
    filing list is read, the missed filing is scored and stamped as seen when
    it was filed. When the feed returns the gap closes once, with no double
    count."""
    intel.DB_PATH = os.path.join(TMP, "gap.db")
    intel.db_init()
    c = intel.db()
    c.execute("INSERT INTO universe (symbol,name,cik,price,asof) VALUES ('NVDA','NVIDIA',1045810,99,?)", (intel.utcnow().isoformat(),))
    c.commit(); c.close()
    intel.GAP_SWEEP_AFTER_MIN = 0
    intel.GAP_SWEEP_EVERY_MIN = 0
    intel._last_full[0] = intel.utcnow() - dt.timedelta(minutes=50)
    intel._gap["open"] = False
    up = [False]
    stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    calls = {"list": 0}
    def fake_get(url, timeout=15):
        if "getcurrent" in url:
            if not up[0]:
                edgar._tls.err = "timed out after 30s"
                return None
            return F.FEED if "type=8-K" in url else F.FEED.replace("8-K", "6-K")
        if "data.sec.gov/submissions" in url:
            calls["list"] += 1
            return _submissions("0001045810-26-000073", "8-K", stamp)
        if url.endswith("-index.htm"):
            return F.INDEX
        return F.EX991
    edgar._get = fake_get
    edgar._fresh = lambda e: False                                     # everything on the live page is old by now
    intel.quote = lambda s: {"price": 100.0, "prev_close": 99.0, "chg_pct": 1, "open": 0, "high": 0, "low": 0}
    intel.score_filing = lambda *a, **k: {"summary": "s", "read": "r", "catalyst": "earnings",
                                           "impact": "high", "direction": "bull", "confidence": 80}
    assert intel.poll_filings(verbose=False) == 1                       # down: the filing list catches it
    assert intel._gap["open"] and calls["list"] == 1
    c = intel.db()
    f = dict(c.execute("SELECT * FROM filings WHERE accession='0001045810-26-000073'").fetchone())
    c.close()
    assert f["scored"] == 1 and f["impact"] == "high" and f["seen_utc"] == f["filed_utc"], f   # stamped late, not fresh
    assert any("found late" in x["msg"] for x in status.snapshot()["events"])
    up[0] = True
    assert intel.poll_filings(verbose=False) == 0                       # back: nothing new, no double count
    assert not intel._gap["open"] and calls["list"] == 2
    c = intel.db()
    assert c.execute("SELECT COUNT(*) FROM filings WHERE accession='0001045810-26-000073'").fetchone()[0] == 1
    c.close()
    e = status.snapshot()["stages"]["edgar"]
    assert e["last_gap_found"] == 1 and e["feeds_ok"] == 2 and e["last_recovered_utc"], e
    assert any("answering again" in x["msg"] for x in status.snapshot()["events"])
    assert intel.poll_filings(verbose=False) == 0 and calls["list"] == 2   # healthy: no more list reads


def test_short_blip_needs_no_sweep():
    """A single missed poll does not cost 85 filing-list reads; the live page
    still covers it."""
    intel.GAP_SWEEP_AFTER_MIN = 5
    intel._gap["open"] = False
    calls = {"list": 0, "n": 0}
    def fake_get(url, timeout=15):
        if "getcurrent" in url:
            calls["n"] += 1
            if calls["n"] <= 2:
                edgar._tls.err = "timed out after 30s"
                return None
            return F.FEED
        if "data.sec.gov" in url:
            calls["list"] += 1
        return None
    edgar._get = fake_get
    intel.poll_filings(verbose=False)
    assert intel._gap["open"]
    intel.poll_filings(verbose=False)
    assert not intel._gap["open"] and calls["list"] == 0
    assert any("live page still covered the gap" in x["msg"] for x in status.snapshot()["events"])


def test_shared_feed():
    """The Korvus processes share one answer: a second ask inside the window
    reads the copy, and a feed that just failed is not asked again."""
    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text, self.encoding = code, text, "utf-8"
    class S:
        def __init__(self):
            self.n, self.fail = 0, False
        def get(self, url, timeout=15):
            self.n += 1
            if self.fail:
                raise edgar.requests.Timeout()
            return R(200, "<feed>%d</feed>" % self.n)
    old_s, old_dir = edgar._session, edgar.FEED_CACHE_DIR
    edgar._get = edgar._REAL_GET
    edgar._session = S()
    edgar.FEED_CACHE_DIR = os.path.join(TMP, "feedcache")
    try:
        assert edgar.fetch_feed("8-K") == "<feed>1</feed>" and edgar.fetch_feed("8-K") == "<feed>1</feed>"
        assert edgar._session.n == 1
        edgar._session.fail = True
        assert edgar.fetch_feed("6-K") is None and edgar._session.n == 3          # its own retry
        assert edgar.fetch_feed("6-K") is None and edgar._session.n == 3          # another process's failure, not re-asked
        assert "another Korvus process" in edgar.last_error()
        assert edgar.fetch_feed("8-K") == "<feed>1</feed>"
        os.utime(os.path.join(edgar.FEED_CACHE_DIR, "8-K.xml"), (0, 0))         # the copy goes stale
        edgar._session.fail = False
        assert edgar.fetch_feed("8-K") == "<feed>4</feed>"
    finally:
        edgar._session, edgar.FEED_CACHE_DIR = old_s, old_dir


if __name__ == "__main__":
    try:
        for n in ("test_poll_records_status", "test_feed_retry", "test_day_rollover", "test_endpoint_key",
                  "test_recent_filings", "test_outage_catch_up", "test_short_blip_needs_no_sweep", "test_shared_feed"):
            globals()[n](); print("ok", n)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

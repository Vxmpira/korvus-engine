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
    edgar._fresh = lambda e: True
    intel.quote = lambda s: {"price": 100.0, "prev_close": 99.0, "chg_pct": 1, "open": 0, "high": 0, "low": 0}
    intel.score_filing = lambda *a, **k: {"summary": "s", "read": "r", "catalyst": "earnings",
                                           "impact": "high", "direction": "bull", "confidence": 80}
    n = intel.poll_filings(verbose=False)
    assert n == 1, n
    snap = status.snapshot()
    e = snap["stages"]["edgar"]
    assert e["polls_today"] == 1 and e["scanned_today"] == 3 and e["new_today"] == 1, e
    assert e["feeds_ok"] == 1 and e["feeds"] == 2 and "last_error" in e   # 6-K feed empty -> flagged
    assert snap["stages"]["scoring"]["scored_today"] == 1
    kinds = [(x["stage"], x["level"]) for x in snap["events"]]
    assert ("edgar", "info") in kinds and ("scoring", "info") in kinds and ("edgar", "error") in kinds, kinds
    assert intel.poll_filings(verbose=False) == 0                         # already seen
    assert status.snapshot()["stages"]["edgar"]["polls_today"] == 2
    status.flush(force=True)
    assert json.load(open(status.STATUS_PATH))["stages"]["edgar"]["new_today"] == 1


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


if __name__ == "__main__":
    try:
        for n in ("test_poll_records_status", "test_day_rollover", "test_endpoint_key"):
            globals()[n](); print("ok", n)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

"""Crown routing: a local fake OpenRouter answers /api/v1/chat/completions like
the real one (OpenAI shape, optional <think> block). Checks crown mode,
fallback to Haiku when Crown is down, shadow mode, auth header, and the DB
columns. Offline. Run: python tests/test_crown_scorer.py"""
import os, sys, json, threading, tempfile, shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="crown_")
import korvus_intel as intel, korvus_intel_status as status
intel.DB_PATH = os.path.join(TMP, "i.db")
status.STATUS_PATH = os.path.join(TMP, "s.json")
SEEN = []
MODE = {"up": True, "think": True}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, self.headers.get("Authorization"), body))
        if not MODE["up"]:
            self.send_response(502); self.end_headers(); return
        text = ('{"summary":"Crown summary","read":"Crown read","catalyst":"earnings",'
                '"impact":"high","direction":"bull","confidence":77}')
        if MODE["think"]:
            text = "<think>let me reason {not json}</think>\nHere you go:\n" + text
        out = json.dumps({"id": "gen-1", "model": body["model"],
                          "choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(out)


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
intel.CROWN_BASE = f"http://127.0.0.1:{srv.server_port}/api/v1"
intel.CROWN_KEY = "crown-key"
intel.CROWN_MODEL = "meta-llama/llama-3.3-70b-instruct"
HAIKU = {"summary": "h", "read": "h", "catalyst": "earnings", "impact": "med", "direction": "bull", "confidence": 60}
intel.score_haiku = lambda *a, **k: dict(HAIKU)
ARGS = ("NVDA", "NVIDIA", "8-K", ["2.02"], "Revenue up 106%")


def test_crown_mode():
    intel.SCORER = "crown"
    d = intel.score_filing(*ARGS)
    assert d["scorer"] == "crown" and d["confidence"] == 77 and d["impact"] == "high", d
    path, auth, body = SEEN[-1]
    assert path == "/api/v1/chat/completions" and auth == "Bearer crown-key"
    assert body["model"] == "meta-llama/llama-3.3-70b-instruct" and body["temperature"] == 0.2
    sysm, user = body["messages"]
    assert sysm["role"] == "system" and "STRICT JSON" in sysm["content"] and "TICKER: NVDA" in user["content"]


def test_crown_down_falls_back():
    intel.SCORER = "crown"; MODE["up"] = False
    d = intel.score_filing(*ARGS)
    MODE["up"] = True
    assert d["scorer"] == "haiku" and d["confidence"] == 60, d
    snap = status.snapshot()
    assert "502" in snap["stages"]["crown"]["last_error"] and "OpenRouter" in snap["stages"]["crown"]["last_error"]
    assert any("Haiku read it instead" in e["msg"] for e in snap["events"])


def test_shadow_mode():
    intel.SCORER = "shadow"
    d = intel.score_filing(*ARGS)
    assert d["scorer"] == "haiku" and d["shadow"]["confidence"] == 77, d   # Haiku decides, Crown recorded
    st = status.snapshot()["stages"]["crown"]
    assert st["compared_today"] == 1 and st["agree_today"] == 0           # med vs high -> differs


def test_db_columns():
    intel.db_init(); intel.db_init()                                        # migration is idempotent
    cols = {r[1] for r in intel.db().execute("PRAGMA table_info(filings)")}
    assert {"scorer", "shadow_json"} <= cols


def test_not_configured_is_haiku():
    intel.SCORER = "crown"; key = intel.CROWN_KEY; intel.CROWN_KEY = ""
    n = len(SEEN)
    d = intel.score_filing(*ARGS)
    intel.CROWN_KEY = key
    assert d["scorer"] == "haiku" and len(SEEN) == n                        # never calls Crown without a key


if __name__ == "__main__":
    try:
        for n in ("test_crown_mode", "test_crown_down_falls_back", "test_shadow_mode", "test_db_columns",
                  "test_not_configured_is_haiku"):
            globals()[n](); print("ok", n)
    finally:
        srv.shutdown(); shutil.rmtree(TMP, ignore_errors=True)

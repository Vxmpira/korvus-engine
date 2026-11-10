"""SEC card -> Discord embed: shape, Discord limits, queued-post flow built
from the live row, legacy queue entries, and the --discord-test path.
A local fake webhook stands in for Discord. Offline.
Run: python tests/test_sec_discord.py"""
import os, sys, json, time, sqlite3, tempfile, shutil, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="secdisc_")
import korvus_edgar as ke
ke.DB_PATH = os.path.join(TMP, "korvus.db")
ke.STATE_FILE = os.path.join(TMP, "state.json")
POSTS = []


class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        POSTS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204); self.end_headers()


srv = HTTPServer(("127.0.0.1", 0), Hook)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ke.DISCORD_HOOK = f"http://127.0.0.1:{srv.server_port}/api/webhooks/1/x"

ROW = {
    "id": "f1", "source": "filing", "source_name": "SEC 6-K · TSM", "url": "https://www.sec.gov/Archives/x-index.htm",
    "published_at": "2026-11-10T13:41:38+00:00", "created_at": "2026-11-10T13:42:03+00:00",
    "headline": "TSM: REPORT OF FOREIGN PRIVATE ISSUER",
    "title": "TSMC Q3: Revenue NT$1.49T, EPS NT$29.43; Board Approves $40.9B 2027 Capex",
    "company": "TSMC", "summary": "TSMC reported Q3 revenue of NT$1.49 trillion.",
    "facts": json.dumps([{"label": "Q3 revenue", "value": "NT$1,494.24B", "change": ""},
                         {"label": "Diluted EPS", "value": "NT$29.43", "change": ""},
                         {"label": "2027 capex", "value": "US$40.93B", "change": ""},
                         {"label": "Dividend", "value": "NT$9.0/sh", "change": "+12.5%"}]),
    "key_points": json.dumps(["Capex guide signals sustained AI demand.", "Dividend raised."]),
    "watch": "ADR open versus the Taiwan close.", "impact": "med", "direction": "neut",
    "confidence": 65, "instruments": json.dumps(["TSM"]), "processed": 1,
}


def _total(e):
    return (len(e.get("title", "")) + len(e.get("description", "")) + len(e["author"]["name"])
            + len(e["footer"]["text"]) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"]))


def test_embed_shape():
    e = ke.build_sec_embed(ROW)
    assert e["title"].startswith("TSMC Q3") and "FOREIGN PRIVATE ISSUER" not in e["title"]
    assert e["author"]["name"] == "SEC · FORM 6-K · FOREIGN ISSUER REPORT · TSMC (TSM)", e["author"]
    assert e["url"] == ROW["url"] and e["timestamp"] == ROW["published_at"]
    d = e["description"]
    assert "**The take**" in d and "**Why it matters**" in d and "**Watch next**" in d and "SEC.gov" in d
    names = [f["name"] for f in e["fields"]]
    assert names[:4] == ["Q3 revenue", "Diluted EPS", "2027 capex", "Dividend"]
    assert len([f for f in e["fields"] if f["name"] == "​"]) == 2           # 4 facts padded to 6
    assert names[-3:] == ["Impact", "Read", "Instruments"]
    assert "+12.5%" in e["fields"][3]["value"] and "Filed 8:41 AM ET" in e["footer"]["text"]
    assert chr(0x2014) not in json.dumps(e, ensure_ascii=False)
    assert len(e["fields"]) <= 25 and _total(e) <= 6000


def test_limits_hold_on_huge_input():
    big = dict(ROW, title="T" * 900, summary="S" * 5000, watch="W" * 3000,
               key_points=json.dumps(["P" * 900] * 9), facts=json.dumps([{"label": "L" * 300, "value": "V" * 300, "change": "C" * 300}] * 12))
    e = ke.build_sec_embed(big)
    assert len(e["title"]) <= 256 and len(e["description"]) <= 4096 and len(e["fields"]) <= 25
    assert all(len(f["name"]) <= 256 and len(f["value"]) <= 1024 for f in e["fields"]) and _total(e) <= 6000


def test_old_card_without_new_fields():
    e = ke.build_sec_embed({k: ROW[k] for k in ("source_name", "url", "headline", "summary", "impact", "direction",
                                                "confidence", "instruments", "published_at")})
    assert e["title"] == ROW["headline"] and [f["name"] for f in e["fields"]] == ["Impact", "Read", "Instruments"]


def test_queue_builds_from_live_row():
    c = sqlite3.connect(ke.DB_PATH)
    cols = ", ".join(ROW); c.execute(f"CREATE TABLE items ({cols})")
    c.execute(f"INSERT INTO items ({cols}) VALUES ({', '.join('?' * len(ROW))})", list(ROW.values()))
    c.commit(); c.close()
    ke._save_state({"seen": [], "discord_pending": [
        {"post_after": time.time() - 1, "item_id": "f1"},
        {"post_after": time.time() - 1, "headline": "Legacy queued post", "summary": "s", "impact": "high",
         "direction": "bear", "instruments": ["MU"], "url": "https://sec.gov/y", "filed_utc": ROW["published_at"],
         "ticker": "MU", "form": "8-K"},
        {"post_after": time.time() + 600, "item_id": "later"}]})
    n = len(POSTS)
    ke.flush_discord()
    got = [p["embeds"][0] for p in POSTS[n:]]
    assert len(got) == 2, got
    assert got[0]["title"].startswith("TSMC Q3") and got[0]["fields"][0]["name"] == "Q3 revenue"
    assert got[1]["title"] == "Legacy queued post" and "FORM 8-K · CURRENT REPORT · MU" in got[1]["author"]["name"]
    assert POSTS[-1]["allowed_mentions"] == {"parse": []}
    assert [p["item_id"] for p in ke._load_state()["discord_pending"]] == ["later"]   # not-yet-due stays queued


def test_missing_db_is_safe():
    ke_db = ke.DB_PATH; ke.DB_PATH = os.path.join(TMP, "nope.db")
    assert ke._row_for("f1") is None and not os.path.exists(ke.DB_PATH)   # read-only: never creates a DB
    ke.DB_PATH = ke_db


if __name__ == "__main__":
    try:
        for n in ("test_embed_shape", "test_limits_hold_on_huge_input", "test_old_card_without_new_fields",
                  "test_queue_builds_from_live_row", "test_missing_db_is_safe"):
            globals()[n](); print("ok", n)
    finally:
        srv.shutdown(); shutil.rmtree(TMP, ignore_errors=True)

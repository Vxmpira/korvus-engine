"""
Korvus Intel live status: a heartbeat + activity log every stage reports into.

Each stage (universe, edgar, scoring, quotes, bot, vault) calls record() or
event(). A background flusher writes intel_status.json atomically every 2s
when something changed, and at least every 15s as a heartbeat, so a reader
can tell "quiet" from "dead". korvus_server.py serves the file to the
BlackCrown admin (key-protected) for the live pipeline view.

Day counters reset at the US Eastern date change. Never raises: status is
observability, it must not be able to break the crawler.
"""
import os
import json
import time
import threading
import datetime as dt
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "intel_status.json")
MAX_EVENTS = 80

_lock = threading.Lock()
_dirty = [True]
_last_write = [0.0]
_state = {
    "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "pid": os.getpid(),
    "day_et": "",
    "stages": {},
}
_events = deque(maxlen=MAX_EVENTS)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _et_day() -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return dt.date.today().isoformat()


def _roll_day():
    d = _et_day()
    if _state["day_et"] != d:
        _state["day_et"] = d
        for st in _state["stages"].values():
            for k in list(st):
                if k.endswith("_today"):
                    st[k] = 0


def record(stage: str, **kv):
    """Set fields on a stage. Keys ending in _today accumulate when passed as
    inc_<name>=n (e.g. inc_polls_today=1)."""
    try:
        with _lock:
            _roll_day()
            st = _state["stages"].setdefault(stage, {})
            for k, v in kv.items():
                if k.startswith("inc_"):
                    name = k[4:]
                    st[name] = int(st.get(name) or 0) + int(v)
                else:
                    st[k] = v
            _dirty[0] = True
    except Exception:
        pass


def error(stage: str, msg: str):
    record(stage, last_error=str(msg)[:300], last_error_utc=_now())
    event(stage, str(msg)[:200], level="error")


def event(stage: str, msg: str, level: str = "info", **extra):
    try:
        with _lock:
            e = {"t": _now(), "stage": stage, "msg": str(msg)[:240], "level": level}
            if extra:
                e.update(extra)
            _events.append(e)
            _dirty[0] = True
    except Exception:
        pass


def snapshot() -> dict:
    with _lock:
        _roll_day()
        return {**_state, "updated_utc": _now(), "events": list(_events)}


def flush(force: bool = False):
    try:
        if not (force or _dirty[0] or time.time() - _last_write[0] > 15):
            return
        snap = snapshot()
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, STATUS_PATH)
        _dirty[0] = False
        _last_write[0] = time.time()
    except Exception:
        pass


def start_flusher():
    def run():
        while True:
            flush()
            time.sleep(2)
    t = threading.Thread(target=run, name="intel-status", daemon=True)
    t.start()
    return t

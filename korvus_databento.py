#!/usr/bin/env python3
"""
==============================================================================
 KORVUS · Databento CME Market-Data client   (by BlackCrownVxJ.LLC)
==============================================================================
 REAL CME futures quotes (MNQ/MES/MYM/M2K/CL/GC/ZN/6E) from one licensed feed:
 the CME Globex MDP 3.0 dataset (GLBX.MDP3), which covers CME, CBOT, NYMEX and
 COMEX. This is the source that makes the Futures Board and Session Map show the
 ACTUAL contract (e.g. MNQ ~21,000), not the QQQ ETF proxy (~740).

 STATUS: DORMANT until licensed. This module does nothing unless:
     1) the `databento` pip package is installed, AND
     2) DATABENTO_API_KEY is set in .env, AND
     3) QUOTES_PROVIDER=databento is set in .env (see korvus_quotes.py).
 With no key the client never connects and never pulls a byte of data, so it is
 safe to commit and `git pull` to the server long before the license is active.
 It cannot be live-tested without a real key + a paid CME license, so VERIFY ON
 FIRST ACTIVATION against a known chart (compare MNQ price/%, session high/low).

 LICENSING (read before going live to members):
   Databento's Standard plan covers exchange license fees for display and
   non-display use by NON-PROFESSIONAL users. Re-serving real-time CME quotes to
   PAYING subscribers can trigger CME professional / per-subscriber terms on top
   of the feed cost. The /api/quotes layer already forces FREE / logged-out users
   onto the delayed proxy feed (never Databento), so only your own Pro session
   touches this feed. Confirm the subscriber-redistribution terms with Databento
   before lighting it up for members. Do not assume the $179 plan covers resale.

 DESIGN
   Databento's live feed is a STREAM. We run it on a daemon thread and keep a
   {root: quote} snapshot; korvus_quotes.py reads that snapshot synchronously
   each cycle (non-blocking), exactly like the Tradovate client. We subscribe to
   the 1-minute OHLCV schema with continuous front-month symbology and replay the
   current session on connect, so session open/high/low are correct immediately.

 GUNICORN NOTE
   Each worker that imports this and calls start() opens its OWN live connection.
   For one operator that is wasteful and may bump the entitlement. Run the web
   app with a SINGLE worker for the live feed, or move this to a dedicated feed
   process later. Documented, not silently assumed.

 DEPENDENCY:  pip install databento
==============================================================================
"""
import os
import time
import threading
import datetime as dt
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# The package is optional. If it isn't installed (e.g. before the license is
# bought), the module still imports cleanly and the client just never starts,
# so the app keeps running on the existing ETF-proxy provider.
try:
    import databento as db
except Exception:
    db = None


# ----- config (read from environment; set in .env) --------------------------
DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "").strip()
# Continuous-contract roll rule: 'c' = front by calendar (default), 'n' = by
# open interest, 'v' = by volume. Volume/OI front is often the most-traded
# contract; calendar is the simplest. Switch in .env if a chart disagrees.
DATABENTO_ROLL = (os.getenv("DATABENTO_ROLL", "c").lower().strip() or "c")
DATASET = "GLBX.MDP3"

# Futures roots we can stream from GLBX.MDP3. NOTE: VX (VIX futures) trades on
# CBOE's CFE, a DIFFERENT dataset, so it is intentionally absent here and stays
# on its VIXY proxy. Everything below is CME/CBOT/NYMEX/COMEX -> one feed.
DATABENTO_ROOTS = ["MNQ", "MES", "MYM", "M2K", "CL", "GC", "ZN", "6E"]

# Databento fixed-point price scale (raw ints are price * 1e9) and the sentinel
# it uses for an undefined price (INT64_MAX). Anything at/above that is "no data".
_PX_SCALE = 1e-9
_UNDEF = 9223372036854775807


def _cont(root: str) -> str:
    """Front-month continuous symbol for a root, e.g. 'MNQ' -> 'MNQ.c.0'."""
    return f"{root}.{DATABENTO_ROLL}.0"


def _root_of(sym: str) -> str:
    """Map any symbol form back to its root.
        continuous  'MNQ.c.0' -> 'MNQ'
        resolved    'MNQU6'    -> 'MNQ'   ('6EU6' -> '6E')
    The historical daily-bar query resolves the continuous symbol to the actual
    front contract (e.g. 'MNQU6'), so a plain split('.') left the root
    unrecognized and every prior-close row was silently dropped — which is what
    forced the daily % onto the session-open fallback and made the board read a
    wildly wrong change. Match the known root prefix instead so both the live
    continuous symbol and the resolved historical contract map home."""
    head = (sym or "").upper().split(".")[0]
    if head in DATABENTO_ROOTS:
        return head
    for r in sorted(DATABENTO_ROOTS, key=len, reverse=True):
        if head.startswith(r):
            return r
    return head


def _px(v):
    """Scale a raw Databento fixed-point price to a float, or None if undefined."""
    if v is None:
        return None
    try:
        iv = int(v)
    except Exception:
        return None
    if abs(iv) >= _UNDEF:
        return None
    return iv * _PX_SCALE


def _session_key(ts_utc: dt.datetime) -> str:
    """A label for the CME 'trading day' so we know when to reset O/H/L.
    Globex opens 18:00 ET; anything from 18:00 ET belongs to the NEXT calendar
    day's session. We bucket by that boundary so session high/low reset cleanly
    once per trading day even though the server never restarts."""
    try:
        from zoneinfo import ZoneInfo
        et = ts_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et = ts_utc.astimezone(dt.timezone(dt.timedelta(hours=-4)))
    d = et.date()
    if et.hour >= 18:
        d = d + dt.timedelta(days=1)
    return d.isoformat()


def _session_open_utc() -> dt.datetime:
    """UTC datetime of the current Globex session open (18:00 ET boundary),
    used as the intraday-replay start so O/H/L are correct the moment we connect.
    Clamped to within the last ~23h (the live replay window)."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
        open_et = et.replace(hour=18, minute=0, second=0, microsecond=0)
        if et.hour < 18:
            open_et = open_et - dt.timedelta(days=1)
        start = open_et.astimezone(dt.timezone.utc)
    except Exception:
        start = now - dt.timedelta(hours=18)
    floor = now - dt.timedelta(hours=23)
    return max(start, floor)


class DatabentoMD:
    """Background live client maintaining a latest-quote snapshot per root."""

    def __init__(self, key: str):
        self._key = key
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._live = None
        # root -> {price, chg_pct, high, low, open, prev_close}
        self._quotes = {}
        # instrument_id -> root (filled from SymbolMappingMsg as it streams)
        self._id_to_root = {}
        # root -> prior-session settlement (for the daily % change), best-effort
        self._prev_close = {}
        # root -> session label currently accumulated (for O/H/L reset)
        self._sess = {}
        self._roots = list(DATABENTO_ROOTS)
        # wall-clock (epoch) of the most recent bar we received. Drives the
        # "is the market actually trading right now" freshness check: when bars
        # stop arriving (holiday early-close, weekend, maintenance, halt) this
        # stops advancing, so the dashboard can flip the board to CLOSED.
        self._last_recv = None

    # ---- prior-session close (for chg_pct) ---------------------------------
    def _fetch_prev_closes(self):
        """Best-effort: pull the last completed daily close per root from the
        historical API so the daily % matches a chart's 'change vs prior settle'.
        If this fails, chg_pct falls back to change-vs-session-open (still sane,
        just measured from the open instead of the prior settlement)."""
        if db is None or not self._key:
            return
        try:
            now = dt.datetime.now(dt.timezone.utc)
            start = (now - dt.timedelta(days=8)).date().isoformat()
            syms = [_cont(r) for r in self._roots]
            h = db.Historical(self._key)
            data = h.timeseries.get_range(
                dataset=DATASET, schema="ohlcv-1d",
                stype_in="continuous", symbols=syms, start=start,
            )
            df = data.to_df()           # prices come back as scaled floats
            if df is None or len(df) == 0:
                return
            today_key = _session_key(now)
            # 'symbol' column carries the (resolved) contract when mapped
            sym_col = "symbol" if "symbol" in df.columns else None
            best = {}   # root -> (session_key, close): keep the MOST RECENT completed
            for _, row in df.iterrows():
                cont = str(row[sym_col]) if sym_col else None
                root = _root_of(cont) if cont else None
                if root not in self._roots:
                    continue
                close = row.get("close")
                if close is None:
                    continue
                try:
                    ts_utc = row.name.to_pydatetime().astimezone(dt.timezone.utc)
                    skey = _session_key(ts_utc)
                except Exception:
                    continue
                if skey >= today_key:          # skip the still-forming current session
                    continue
                prev = best.get(root)
                if prev is None or skey > prev[0]:   # latest completed session wins
                    best[root] = (skey, float(close))
            for root, (skey, close) in best.items():
                self._prev_close[root] = close
            if self._prev_close:
                print("  [db] prev closes (vs prior settle): "
                      + ", ".join(f"{k}={v:.2f}" for k, v in self._prev_close.items()))
            else:
                cols = list(df.columns)[:8]
                print(f"  [db] prev-close fetch returned no usable rows "
                      f"(df rows={len(df)}, cols={cols}) — falling back to session open")
        except Exception as e:
            print(f"  [db] prev-close fetch failed (chg vs open instead): {e}")

    # ---- stream handling ---------------------------------------------------
    def _handle(self, record):
        """Callback for every live record. We only care about two kinds:
        symbol mappings (instrument_id -> 'MNQ.c.0') and 1-min OHLCV bars."""
        try:
            # SymbolMappingMsg: learn which numeric instrument is which root
            si = getattr(record, "stype_in_symbol", None)
            if si is not None:
                iid = getattr(record, "instrument_id", None)
                if iid is None:
                    hd = getattr(record, "hd", None)
                    iid = getattr(hd, "instrument_id", None)
                if iid is not None:
                    self._id_to_root[int(iid)] = _root_of(si)
                return

            # OHLCVMsg: open/high/low/close present; price-only records skipped
            if not (hasattr(record, "close") and hasattr(record, "open")
                    and hasattr(record, "high") and hasattr(record, "low")):
                return
            iid = getattr(record, "instrument_id", None)
            if iid is None:
                hd = getattr(record, "hd", None)
                iid = getattr(hd, "instrument_id", None)
            root = self._id_to_root.get(int(iid)) if iid is not None else None
            if not root:
                return

            o = _px(getattr(record, "open", None))
            hi = _px(getattr(record, "high", None))
            lo = _px(getattr(record, "low", None))
            c = _px(getattr(record, "close", None))
            if c is None:
                return

            # which trading day this bar belongs to (for O/H/L reset)
            ts_ns = getattr(record, "ts_event", None)
            try:
                ts_utc = dt.datetime.fromtimestamp(int(ts_ns) / 1e9, dt.timezone.utc)
                skey = _session_key(ts_utc)
            except Exception:
                skey = self._sess.get(root)

            with self._lock:
                q = self._quotes.get(root)
                new_session = (q is None) or (self._sess.get(root) != skey)
                if new_session:
                    # first bar of a new session: seed open/high/low fresh
                    self._sess[root] = skey
                    q = {"price": c,
                         "open": o if o is not None else c,
                         "high": hi if hi is not None else c,
                         "low": lo if lo is not None else c,
                         "chg_pct": 0.0,
                         "prev_close": self._prev_close.get(root, 0.0)}
                else:
                    q["price"] = c
                    if hi is not None:
                        q["high"] = max(q.get("high", hi), hi)
                    if lo is not None:
                        q["low"] = min(q.get("low", lo) or lo, lo)
                # daily % vs prior settlement, else vs this session's open
                base = self._prev_close.get(root) or q.get("open")
                if base:
                    q["prev_close"] = self._prev_close.get(root, q.get("prev_close", 0.0))
                    try:
                        q["chg_pct"] = (c - base) / base * 100.0
                    except Exception:
                        q["chg_pct"] = 0.0
                self._quotes[root] = q
                self._last_recv = time.time()
        except Exception as e:
            print(f"  [db] record handler error: {e}")

    def _run(self):
        """Connect, subscribe with session replay, stream forever; reconnect on
        drop. All failures are caught so a feed hiccup never takes down the app
        (the dashboard simply keeps its last numbers / proxy fallback)."""
        while self._running:
            if db is None:
                print("  [db] databento package not installed (pip install databento)")
                return
            try:
                self._prev_close = {}
                self._fetch_prev_closes()
                live = db.Live(key=self._key)
                self._live = live
                syms = [_cont(r) for r in self._roots]
                start = _session_open_utc().isoformat()
                try:
                    live.subscribe(dataset=DATASET, schema="ohlcv-1m",
                                   stype_in="continuous", symbols=syms, start=start)
                except Exception as e_sub:
                    # if intraday replay is rejected, fall back to live-from-now
                    print(f"  [db] replay subscribe failed ({e_sub}); live-from-now")
                    live.subscribe(dataset=DATASET, schema="ohlcv-1m",
                                   stype_in="continuous", symbols=syms)
                live.add_callback(self._handle)
                print(f"  [db] live: {DATASET} ohlcv-1m {syms} (roll={DATABENTO_ROLL})")
                live.start()
                live.block_for_close()
            except Exception as e:
                print(f"  [db] stream error: {e}")
            finally:
                self._live = None
            if self._running:
                time.sleep(5)   # brief backoff, then reconnect

    # ---- public API --------------------------------------------------------
    def start(self, roots=None):
        """Begin streaming (idempotent: the socket starts only once)."""
        if db is None or not self._key:
            return
        if roots:
            # keep only roots we know are on this dataset
            self._roots = [r for r in roots if r in DATABENTO_ROOTS] or list(DATABENTO_ROOTS)
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass

    def get(self, roots) -> dict:
        """Return {root: {price, chg_pct, high, low, open, prev_close}} for the
        requested roots, reading the latest snapshot (non-blocking)."""
        out = {}
        with self._lock:
            for r in roots:
                if r in self._quotes:
                    out[r] = dict(self._quotes[r])
        return out

    def seconds_since_last_bar(self):
        """Age in seconds of the most recent bar we received, or None if we've
        never received one. A large/None value means the feed is quiet, i.e. the
        market is not trading right now (holiday close, weekend, maintenance,
        or a halt). This is the freshness signal the status layer trusts over
        any hardcoded clock, since CME holiday hours shift year to year."""
        with self._lock:
            if self._last_recv is None:
                return None
            return time.time() - self._last_recv


# module-level singleton the quotes layer imports
_client: Optional[DatabentoMD] = None


def get_client(key: str = "") -> DatabentoMD:
    global _client
    if _client is None:
        _client = DatabentoMD(key or DATABENTO_API_KEY)
    return _client


def feed_age():
    """Seconds since the live feed last produced a bar, or None if the stream
    hasn't started / no bars yet. Lets the quotes layer decide whether the CME
    market is actually trading right now without trusting a fixed clock."""
    if _client is None:
        return None
    try:
        return _client.seconds_since_last_bar()
    except Exception:
        return None


if __name__ == "__main__":
    # Manual smoke test (needs: pip install databento + a real key + license):
    #   DATABENTO_API_KEY=... python korvus_databento.py
    # Prints, per root: prior settle (the daily-% baseline), the live price, and
    # the resulting change — so you can confirm it matches your broker's daily %.
    if not DATABENTO_API_KEY:
        print("DATABENTO_API_KEY not set — dormant, nothing to test.")
        raise SystemExit(0)
    roots = ["MNQ", "MES", "MYM", "M2K", "CL", "GC", "ZN", "6E"]
    c = get_client()
    c.start(roots)
    print("connecting + replaying session… (give it ~15s for the first bars)")
    try:
        for i in range(12):
            time.sleep(5)
            snap = c.get(roots)
            if not snap:
                print(f"  [{(i+1)*5:>3}s] no bars yet…")
                continue
            print(f"  --- snapshot at {(i+1)*5}s ---")
            print(f"  {'root':5} {'prev_close':>11} {'price':>11} {'chg%':>8}")
            for r in roots:
                q = snap.get(r)
                if not q:
                    continue
                pc = q.get("prev_close") or 0
                base_note = "" if pc else "  (no prior settle -> chg vs session open)"
                print(f"  {r:5} {pc:>11.2f} {q.get('price',0):>11.2f} "
                      f"{q.get('chg_pct',0):>+7.2f}%{base_note}")
    except KeyboardInterrupt:
        pass
    finally:
        c.stop()

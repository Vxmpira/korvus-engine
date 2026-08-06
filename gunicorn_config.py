"""
Gunicorn production config for the Korvus server.
Run with:  gunicorn -c gunicorn_config.py korvus_server:app
(systemd does this for you - see deploy/DEPLOY.md)
"""
import multiprocessing

# Bind to a local port; Nginx sits in front and faces the public.
bind = "127.0.0.1:8000"

# SINGLE worker, many threads. This is deliberate, not a downgrade:
#   - The live Databento stream, its settle baseline, the quote caches and the
#     background warmer all live INSIDE the worker process. Two workers meant
#     two parallel CME streams (double usage billing) and split caches.
#     One worker = one stream, one settle, one warm cache, halved data bill.
#   - Twelve threads give 12 concurrent request slots (was 4), which covers
#     the 1-second pro polling comfortably; each request is a few ms of RAM.
workers = 1
threads = 12

# Worker recycling is DISABLED, also deliberate: at ~2 requests/second per open
# terminal, max_requests=1000 recycled the worker every few minutes, and every
# recycle threw away the stream, the settle, and the warm caches, then re-pulled
# a full day replay from Databento (paid) while requests waited on cold data.
# That churn was the "reload takes a minute" and the board's stale windows.
# The process is small; systemd (Restart=always) is the safety net.
max_requests = 0
max_requests_jitter = 0

# Give Claude-scoring requests room (the engine runs separately, but be safe).
timeout = 120

# Logging - systemd captures these via journald.
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Make the app's own print() lines reach journald IMMEDIATELY. Python
# block-buffers stdout when it is a pipe, so diagnostics like the [db] stream
# lines could sit unflushed for a long time; ironically the old worker
# recycling flushed them on every worker death, and with recycling disabled a
# healthy process might log nothing for hours. Unbuffered + captured output
# makes `journalctl | grep '[db]'` a reliable health check.
raw_env = ["PYTHONUNBUFFERED=1"]
capture_output = True


def post_worker_init(worker):
    """Start the background warmer (and with it the licensed CME stream, the
    settle baseline, and the quote-cache warm-up) the moment the worker boots,
    instead of waiting for the first HTTP request. After every restart the
    board is primed before the first visitor arrives, and the [db] startup
    lines appear in the journal right away."""
    try:
        from korvus_server import _start_warmer
        _start_warmer()
    except Exception as e:
        print(f"[gunicorn] warmer boot skipped: {e}")

proc_name = "korvus"

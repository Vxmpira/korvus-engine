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

proc_name = "korvus"

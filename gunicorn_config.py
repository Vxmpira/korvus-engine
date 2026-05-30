"""
Gunicorn production config for the Korvus server.
Run with:  gunicorn -c gunicorn_config.py korvus_server:app
(systemd does this for you — see deploy/DEPLOY.md)
"""
import multiprocessing

# Bind to a local port; Nginx sits in front and faces the public.
bind = "127.0.0.1:8000"

# A small VPS (1-2 vCPU) is plenty for Korvus. 2-3 workers is right.
workers = 2
threads = 2

# Restart workers periodically to keep memory tidy on a long-running box.
max_requests = 1000
max_requests_jitter = 100

# Give Claude-scoring requests room (the engine runs separately, but be safe).
timeout = 120

# Logging — systemd captures these via journald.
accesslog = "-"
errorlog = "-"
loglevel = "info"

proc_name = "korvus"

"""
Korvus — admin gating + agent/admin pages
==========================================

Matched to your actual stack: single-file Flask (korvus_server.py), Flask-Login
(`current_user`), pages served as STATIC files from the project root via
send_from_directory. No Jinja, no sessions-by-hand.

Wire-up — in korvus_server.py, after `app` and `login_manager` are created:

    from korvus_admin import korvus_admin, is_admin
    app.register_blueprint(korvus_admin)

Then add  is_admin()  to the /api/me response (see chat for the one-line edit)
so the static dashboard can show the buttons.

This gives you:
  /agent   -> serves agent.html   (admin only, 403 otherwise)
  /admin   -> serves admin.html   (admin only, 403 otherwise)
  is_admin()        -> use in /api/me
  admin_required    -> reused by korvus_promo_api.py to lock /api/promo-generate
"""

import os
from functools import wraps
from flask import Blueprint, abort, send_from_directory
from flask_login import current_user

HERE = os.path.dirname(os.path.abspath(__file__))

# The ONLY place admin access is defined. Match is case-insensitive so a casing
# slip can't lock you out — but put your EXACT signup username here. Your auth
# stores usernames case-sensitively, so confirm what you actually registered as.
ADMIN_USERS = {"vxmpira.n"}


def is_admin():
    if not getattr(current_user, "is_authenticated", False):
        return False
    uname = (getattr(current_user, "username", "") or "").strip().lower()
    return uname in ADMIN_USERS


def admin_required(view):
    """Server-side gate — the real lock. The hidden button is only UX."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


korvus_admin = Blueprint("korvus_admin", __name__)


@korvus_admin.route("/agent")
@admin_required
def agent_page():
    return send_from_directory(HERE, "agent.html")


@korvus_admin.route("/admin")
@admin_required
def admin_page():
    return send_from_directory(HERE, "admin.html")

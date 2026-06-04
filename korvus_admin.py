"""
Korvus — admin gating + agent access
=====================================

Drop this file next to app.py and register it once, near where you create `app`:

    from korvus_admin import korvus_admin
    app.register_blueprint(korvus_admin)

That gives you:
  - /agent   -> Promo Studio  (admin only)
  - /admin   -> Admin panel    (admin only)
  - a `viewer_is_admin` flag available in every Jinja template
  - an `admin_required` decorator you can reuse on any other route

The ONLY place admin access is defined is ADMIN_USERS below, and the ONLY
real security boundary is `admin_required` on the routes. The buttons in the
profile template are just UX so non-admins never see a dead/forbidden link.
"""

from functools import wraps
from flask import Blueprint, session, abort, render_template


# --------------------------------------------------------------------------
# WHO IS ADMIN
# --------------------------------------------------------------------------
# Add usernames here. Matching is case-insensitive so a capitalization
# mismatch can't lock you out — but confirm this matches the value actually
# stored for your account at signup (memory shows it as "Vxmpira.n").
ADMIN_USERS = {"vxmpira.n"}


def current_username():
    """
    Return the logged-in user's username, or None.

    >>> INTEGRATION POINT — match this to how YOUR app tracks login. <<<

    Plain Flask sessions (most likely your setup):
        return session.get("username")

    Flask-Login:
        from flask_login import current_user
        return current_user.username if current_user.is_authenticated else None
    """
    return session.get("username")


def is_admin(username=None):
    u = username if username is not None else current_username()
    return bool(u) and u.strip().lower() in ADMIN_USERS


def admin_required(view):
    """Server-side gate. This is the actual lock — not the hidden button."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)  # swap for redirect(url_for('login')) if you prefer
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------------
# ROUTES
# --------------------------------------------------------------------------
korvus_admin = Blueprint("korvus_admin", __name__)


@korvus_admin.route("/agent")
@admin_required
def agent():
    # Renders the Promo Studio page (templates/agent.html).
    # NOTE: the studio's AI calls + saved-posts storage need a small
    # server-side adaptation to run on korvus.industries — see the chat.
    return render_template("agent.html")


@korvus_admin.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html")


# Makes `viewer_is_admin` usable in any template, including the profile page.
@korvus_admin.app_context_processor
def inject_admin_flag():
    return {"viewer_is_admin": is_admin()}

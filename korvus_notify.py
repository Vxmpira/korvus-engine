"""
korvus_notify.py - Discord webhook notifications for owner-action moments.

Purpose: LodeStone access on TradingView is granted and revoked by hand, so the
platform pings a private Discord channel at exactly the moments that need a
human: a member becomes Pro (grant), a Pro member saves or changes their
TradingView username (grant / regrant), and a Pro membership ends (revoke).

Design rules:
  - Best-effort by design. A Discord outage, a bad URL, or a missing setting
    must NEVER break billing webhooks, account saves, or anything else. Every
    send runs in a daemon thread with a short timeout and swallows errors.
  - Stdlib only (urllib), so this module adds no dependencies.
  - Configured by one .env value:
        DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXX/YYYY
    Create it in Discord: Server Settings > Integrations > Webhooks >
    New Webhook, pick a private channel only you can read, copy the URL.
    If the value is missing, every call is a silent no-op.
"""

import os
import json
import threading
import urllib.request
from dotenv import load_dotenv

load_dotenv()

DISCORD_WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()

# embed colors matched to the terminal palette
_GREEN = 0x3CE594   # grant / good news
_RED   = 0xFF2E4D   # revoke / membership ended
_PINK  = 0xFF2D8F   # username changes / brand accent


def notify_enabled() -> bool:
    return bool(DISCORD_WEBHOOK_URL)


def _post(payload: dict):
    """POST the payload to the webhook. Runs inside the worker thread."""
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "korvus-notify/1.0"},
            method="POST")
        with urllib.request.urlopen(req, timeout=6) as resp:
            resp.read()
    except Exception as e:
        # log and move on; notifications are never allowed to break the caller
        print(f"  [notify] discord send failed: {e}")


def _send(title: str, description: str, color: int):
    """Fire-and-forget embed send. No-op when no webhook is configured."""
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"embeds": [{"title": title,
                           "description": description,
                           "color": color}]}
    threading.Thread(target=_post, args=(payload,), daemon=True).start()


# ----------------------------------------------------------------------------
# Formatted events (called from korvus_billing and korvus_server)
# ----------------------------------------------------------------------------
def member_went_pro(username, email, tv_username, status):
    """A member's tier just became Pro (new subscription or recovery)."""
    lines = [f"**Member:** {username}",
             f"**Email:** {email or 'unknown'}",
             f"**Stripe status:** {status}"]
    if tv_username:
        lines.append("")
        lines.append(f"**ACTION: grant LodeStone access to `{tv_username}` on TradingView.**")
    else:
        lines.append("")
        lines.append("No TradingView username saved yet. You will get another "
                     "ping the moment they add one on their account page.")
    _send("Korvus · new Pro member", "\n".join(lines), _GREEN)


def member_pro_ended(username, email, tv_username, status):
    """A member's tier just dropped from Pro (canceled, unpaid, or expired)."""
    lines = [f"**Member:** {username}",
             f"**Email:** {email or 'unknown'}",
             f"**Stripe status:** {status}"]
    if tv_username:
        lines.append("")
        lines.append(f"**ACTION: revoke LodeStone access for `{tv_username}` on TradingView.**")
        lines.append("Manage Access on the script > remove that username.")
    else:
        lines.append("")
        lines.append("No TradingView username on file, so there is nothing to revoke.")
    _send("Korvus · Pro membership ended", "\n".join(lines), _RED)


def tv_username_changed(username, old_tv, new_tv):
    """A PRO member set, changed, or cleared their TradingView username.
    Free-tier saves do not call this; their grant ping fires on upgrade."""
    lines = [f"**Member:** {username}"]
    if old_tv and new_tv:
        lines.append(f"Changed TradingView username: `{old_tv}` to `{new_tv}`.")
        lines.append("")
        lines.append(f"**ACTION: revoke `{old_tv}`, then grant `{new_tv}`.**")
    elif new_tv:
        lines.append(f"Saved TradingView username: `{new_tv}`.")
        lines.append("")
        lines.append(f"**ACTION: grant LodeStone access to `{new_tv}` on TradingView.**")
    else:
        lines.append(f"Cleared their TradingView username (was `{old_tv}`).")
        lines.append("")
        lines.append(f"**ACTION: revoke LodeStone access for `{old_tv}`.**")
    _send("Korvus · TradingView username updated (Pro member)", "\n".join(lines), _PINK)

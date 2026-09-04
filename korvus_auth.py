#!/usr/bin/env python3
"""
==============================================================================
 KORVUS AUTH  ·  Phase 5   (by BlackCrownVxJ.LLC)
==============================================================================
 User accounts, secure password hashing, sessions, email verification, and
 the free/pro tier flag.

 DESIGN NOTES
 - Passwords are stored HASHED (werkzeug pbkdf2). The real password is never
   stored and cannot be recovered - only checked. This is the correct, safe
   design: even with database access, nobody can read members' passwords.
 - Users self-register through the signup page. Nothing here creates accounts
   on anyone's behalf.
 - Email verification uses a one-time token. Until an email provider is
   configured (EMAIL_PROVIDER in .env), the "send" step is stubbed: the system
   works, accounts are created, but the verification link is logged to the
   server console instead of emailed. Flip EMAIL_PROVIDER to "ses" and add the
   AWS creds to turn real sending on - no code change needed.
 - Tier is 'free' by default. Nobody becomes 'pro' here; that happens via the
   Stripe step (next phase). You can manually promote your own test account
   with: python korvus_auth.py promote <username>
==============================================================================
"""
import os
import re
import sqlite3
import secrets
import datetime as dt
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "korvus.db")

EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "off").lower().strip()   # off | ses
EMAIL_FROM     = os.getenv("EMAIL_FROM", "noreply@korvus.industries")
SITE_URL       = os.getenv("SITE_URL", "https://korvus.industries")
AWS_REGION     = os.getenv("AWS_SES_REGION", "us-east-2")


# ----------------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    """Create the users table if it doesn't exist. Safe to run repeatedly."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            tier          TEXT NOT NULL DEFAULT 'free',
            email_verified INTEGER NOT NULL DEFAULT 0,
            verify_token  TEXT,
            created_at    TEXT NOT NULL
        )
    """)
    # --- billing columns (added in the Stripe phase; safe to run repeatedly) ---
    existing = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col, ddl in (("stripe_customer_id", "TEXT"),
                     ("stripe_subscription_id", "TEXT"),
                     ("subscription_status", "TEXT"),
                     ("current_period_end", "TEXT"),
                     ("reset_token", "TEXT"),
                     ("reset_expires", "TEXT"),
                     ("alert_opt_in", "INTEGER DEFAULT 1"),
                     ("last_login", "TEXT"),
                     ("tv_username", "TEXT")):
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    # --- member timeline (admin console drawer). Append-only event log for
    #     signups, tier changes, billing transitions, and reset emails. ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS member_events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            username TEXT NOT NULL,
            kind     TEXT NOT NULL,
            detail   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_member_events_user "
                 "ON member_events(username, ts)")
    conn.commit()
    conn.close()


def log_member_event(username, kind, detail=""):
    """Append one row to the member timeline. Best-effort by design: an event
    that fails to record must never break signup, login, billing, or resets."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO member_events (ts, username, kind, detail) VALUES (?,?,?,?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(),
             (username or "").strip(), kind, detail))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# REGISTRATION  (users self-register; never called to seed accounts)
# ----------------------------------------------------------------------------
def create_user(username, email, password):
    """
    Returns (ok: bool, message: str, verify_token: str|None).
    Validates uniqueness, hashes the password, stores unverified, issues a token.
    Usernames are CASE-SENSITIVE: 'Rob' and 'rob' are different accounts, and
    login must use the exact casing chosen at signup.
    """
    username = (username or "").strip()
    email = (email or "").strip().lower()
    if len(username) < 3:
        return False, "Username must be at least 3 characters.", None
    if len(password or "") < 8:
        return False, "Password must be at least 8 characters.", None
    if "@" not in email or "." not in email:
        return False, "Please enter a valid email address.", None

    conn = get_db()
    # case-sensitive uniqueness checks (exact match)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        conn.close(); return False, "That username is taken.", None
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        conn.close(); return False, "An account with that email already exists.", None

    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO users (username,email,password_hash,tier,email_verified,verify_token,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (username, email, generate_password_hash(password), "free", 0, token,
         dt.datetime.now(dt.timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    log_member_event(username, "signup", "account created")
    return True, "Account created. Check your email to verify.", token


def verify_password(username, password):
    """Returns the user row (dict) if credentials are valid, else None.
    Username match is CASE-SENSITIVE (exact)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?",
                       ((username or "").strip(),)).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password or ""):
        # stamp last_login for the admin console's member drawer; best-effort
        try:
            c2 = get_db()
            c2.execute("UPDATE users SET last_login = ? WHERE id = ?",
                       (dt.datetime.now(dt.timezone.utc).isoformat(), row["id"]))
            c2.commit()
            c2.close()
        except Exception:
            pass
        return dict(row)
    return None


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def confirm_email(token):
    """Marks the matching account verified. Returns True if a user was verified."""
    if not token:
        return False
    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE verify_token = ?", (token,)).fetchone()
    if not row:
        conn.close(); return False
    conn.execute("UPDATE users SET email_verified = 1, verify_token = NULL WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


def set_tier(username, tier):
    """Set a user's tier ('free' or 'pro'). Used by Stripe webhook later, or manual test promote."""
    conn = get_db()
    conn.execute("UPDATE users SET tier = ? WHERE username = ?", (tier, (username or "").strip()))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# BILLING HELPERS  (used by korvus_billing.py / the Stripe webhook)
# ----------------------------------------------------------------------------
def get_user_by_username(username):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?",
                       ((username or "").strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_customer_id(customer_id):
    if not customer_id:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE stripe_customer_id = ?",
                       (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_stripe_customer(username, customer_id):
    """Link a Stripe customer id to a user (stored once, at first checkout)."""
    conn = get_db()
    conn.execute("UPDATE users SET stripe_customer_id = ? WHERE username = ?",
                 (customer_id, (username or "").strip()))
    conn.commit()
    conn.close()


# subscription statuses that grant Pro access
PRO_STATUSES = {"active", "trialing"}

def apply_subscription(customer_id, status, subscription_id=None, current_period_end=None):
    """Sync a user's tier + subscription fields from a Stripe event, matched by
    stripe_customer_id. Tier becomes 'pro' while active/trialing, else 'free'.
    Returns the affected username, or None if no user matched that customer."""
    user = get_user_by_customer_id(customer_id)
    if not user:
        return None
    tier = "pro" if (status in PRO_STATUSES) else "free"
    conn = get_db()
    conn.execute(
        "UPDATE users SET tier = ?, subscription_status = ?, stripe_subscription_id = ?, "
        "current_period_end = ? WHERE stripe_customer_id = ?",
        (tier, status, subscription_id, current_period_end, customer_id))
    conn.commit()
    conn.close()
    log_member_event(user["username"], "billing",
                     f"subscription {status} (tier {tier})")
    return user["username"]


# ----------------------------------------------------------------------------
# PROFILE / ACCOUNT EDITS  (used by the /account page)
# ----------------------------------------------------------------------------
def update_username(user_id, new_username):
    """Change a username after a uniqueness check. Returns (ok, message).
    Safe for billing: the Stripe link is keyed by customer id, not username."""
    new_username = (new_username or "").strip()
    if len(new_username) < 3:
        return False, "Username must be at least 3 characters."
    conn = get_db()
    row = conn.execute("SELECT id FROM users WHERE username = ?", (new_username,)).fetchone()
    if row and str(row["id"]) != str(user_id):
        conn.close(); return False, "That username is taken."
    conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
    conn.commit(); conn.close()
    return True, "Username updated."


def set_tv_username(user_id, name):
    """Store the member's exact TradingView username, used to grant LodeStone
    invite-only access. Empty input clears the field. Returns (ok, message)."""
    name = (name or "").strip().lstrip("@").strip()
    conn = get_db()
    if name == "":
        conn.execute("UPDATE users SET tv_username = NULL WHERE id = ?", (user_id,))
        conn.commit(); conn.close()
        return True, "TradingView username cleared."
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,32}", name):
        conn.close()
        return False, "That does not look like a valid TradingView username (letters, numbers, dot, dash, underscore)."
    conn.execute("UPDATE users SET tv_username = ? WHERE id = ?", (name, user_id))
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.commit(); conn.close()
    if row:
        log_member_event(row["username"], "tv_username", "set to " + name)
    return True, "Saved. LodeStone access is granted to this exact TradingView username."


def change_password(user_id, current_pw, new_pw):
    """Verify the current password, then store a new hash. Returns (ok, message)."""
    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        conn.close(); return False, "Account not found."
    if not check_password_hash(row["password_hash"], current_pw or ""):
        conn.close(); return False, "Current password is incorrect."
    if len(new_pw or "") < 8:
        conn.close(); return False, "New password must be at least 8 characters."
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(new_pw), user_id))
    conn.commit(); conn.close()
    return True, "Password changed."


# ----------------------------------------------------------------------------
# EMAIL  (pluggable; stubbed until EMAIL_PROVIDER is configured)
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# PASSWORD RESET + EMAIL VERIFICATION POLISH
# ----------------------------------------------------------------------------
RESET_TTL_MIN = 60   # reset links are valid for one hour

def create_reset_token(email):
    """Issue a password-reset token for a known email. Returns (token, email) if
    a matching account exists, else (None, None). Callers must respond generically
    either way so account existence isn't leaked."""
    email = (email or "").strip().lower()
    conn = get_db()
    row = conn.execute("SELECT id, username FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        conn.close(); return None, None
    token = secrets.token_urlsafe(32)
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=RESET_TTL_MIN)).isoformat()
    conn.execute("UPDATE users SET reset_token = ?, reset_expires = ? WHERE id = ?",
                 (token, expires, row["id"]))
    conn.commit(); conn.close()
    log_member_event(row["username"], "reset", "password reset link issued")
    return token, email


def reset_password(token, new_pw):
    """Consume a valid, unexpired reset token and set a new password. Returns (ok, message)."""
    if not token:
        return False, "Invalid or missing reset link."
    if len(new_pw or "") < 8:
        return False, "New password must be at least 8 characters."
    conn = get_db()
    row = conn.execute("SELECT id, reset_expires FROM users WHERE reset_token = ?", (token,)).fetchone()
    if not row:
        conn.close(); return False, "This reset link is invalid or has already been used."
    try:
        expired = dt.datetime.fromisoformat(row["reset_expires"]) < dt.datetime.now(dt.timezone.utc)
    except Exception:
        expired = True
    if expired:
        conn.execute("UPDATE users SET reset_token = NULL, reset_expires = NULL WHERE id = ?", (row["id"],))
        conn.commit(); conn.close()
        return False, "This reset link has expired. Please request a new one."
    conn.execute("UPDATE users SET password_hash = ?, reset_token = NULL, reset_expires = NULL WHERE id = ?",
                 (generate_password_hash(new_pw), row["id"]))
    conn.commit(); conn.close()
    return True, "Password updated. You can now log in."


def resend_verification(email):
    """Reissue a verification token for an unverified account. Returns (token, email)
    or (None, None). Respond generically regardless."""
    email = (email or "").strip().lower()
    conn = get_db()
    row = conn.execute("SELECT id, email_verified FROM users WHERE email = ?", (email,)).fetchone()
    if not row or row["email_verified"]:
        conn.close(); return None, None
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE users SET verify_token = ? WHERE id = ?", (token, row["id"]))
    conn.commit(); conn.close()
    return token, email


def change_email(user_id, new_email):
    """Change the account email, mark it unverified, and issue a fresh verify token
    (the caller emails it to the NEW address). Returns (ok, message, token, email)."""
    new_email = (new_email or "").strip().lower()
    if "@" not in new_email or "." not in new_email:
        return False, "Please enter a valid email address.", None, None
    conn = get_db()
    other = conn.execute("SELECT id FROM users WHERE email = ?", (new_email,)).fetchone()
    if other and str(other["id"]) != str(user_id):
        conn.close(); return False, "That email is already in use.", None, None
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE users SET email = ?, email_verified = 0, verify_token = ? WHERE id = ?",
                 (new_email, token, user_id))
    conn.commit(); conn.close()
    return True, "Email updated - check your new inbox to verify it.", token, new_email


def send_reset_email(email, token):
    """Sends the password-reset link (SES, or console stub until a provider is set)."""
    link = f"{SITE_URL}/reset?token={token}"
    subject = "Reset your Korvus password"
    body = (f"We received a request to reset your Korvus password.\n\n"
            f"Set a new password here (valid for {RESET_TTL_MIN} minutes):\n{link}\n\n"
            f"If you didn't request this, you can safely ignore this message - "
            f"your password won't change.\n\n- BlackCrownVxJ.LLC")
    if EMAIL_PROVIDER == "ses":
        return _send_ses(email, subject, body)
    print("\n" + "="*60)
    print("  [email:stub] EMAIL_PROVIDER is off - not actually sending.")
    print(f"  To: {email}")
    print(f"  Reset link: {link}")
    print("="*60 + "\n")
    return True


# ----------------------------------------------------------------------------
# ALERTS - email opted-in Pro members when the engine flags a high-impact event
# ----------------------------------------------------------------------------
def set_alert_opt_in(user_id, on):
    """Turn high-impact email alerts on/off for one user."""
    conn = get_db()
    conn.execute("UPDATE users SET alert_opt_in = ? WHERE id = ?",
                 (1 if on else 0, user_id))
    conn.commit()
    conn.close()
    return True


def alert_recipients():
    """Emails of verified Pro members who haven't opted out of alerts."""
    conn = get_db()
    rows = conn.execute(
        "SELECT email FROM users WHERE tier='pro' AND email_verified=1 "
        "AND COALESCE(alert_opt_in,1)=1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    conn.close()
    return [r["email"] for r in rows]


def send_high_impact_alert(item):
    """Email a high-impact event to opted-in Pro members.

    `item` is a dict with headline / summary / impact_desc / direction /
    instruments / url. Safe to call from the engine - it never raises and
    returns the number of recipients emailed. When EMAIL_PROVIDER is 'off'
    it logs to the console instead of sending (same stub behavior as the
    verification + reset emails)."""
    try:
        recips = alert_recipients()
    except Exception as e:
        print(f"  [alert] recipient lookup failed: {e}")
        return 0
    if not recips:
        return 0

    inst = ", ".join(item.get("instruments") or []) or "\u2014"
    dword = {"bull": "bullish", "bear": "bearish", "neut": "neutral"}.get(
        item.get("direction", ""), item.get("direction", "") or "neutral")
    headline = (item.get("headline") or "")[:140]
    subject = f"Korvus \u00b7 High-impact: {headline}"

    body = ("A HIGH-IMPACT event was just flagged by the Korvus engine.\n\n"
            f"{item.get('headline','')}\n\n"
            f"{item.get('summary','')}\n\n")
    if item.get("impact_desc"):
        body += f"Why it matters:\n{item.get('impact_desc')}\n\n"
    body += f"Instruments: {inst}\nLikely direction: {dword}\n"
    if item.get("url"):
        body += f"Source: {item.get('url')}\n"
    body += (f"\nOpen the terminal: {SITE_URL}/terminal\n"
             f"Manage or turn off alerts: {SITE_URL}/account\n\n"
             "\u2014 Korvus \u00b7 BlackCrownVxJ.LLC")

    sent = 0
    for email in recips:
        try:
            if EMAIL_PROVIDER == "ses":
                if _send_ses(email, subject, body):
                    sent += 1
            else:
                print(f"  [alert:stub] EMAIL_PROVIDER off \u2014 would email "
                      f"{email}: {subject}")
                sent += 1
        except Exception as e:
            print(f"  [alert] send to {email} failed: {e}")
    print(f"  [alert] high-impact \u2192 {sent}/{len(recips)} recipient(s)")
    return sent


def send_verification_email(email, token):
    """
    Sends the verification link. Until a provider is set up, this logs the link
    to the server console (so you can still test by copy-pasting it).
    """
    link = f"{SITE_URL}/verify?token={token}"
    subject = "Verify your Korvus account"
    body = (f"Welcome to Korvus.\n\n"
            f"Confirm your email to activate your account:\n{link}\n\n"
            f"If you didn't sign up, you can ignore this message.\n\n"
            f"- BlackCrownVxJ.LLC")

    if EMAIL_PROVIDER == "ses":
        return _send_ses(email, subject, body)

    # stubbed mode - no provider yet
    print("\n" + "="*60)
    print("  [email:stub] EMAIL_PROVIDER is off - not actually sending.")
    print(f"  To: {email}")
    print(f"  Verify link: {link}")
    print("="*60 + "\n")
    return True


def _send_ses(to_email, subject, body):
    """AWS SES sender. Requires boto3 + SES set up (domain verified, out of sandbox)."""
    try:
        import boto3
        client = boto3.client("ses", region_name=AWS_REGION)
        client.send_email(
            Source=EMAIL_FROM,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        return True
    except Exception as e:
        print(f"  [email:ses] send failed: {e}")
        return False


# ----------------------------------------------------------------------------
# CLI - init the table, or promote a test account to pro
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    init_auth_db()
    if len(sys.argv) >= 3 and sys.argv[1] == "promote":
        set_tier(sys.argv[2], "pro")
        print(f"✓ {sys.argv[2]} promoted to pro")
    elif len(sys.argv) >= 3 and sys.argv[1] == "demote":
        set_tier(sys.argv[2], "free")
        print(f"✓ {sys.argv[2]} set to free")
    else:
        print("✓ auth table ready (users)")
        print("  Promote a test account:  python korvus_auth.py promote <username>")

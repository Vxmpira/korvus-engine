#!/usr/bin/env python3
"""
==============================================================================
 KORVUS AUTH  ·  Phase 5   (by BlackCrownVxJ.LLC)
==============================================================================
 User accounts, secure password hashing, sessions, email verification, and
 the free/pro tier flag.

 DESIGN NOTES
 - Passwords are stored HASHED (werkzeug pbkdf2). The real password is never
   stored and cannot be recovered — only checked. This is the correct, safe
   design: even with database access, nobody can read members' passwords.
 - Users self-register through the signup page. Nothing here creates accounts
   on anyone's behalf.
 - Email verification uses a one-time token. Until an email provider is
   configured (EMAIL_PROVIDER in .env), the "send" step is stubbed: the system
   works, accounts are created, but the verification link is logged to the
   server console instead of emailed. Flip EMAIL_PROVIDER to "ses" and add the
   AWS creds to turn real sending on — no code change needed.
 - Tier is 'free' by default. Nobody becomes 'pro' here; that happens via the
   Stripe step (next phase). You can manually promote your own test account
   with: python korvus_auth.py promote <username>
==============================================================================
"""
import os
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
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# REGISTRATION  (users self-register; never called to seed accounts)
# ----------------------------------------------------------------------------
def create_user(username, email, password):
    """
    Returns (ok: bool, message: str, verify_token: str|None).
    Validates uniqueness, hashes the password, stores unverified, issues a token.
    Usernames are matched case-insensitively (so 'Rob' and 'rob' are the same
    account) to avoid login confusion, but the original casing is preserved.
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
    # case-insensitive uniqueness checks
    if conn.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone():
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
    return True, "Account created. Check your email to verify.", token


def verify_password(username, password):
    """Returns the user row (dict) if credentials are valid, else None.
    Username match is case-insensitive."""
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
                       ((username or "").strip(),)).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password or ""):
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
# EMAIL  (pluggable; stubbed until EMAIL_PROVIDER is configured)
# ----------------------------------------------------------------------------
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
            f"— BlackCrownVxJ.LLC")

    if EMAIL_PROVIDER == "ses":
        return _send_ses(email, subject, body)

    # stubbed mode — no provider yet
    print("\n" + "="*60)
    print("  [email:stub] EMAIL_PROVIDER is off — not actually sending.")
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
# CLI — init the table, or promote a test account to pro
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

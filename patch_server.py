#!/usr/bin/env python3
"""
patch_server.py — wires the admin blueprints + is_admin into korvus_server.py
                  in the CORRECT place (after `app` and `login_manager` exist).

Safe + idempotent:
  - edits korvus_server.py in place; nothing existing is lost
  - skips anything already applied (re-runnable)
  - if an expected spot can't be found, ABORTS and writes nothing
  - backs up korvus_server.py.bak first

Recommended flow (clean base, then deterministic edit):
    cd ~/korvus-engine
    git checkout -- korvus_server.py        # discard any broken hand-edit
    python3 patch_server.py
    sudo systemctl restart korvus-server
    systemctl status korvus-server --no-pager
"""
import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "korvus_server.py")

# blueprint registration goes right AFTER this line (app + login_manager exist by here)
REGISTER_ANCHOR = 'login_manager.login_view = "login_page"'
REGISTER_INSERT = (
    "\nfrom korvus_admin import korvus_admin, is_admin\n"
    "from korvus_promo_api import korvus_promo_api\n"
    "app.register_blueprint(korvus_admin)\n"
    "app.register_blueprint(korvus_promo_api)\n"
)

# add is_admin to the /api/me response (stable single-line anchor)
ME_ANCHOR  = '"verified": current_user.email_verified})'
ME_REPLACE = '"verified": current_user.email_verified, "is_admin": is_admin()})'


def main():
    if not os.path.exists(PATH):
        sys.exit(f"Can't find {PATH} — run this from the project folder.")

    with open(PATH, encoding="utf-8") as f:
        src = f.read()
    out = src
    notes = []

    # 1) blueprint registration
    if "from korvus_promo_api import" in out:
        notes.append("blueprint registration: already present")
    elif REGISTER_ANCHOR in out:
        out = out.replace(REGISTER_ANCHOR, REGISTER_ANCHOR + REGISTER_INSERT, 1)
        notes.append("blueprint registration: added")
    else:
        sys.exit(f"ABORTED — wrote nothing. Couldn't find anchor:\n  {REGISTER_ANCHOR}")

    # 2) is_admin in /api/me
    if '"is_admin": is_admin()' in out:
        notes.append("api_me is_admin: already present")
    elif ME_ANCHOR in out:
        out = out.replace(ME_ANCHOR, ME_REPLACE, 1)
        notes.append("api_me is_admin: added")
    else:
        sys.exit(f"ABORTED — wrote nothing. Couldn't find anchor:\n  {ME_ANCHOR}")

    if out != src:
        shutil.copy2(PATH, PATH + ".bak")
        with open(PATH, "w", encoding="utf-8") as f:
            f.write(out)
        print("Patched korvus_server.py  (backup: korvus_server.py.bak)")
    else:
        print("Already fully wired — no changes needed.")
    for n in notes:
        print("  -", n)
    print("Now run:  sudo systemctl restart korvus-server")


if __name__ == "__main__":
    main()

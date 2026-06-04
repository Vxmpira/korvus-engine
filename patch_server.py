#!/usr/bin/env python3
"""
patch_server.py  (updated) — registers the admin/agent blueprints in korvus_server.py.

Your live /api/me already exposes ownership via "is_owner", so this no longer
touches /api/me. It ONLY registers the blueprints that add the routes:
    /agent              -> Promo Studio   (owner-gated)
    /admin              -> Admin panel    (owner-gated)
    /api/promo-generate -> generation API (owner-gated)

Safe + idempotent:
  - skips if already registered
  - aborts and writes nothing if the anchor isn't found
  - backs up korvus_server.py.bak (only if no backup exists yet)
"""
import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "korvus_server.py")

ANCHOR = 'login_manager.login_view = "login_page"'
INSERT = (
    "\nfrom korvus_admin import korvus_admin\n"
    "from korvus_promo_api import korvus_promo_api\n"
    "app.register_blueprint(korvus_admin)\n"
    "app.register_blueprint(korvus_promo_api)\n"
)


def main():
    if not os.path.exists(PATH):
        sys.exit(f"Can't find {PATH} — run from the project folder.")

    with open(PATH, encoding="utf-8") as f:
        src = f.read()

    if "from korvus_promo_api import" in src:
        print("Already registered — nothing to do.")
        return

    if ANCHOR not in src:
        sys.exit(f"ABORTED — wrote nothing. Couldn't find anchor:\n  {ANCHOR}")

    out = src.replace(ANCHOR, ANCHOR + INSERT, 1)

    if not os.path.exists(PATH + ".bak"):
        shutil.copy2(PATH, PATH + ".bak")
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(out)

    print("Registered blueprints in korvus_server.py  (backup: korvus_server.py.bak)")
    print("Now run:  sudo systemctl restart korvus-server")


if __name__ == "__main__":
    main()

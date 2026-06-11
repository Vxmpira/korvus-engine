#!/usr/bin/env python3
"""
patch_autofill.py — wires the AI Auto-fill + Background add-ons into Korvus.

Upload these into your project folder (same dir as korvus_server.py / agent.html):
    korvus_fill_api.py
    agent_autofill.js
    agent_backgrounds.js
    patch_autofill.py

Then run from that folder:
    python3 patch_autofill.py
    sudo systemctl restart korvus-server

It makes small, idempotent edits (each with a one-time .bak backup):
  1. korvus_server.py — registers the korvus_fill_api blueprint
  2. agent.html       — injects, before </body>:
         <script src="/agent_backgrounds.js"></script>
         <script src="/agent_autofill.js"></script>

Safe: never reconstructs your files, skips work already done, and aborts a step
(writing nothing) if it can't find a known anchor — printing what to add by hand.

NOTE (git): this edits two tracked files on the server. Either commit the result,
or just re-run this script after each `git pull` (it's idempotent).
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "korvus_server.py")
AGENT = os.path.join(HERE, "agent.html")

SERVER_ANCHORS = [
    "app.register_blueprint(korvus_promo_api)",
    "app.register_blueprint(korvus_admin)",
    'login_manager.login_view = "login_page"',
]
SERVER_INSERT = (
    "\nfrom korvus_fill_api import korvus_fill_api\n"
    "app.register_blueprint(korvus_fill_api)\n"
)

# Order matters: backgrounds first (defines window.korvusBg), then autofill.
SCRIPT_TAGS = [
    '<script src="/agent_backgrounds.js"></script>',
    '<script src="/agent_autofill.js"></script>',
]


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, t):
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)


def _backup(p):
    if not os.path.exists(p + ".bak"):
        shutil.copy2(p, p + ".bak")


def patch_server():
    if not os.path.exists(SERVER):
        print(f"  [server] SKIP — {SERVER} not found")
        return
    src = _read(SERVER)
    if "korvus_fill_api" in src:
        print("  [server] already registered — nothing to do")
        return
    for anchor in SERVER_ANCHORS:
        if anchor in src:
            _backup(SERVER)
            _write(SERVER, src.replace(anchor, anchor + SERVER_INSERT, 1))
            print(f"  [server] registered blueprint (after: {anchor})")
            return
    print("  [server] ABORTED — wrote nothing. Add these 2 lines next to your other")
    print("           app.register_blueprint(...) calls in korvus_server.py:")
    for line in SERVER_INSERT.strip().splitlines():
        print("             " + line)


def patch_agent():
    if not os.path.exists(AGENT):
        print(f"  [agent] SKIP — {AGENT} not found")
        return
    src = _read(AGENT)
    to_add = [tag for tag in SCRIPT_TAGS if tag.split('"')[1] not in src]
    if not to_add:
        print("  [agent] both scripts already included — nothing to do")
        return
    _backup(AGENT)
    inject = "".join("  " + tag + "\n" for tag in to_add)
    if "</body>" in src:
        out = src.replace("</body>", inject + "</body>", 1)
        note = "before </body>"
    else:
        out = src.rstrip() + "\n" + inject
        note = "appended at end (no </body> found)"
    _write(AGENT, out)
    added = ", ".join(t.split('"')[1] for t in to_add)
    print(f"  [agent] added: {added} ({note})")


def main():
    print("KORVUS — wiring AI Auto-fill + Background add-ons")
    print("-" * 48)
    missing = [f for f in ("korvus_fill_api.py", "agent_autofill.js", "agent_backgrounds.js")
               if not os.path.exists(os.path.join(HERE, f))]
    if missing:
        print("  !! Upload these into this folder first: " + ", ".join(missing))
        print("-" * 48)
    patch_server()
    patch_agent()
    print("-" * 48)
    print("Done. Restart:  sudo systemctl restart korvus-server")
    print("Backups written as *.bak.")


if __name__ == "__main__":
    main()

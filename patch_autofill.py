#!/usr/bin/env python3
"""
patch_autofill.py — wires the AI Auto-fill add-on into Korvus.

Upload these three files into your project folder (same dir as korvus_server.py
and agent.html) first:

    korvus_fill_api.py
    agent_autofill.js
    patch_autofill.py

Then run from that folder:

    python patch_autofill.py
    sudo systemctl restart korvus-server

It makes two SMALL, idempotent edits (each with a one-time .bak backup):

  1. korvus_server.py — registers the korvus_fill_api blueprint
       from korvus_fill_api import korvus_fill_api
       app.register_blueprint(korvus_fill_api)

  2. agent.html — injects, right before </body>:
       <script src="/agent_autofill.js"></script>

Safe: it never reconstructs your files, skips work already done, and aborts a
step (writing nothing) if it can't find a known anchor — printing exactly what
to add by hand if so.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "korvus_server.py")
AGENT = os.path.join(HERE, "agent.html")

# Try these anchors in order; insert the registration right after the first hit.
SERVER_ANCHORS = [
    "app.register_blueprint(korvus_promo_api)",
    "app.register_blueprint(korvus_admin)",
    'login_manager.login_view = "login_page"',
]
SERVER_INSERT = (
    "\nfrom korvus_fill_api import korvus_fill_api\n"
    "app.register_blueprint(korvus_fill_api)\n"
)
SCRIPT_TAG = '<script src="/agent_autofill.js"></script>'


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _backup(path):
    if not os.path.exists(path + ".bak"):
        shutil.copy2(path, path + ".bak")


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
            out = src.replace(anchor, anchor + SERVER_INSERT, 1)
            _backup(SERVER)
            _write(SERVER, out)
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
    if "agent_autofill.js" in src:
        print("  [agent] script already included — nothing to do")
        return
    _backup(AGENT)
    if "</body>" in src:
        out = src.replace("</body>", "  " + SCRIPT_TAG + "\n</body>", 1)
        note = "before </body>"
    else:
        out = src.rstrip() + "\n" + SCRIPT_TAG + "\n"
        note = "appended at end (no </body> found)"
    _write(AGENT, out)
    print(f"  [agent] added auto-fill script ({note})")


def main():
    print("KORVUS — wiring AI Auto-fill add-on")
    print("-" * 40)
    missing = [f for f in ("korvus_fill_api.py", "agent_autofill.js")
               if not os.path.exists(os.path.join(HERE, f))]
    if missing:
        print("  !! Upload these into this folder first: " + ", ".join(missing))
        print("-" * 40)
    patch_server()
    patch_agent()
    print("-" * 40)
    print("Done. Restart the server:  sudo systemctl restart korvus-server")
    print("Backups written as *.bak (keep or delete as you like).")


if __name__ == "__main__":
    main()

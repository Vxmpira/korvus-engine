#!/usr/bin/env python3
"""
patch_dashboard.py — adds the Promo Studio + Admin buttons to korvus_dashboard.html

Why a patch instead of a whole new file: this edits YOUR real dashboard in
place, so nothing already in it is lost.

Safety:
  - idempotent: if it's already patched, it does nothing
  - if any expected spot can't be found, it ABORTS and writes NOTHING
    (so it can never leave you with a half-broken page)
  - saves korvus_dashboard.html.bak before changing anything

Run it (pure standard library — no venv, no packages needed):
    python3 patch_dashboard.py

This is a static-file change, so it takes effect on the next page load — no
restart needed for the dashboard itself. The buttons only light up once the
korvus_server.py edits (blueprint register + is_admin in /api/me) are also
live and the service is restarted — see chat.
"""
import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "korvus_dashboard.html")

# ---- what we insert -------------------------------------------------------
BUTTONS = (
    '    <a class="adminbtn" href="/agent" id="agentBtn" style="display:none;">\u27f6 Promo Studio</a>\n'
    '    <a class="adminbtn admin" href="/admin" id="adminBtn" style="display:none;">\u2699 Admin</a>\n'
)

# uses tokens that exist in your stylesheet (var(--mono), var(--gold),
# var(--gold-line)); crimson is the literal you already use (rgba 255,31,68)
CSS = (
    "  .adminbtn{margin-left:8px;font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;\n"
    "    text-decoration:none;padding:6px 13px;border-radius:999px;align-self:center;transition:all .2s;\n"
    "    color:#0a0d12;background:#ff1f44;border:1px solid #ff1f44;}\n"
    "  .adminbtn:hover{filter:brightness(1.08);}\n"
    "  .adminbtn.admin{color:var(--gold);background:transparent;border:1px solid var(--gold-line);}\n"
    "  .adminbtn.admin:hover{border-color:var(--gold);}\n"
)

REVEAL = (
    "\n      if(me.is_admin){['agentBtn','adminBtn'].forEach(function(id){"
    "var el=document.getElementById(id); if(el) el.style.display='inline-flex';});}"
)

# ---- anchors we look for (verbatim, from your file) -----------------------
LOGOUT_ANCHOR = '<a class="logoutbtn" href="/logout"'
STYLE_CLOSE   = "</style>"
REVEAL_ANCHOR = "lo.style.display = 'inline-block';"


def main():
    if not os.path.exists(PATH):
        sys.exit(f"Can't find {PATH} — run this from the project folder.")

    with open(PATH, encoding="utf-8") as f:
        html = f.read()

    if 'id="agentBtn"' in html:
        print("Already patched — nothing to do.")
        return

    problems = []
    if LOGOUT_ANCHOR not in html:
        problems.append("logout button line not found")
    if STYLE_CLOSE not in html:
        problems.append("</style> not found")
    if REVEAL_ANCHOR not in html:
        problems.append("loadMe() logout-show line not found")
    if problems:
        print("ABORTED — wrote nothing. Couldn't locate:")
        for p in problems:
            print("   -", p)
        print("Your file is untouched. Paste me the header block and the loadMe()"
              " function and I'll adjust the anchors.")
        sys.exit(1)

    # 1) insert the two buttons on the line above the logout link
    idx = html.index(LOGOUT_ANCHOR)
    line_start = html.rfind("\n", 0, idx) + 1
    html = html[:line_start] + BUTTONS + html[line_start:]

    # 2) add the CSS just before the first </style>
    html = html.replace(STYLE_CLOSE, CSS + STYLE_CLOSE, 1)

    # 3) reveal the buttons for admins, right after the logout-show line
    html = html.replace(REVEAL_ANCHOR, REVEAL_ANCHOR + REVEAL, 1)

    shutil.copy2(PATH, PATH + ".bak")
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print("Patched korvus_dashboard.html  (backup: korvus_dashboard.html.bak)")
    print("Reload the dashboard once the korvus_server.py edits are live.")


if __name__ == "__main__":
    main()

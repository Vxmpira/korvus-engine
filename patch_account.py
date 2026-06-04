#!/usr/bin/env python3
"""
patch_account.py  (updated) — adds owner-only PROMO STUDIO + ADMIN links to the
account page, keyed off "is_owner" (which your live /api/me actually returns).

Re-runnable: it strips any earlier injected block (including the old is_admin
version) and writes a fresh one, so running it again just upgrades in place.

    python3 patch_account.py                 # defaults to korvus_account.html
    python3 patch_account.py <filename.html> # if your account page is named differently
"""
import os
import sys
import glob
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
fname = sys.argv[1] if len(sys.argv) > 1 else "korvus_account.html"
PATH = os.path.join(HERE, fname)

START  = "<!-- korvus-admin-links:start -->"
END    = "<!-- korvus-admin-links:end -->"
LEGACY = "<!-- korvus-admin-links v1 -->"

BLOCK = START + """
<script>
(function(){
  function find(hrefSub, textRe){
    var els = document.querySelectorAll('a,button'), i;
    for(i=0;i<els.length;i++){ var h=els[i].getAttribute('href')||''; if(hrefSub && h.indexOf(hrefSub)!==-1) return els[i]; }
    for(i=0;i<els.length;i++){ if(textRe.test((els[i].textContent||'').trim())) return els[i]; }
    return null;
  }
  fetch('/api/me',{cache:'no-store',credentials:'same-origin'})
    .then(function(r){return r.json();})
    .then(function(me){
      if(!me || !(me.is_owner || me.is_admin)) return;
      var logout=find('logout',/log\\s*out/i), terminal=find('terminal',/terminal/i);
      var styleSrc=terminal||logout, anchor=logout||terminal;
      if(!anchor || !anchor.parentNode) return;
      function mk(t,h){var a=document.createElement('a');a.href=h;a.textContent=t;if(styleSrc&&styleSrc.className)a.className=styleSrc.className;a.style.textDecoration='none';return a;}
      anchor.parentNode.insertBefore(mk('PROMO STUDIO','/agent'),anchor);
      anchor.parentNode.insertBefore(mk('ADMIN','/admin'),anchor);
    })
    .catch(function(){});
})();
</script>
""" + END + "\n"


def strip_existing(html):
    while START in html and END in html:
        a = html.index(START)
        b = html.index(END, a) + len(END)
        html = html[:a] + html[b:]
    while LEGACY in html:                      # remove the old is_admin version
        a = html.index(LEGACY)
        s = html.index("</script>", a) + len("</script>")
        html = html[:a] + html[s:]
    return html


def main():
    if not os.path.exists(PATH):
        print(f"Couldn't find {PATH}")
        others = [os.path.basename(p) for p in glob.glob(os.path.join(HERE, "*.html"))]
        if others:
            print("HTML files here — re-run with the right one:")
            for o in others:
                print("   python3 patch_account.py", o)
        sys.exit(1)

    with open(PATH, encoding="utf-8") as f:
        html = f.read()

    cleaned = strip_existing(html)
    idx = cleaned.rfind("</body>")
    if idx == -1:
        sys.exit("ABORTED — no </body> found, wrote nothing.")

    out = cleaned[:idx] + BLOCK + cleaned[idx:]

    if not os.path.exists(PATH + ".bak"):
        shutil.copy2(PATH, PATH + ".bak")
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(out)

    print(f"Patched {fname} (links keyed off is_owner).")
    print("Reload /account as the owner — PROMO STUDIO + ADMIN appear by TERMINAL / LOG OUT.")


if __name__ == "__main__":
    main()

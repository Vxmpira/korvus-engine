#!/usr/bin/env python3
"""
patch_account.py — adds admin-only PROMO STUDIO + ADMIN links to the Account page.

It injects a tiny <script> before </body> that:
  - calls /api/me
  - if (and only if) you're admin, inserts two links next to TERMINAL / LOG OUT
  - clones the existing buttons' style so they match automatically

Works no matter how the page is rendered (Jinja, string-injected, or static),
because it only needs the page to load and to have a logout link to anchor to.

Safe + idempotent:
  - skips if already injected
  - aborts and writes nothing if it can't find the page or </body>
  - backs up <file>.bak first

Run it (pure stdlib):
    python3 patch_account.py                 # defaults to korvus_account.html
    python3 patch_account.py <filename.html> # if your account page is named differently

PREREQUISITE: run patch_server.py + restart first, or /api/me won't report
is_admin and /agent /admin won't exist.
"""
import os
import sys
import glob
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
fname = sys.argv[1] if len(sys.argv) > 1 else "korvus_account.html"
PATH = os.path.join(HERE, fname)

MARKER = "korvus-admin-links"

SCRIPT_BLOCK = """
<!-- korvus-admin-links v1 -->
<script>
(function(){
  function findByHrefOrText(hrefSub, textRe){
    var els = document.querySelectorAll('a,button');
    var i;
    for(i=0;i<els.length;i++){
      var h = els[i].getAttribute('href') || '';
      if(hrefSub && h.indexOf(hrefSub) !== -1) return els[i];
    }
    for(i=0;i<els.length;i++){
      if(textRe.test((els[i].textContent||'').trim())) return els[i];
    }
    return null;
  }
  fetch('/api/me', {cache:'no-store', credentials:'same-origin'})
    .then(function(r){ return r.json(); })
    .then(function(me){
      if(!me || !me.is_admin) return;
      var logout   = findByHrefOrText('logout', /log\\s*out/i);
      var terminal = findByHrefOrText('terminal', /terminal/i);
      var styleSrc = terminal || logout;
      var anchor   = logout || terminal;
      if(!anchor || !anchor.parentNode) return;
      function mk(text, href){
        var a = document.createElement('a');
        a.href = href; a.textContent = text;
        if(styleSrc && styleSrc.className) a.className = styleSrc.className;
        a.style.textDecoration = 'none';
        return a;
      }
      anchor.parentNode.insertBefore(mk('PROMO STUDIO', '/agent'), anchor);
      anchor.parentNode.insertBefore(mk('ADMIN', '/admin'), anchor);
    })
    .catch(function(){ /* not logged in / no endpoint: show nothing */ });
})();
</script>
"""


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

    if MARKER in html:
        print("Already patched — nothing to do.")
        return

    idx = html.rfind("</body>")
    if idx == -1:
        sys.exit("ABORTED — no </body> found, wrote nothing.")

    html = html[:idx] + SCRIPT_BLOCK + "\n" + html[idx:]
    shutil.copy2(PATH, PATH + ".bak")
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Patched {fname}  (backup: {fname}.bak)")
    print("Reload /account while logged in as the admin — PROMO STUDIO + ADMIN appear by TERMINAL / LOG OUT.")


if __name__ == "__main__":
    main()

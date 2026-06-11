/* ============================================================================
   Korvus Promo Studio — AI Auto-fill + Caption add-on
   ----------------------------------------------------------------------------
   Served at /agent_autofill.js; injected before </body> by patch_autofill.py.
   Runs AFTER agent.html's inline script (uses S, syncInputs, render) and after
   agent_backgrounds.js (uses window.korvusBg).

   Adds:
     • "✦ Auto-fill this layout with AI" — fills the layout. Korvus pulls the
       live engine feed; other brands read the brand's front page. Also applies
       a suggested background, accent, AND a brand-correct footer price line
       (fixes the Statement footer that used to hardcode Korvus's $3.99).
     • "✎ Caption + hashtags" — writes a platform caption + current hashtags for
       the brand (live web search when available), based on the on-screen ad.
   ========================================================================== */
(function () {
  "use strict";

  function ready(fn) { document.readyState !== "loading" ? fn() : document.addEventListener("DOMContentLoaded", fn); }
  function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
  function normUrl(u) { u = String(u || "").trim(); if (!u) return ""; if (!/^https?:\/\//i.test(u)) u = "https://" + u; return u; }

  window.korvusFill = window.korvusFill || { footer: null };

  ready(function () {
    if (typeof S === "undefined" || typeof syncInputs !== "function" || typeof render !== "function") {
      console.warn("[autofill] agent.html globals not found — load this AFTER the page script.");
      return;
    }

    /* ---- override the hardcoded Statement footer price after each render ---- */
    function applyFooter() {
      if (S.layout !== "statement") return;
      const f = window.korvusFill.footer;
      if (!f) return;
      const cv = document.getElementById("creative");
      const pr = cv && cv.querySelector(".botrow .pr");
      if (pr) pr.textContent = f;
    }

    /* ---- auto-fit: scale the type down so nothing overflows the frame
       (the creative is overflow:hidden with a fixed height, so a tall layout
       like Engine clips at the bottom in Square 1:1 — this prevents that) ---- */
    function fitToFrame() {
      const cv = document.getElementById("creative");
      if (!cv) return;
      cv.style.fontSize = "";                       // restore natural calc(15px * --k)
      const avail = cv.clientHeight;
      if (!avail) return;
      if (cv.scrollHeight <= avail + 1) return;      // already fits — leave it alone
      const base = parseFloat(getComputedStyle(cv).fontSize) || 15;
      let fs = base * (avail / cv.scrollHeight) * 0.985;   // proportional first guess
      cv.style.fontSize = fs + "px";
      let guard = 0;                                  // refine for line-wrap nonlinearity
      while (cv.scrollHeight > cv.clientHeight + 1 && guard < 14 && fs > 5) {
        fs *= 0.97; cv.style.fontSize = fs + "px"; guard++;
      }
    }

    const _render = render;
    render = function () {
      const out = _render.apply(this, arguments);
      try { applyFooter(); } catch (e) {}
      try { fitToFrame(); } catch (e) {}
      return out;
    };
    window.render = render;

    /* ---- build + insert the two sidebar groups ---- */
    const afBlock = el(`
      <div class="grp" id="af-grp">
        <span class="lbl">AI auto-fill</span>
        <button class="mini" id="af-go" style="margin-bottom:8px">\u2726 Auto-fill this layout with AI</button>
        <div class="toggle" style="margin-bottom:8px">
          <span id="af-live-label">Use live engine feed</span>
          <div class="sw on" id="af-live"></div>
        </div>
        <div class="ai-note" id="af-note">Fills every field for the current layout. Press again for a fresh take.</div>
        <div class="ai-note" id="af-ctx" style="margin-top:6px;display:none"></div>
      </div>`);
    const capBlock = el(`
      <div class="grp" id="cap-grp">
        <span class="lbl">Caption + hashtags</span>
        <div class="field" style="margin:0 0 8px"><select id="cap-platform">
          <option>Instagram</option><option>X</option><option>TikTok</option><option>LinkedIn</option></select></div>
        <button class="mini" id="cap-go" style="margin-bottom:8px">\u270E Write caption + hashtags</button>
        <div class="ai-note" id="cap-note">Uses the current ad + brand site to write a caption and current hashtags.</div>
        <div id="cap-out" style="display:none;margin-top:8px"></div>
      </div>`);

    const aside = document.querySelector("aside.controls") || document.querySelector("aside") || document.querySelector(".controls");
    const anchor = (aside && aside.querySelector(".lay-fields")) || (aside && aside.firstElementChild);
    if (aside && anchor) { aside.insertBefore(afBlock, anchor); afBlock.insertAdjacentElement("afterend", capBlock); }
    else if (aside) { aside.appendChild(afBlock); aside.appendChild(capBlock); }
    else { document.body.appendChild(afBlock); document.body.appendChild(capBlock); }

    const btn = document.getElementById("af-go");
    const note = document.getElementById("af-note");
    const ctxBox = document.getElementById("af-ctx");
    const liveSw = document.getElementById("af-live");
    const liveLabel = document.getElementById("af-live-label");

    let useLive = true;
    liveSw.addEventListener("click", () => { useLive = !useLive; liveSw.classList.toggle("on", useLive); if (!useLive) ctxBox.style.display = "none"; });

    function updateLiveLabel() {
      liveLabel.textContent = (S.brand || "korvus") === "korvus" ? "Use live engine feed" : "Pull from the brand's site";
    }
    updateLiveLabel();
    const brandSeg = document.getElementById("seg-brand");
    if (brandSeg) brandSeg.addEventListener("click", () => setTimeout(() => {
      window.korvusFill.footer = null; updateLiveLabel(); try { render(); } catch (e) {}
    }, 0));

    /* ---- Korvus live context (engine feed) ---- */
    function rankImp(imp) { imp = String(imp || "").toLowerCase(); return imp === "high" ? 3 : imp === "med" ? 2 : imp === "low" ? 1 : 0; }
    async function pullKorvusContext() {
      const ctx = {};
      try {
        const r = await fetch("/api/news", { cache: "no-store", credentials: "same-origin" });
        if (r.ok) {
          const d = await r.json();
          const list = (d.items || []).filter(n => n && n.noise !== true);
          list.sort((a, b) => { const k = rankImp(b.impact) - rankImp(a.impact); return k || (new Date(b.time || 0) - new Date(a.time || 0)); });
          const t = list[0];
          if (t) {
            ctx.headline = t.headline || ""; ctx.summary = t.summary || ""; ctx.impact = String(t.impact || "").toUpperCase();
            ctx.direction = t.dir || "neut"; ctx.confidence = t.conf || 0; ctx.instruments = t.inst || []; ctx.time = t.time || "";
          }
        }
      } catch (e) {}
      try {
        const r = await fetch("/api/smt", { cache: "no-store", credentials: "same-origin" });
        if (r.ok) { const d = await r.json(); if (d && d.verdict) ctx.smt = (d.verdict.title ? d.verdict.title + " — " : "") + (d.verdict.note || ""); }
      } catch (e) {}
      return ctx;
    }

    function applyFill(data) {
      const fields = (data && (data.fields || data)) || {};
      window.korvusFill.footer = (data && data.footer) || null;
      if (data && data.accent && ["indigo", "gold", "crimson"].indexOf(data.accent) >= 0) S.accent = data.accent;
      Object.keys(fields).forEach(k => {
        if (k === "e1" || k === "e2") { if (fields[k] && typeof fields[k] === "object") S[k] = Object.assign({}, S[k], fields[k]); }
        else if (k in S) S[k] = fields[k];
      });
      syncInputs();
      render();
      if (data && data.bg && window.korvusBg && typeof window.korvusBg.apply === "function") window.korvusBg.apply(data.bg);
    }

    let filledOnce = false;
    btn.addEventListener("click", async function () {
      btn.disabled = true; note.className = "ai-note"; btn.textContent = "Thinking\u2026"; note.textContent = "Drafting a full fill\u2026";
      const brand = S.brand || "korvus", isKorvus = brand === "korvus";
      let context = null, brandUrl = null;
      try {
        if (useLive && isKorvus) {
          note.textContent = "Reading the live engine feed\u2026";
          context = await pullKorvusContext();
          if (context && context.headline) {
            const h = context.headline.length > 80 ? context.headline.slice(0, 77) + "\u2026" : context.headline;
            ctxBox.style.display = ""; ctxBox.innerHTML = "live \u2192 " + (context.impact ? "[" + esc(context.impact) + "] " : "") + esc(h);
          } else { ctxBox.style.display = "none"; note.textContent = "No live items \u2014 writing evergreen copy\u2026"; context = null; }
        } else if (useLive && !isKorvus) {
          brandUrl = normUrl(S.url);
          const host = brandUrl ? brandUrl.replace(/^https?:\/\//i, "").replace(/\/.*$/, "") : "";
          ctxBox.style.display = host ? "" : "none"; if (host) ctxBox.innerHTML = "reading site \u2192 " + esc(host);
          note.textContent = "Reading the brand's front page\u2026";
        } else { ctxBox.style.display = "none"; }

        const res = await fetch("/api/promo-fill", {
          method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
          body: JSON.stringify({ layout: S.layout, brand: brand, context: context, brand_url: brandUrl })
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
        applyFill(data);
        filledOnce = true; note.className = "ai-note";
        note.textContent = !isKorvus
          ? (data.grounded ? "Filled from " + esc(data.brand_name || "the site") + ". Press \u21BB for another take."
                           : "Couldn't read the site \u2014 wrote evergreen copy. Press \u21BB to retry.")
          : (data.grounded ? "Filled from the live feed. Press \u21BB for a different take."
                           : "Done. Press \u21BB to regenerate a fresh take.");
        btn.textContent = "\u21BB Regenerate";
      } catch (err) {
        note.className = "ai-note"; note.textContent = "Couldn\u2019t auto-fill: " + (err.message || err);
        btn.textContent = filledOnce ? "\u21BB Regenerate" : "\u2726 Auto-fill this layout with AI";
      } finally { btn.disabled = false; }
    });

    /* ---- Caption + hashtags ---- */
    const capBtn = document.getElementById("cap-go");
    const capNote = document.getElementById("cap-note");
    const capOut = document.getElementById("cap-out");
    const capPlat = document.getElementById("cap-platform");

    function adCopyText() {
      const p = [];
      if (S.layout === "engine") { [S.e_h1, S.e_h2, S.e_h3].forEach(x => x && p.push(x)); if (S.e_sub) p.push(S.e_sub); }
      else {
        [S.c_hmuted, S.c_hlight, S.c_haccent].forEach(x => x && p.push(x));
        if (S.c_sub) p.push(S.c_sub);
        const cc = [S.c_calla, S.c_callb, S.c_callbold].filter(Boolean).join(" ");
        if (cc) p.push(cc);
      }
      return p.join(" / ");
    }

    capBtn.addEventListener("click", async function () {
      capBtn.disabled = true; const lbl = capBtn.textContent; capBtn.textContent = "Writing\u2026";
      capNote.className = "ai-note"; capNote.textContent = "Finding current hashtags\u2026"; capOut.style.display = "none";
      try {
        const res = await fetch("/api/promo-caption", {
          method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
          body: JSON.stringify({ platform: capPlat.value, brand: S.brand || "korvus", brand_url: normUrl(S.url), copy: adCopyText() })
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
        const tags = data.hashtags || [];
        const chips = tags.map(t => '<span class="ai-chip" style="display:inline-block;cursor:default;margin:0 4px 4px 0;padding:4px 8px">' + esc(t) + "</span>").join("");
        const copyText = (data.caption || "") + (tags.length ? "\n\n" + tags.join(" ") : "");
        capOut.style.display = "";
        capOut.innerHTML =
          '<div class="ai-chip" style="cursor:default;white-space:pre-wrap;line-height:1.5">' + esc(data.caption || "") + "</div>" +
          (chips ? '<div style="margin-top:8px">' + chips + "</div>" : "") +
          '<button class="mini" id="cap-copy" style="margin-top:8px">\u29C9 Copy caption + hashtags</button>';
        capNote.textContent = (data.platform || "") + " caption ready.";
        const cc = document.getElementById("cap-copy");
        cc.addEventListener("click", () => {
          navigator.clipboard.writeText(copyText)
            .then(() => { cc.textContent = "\u2713 Copied"; setTimeout(() => cc.textContent = "\u29C9 Copy caption + hashtags", 1400); })
            .catch(() => { cc.textContent = "Copy failed"; });
        });
      } catch (err) {
        capNote.className = "ai-note"; capNote.textContent = "Couldn\u2019t write caption: " + (err.message || err);
      } finally { capBtn.disabled = false; capBtn.textContent = lbl; }
    });

    /* ---- fit the initial render too (covers a Square 1:1 that's already on screen) ---- */
    try { render(); } catch (e) {}
  });
})();

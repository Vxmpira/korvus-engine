/* ============================================================================
   Korvus Promo Studio — AI Auto-fill add-on
   ----------------------------------------------------------------------------
   This file is served by korvus_fill_api.py at /agent_autofill.js and is wired
   into agent.html by patch_autofill.py, which injects right before </body>:

       <script src="/agent_autofill.js"></script>

   It runs AFTER agent.html's own <script>, so it can use the globals that
   script defines: S, syncInputs, render.

   It adds, near the top of the controls sidebar:
     • "✦ Auto-fill this layout with AI" — fills EVERY field for the current
        layout in one click. Press again -> a fresh take (button becomes ↻).
     • "Use live engine feed" toggle — when on, it pulls the top scored item
        from /api/news (+ the SMT read from /api/smt) and grounds the mockup in
        the real, current market story. Off -> evergreen Korvus copy.

   Backend: POST /api/promo-fill  (korvus_fill_api.py). Owner-gated.
   ========================================================================== */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstChild;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  ready(function () {
    // These globals come from agent.html's inline script.
    if (typeof S === "undefined" || typeof syncInputs !== "function" || typeof render !== "function") {
      console.warn("[autofill] agent.html globals not found — load this AFTER the page script.");
      return;
    }

    /* ---------- build + insert the UI block ---------- */
    const block = el(`
      <div class="grp" id="af-grp">
        <span class="lbl">AI auto-fill</span>
        <button class="mini" id="af-go" style="margin-bottom:8px">\u2726 Auto-fill this layout with AI</button>
        <div class="toggle" style="margin-bottom:8px">
          <span>Use live engine feed</span>
          <div class="sw on" id="af-live"></div>
        </div>
        <div class="ai-note" id="af-note">Fills every field for the current layout. Press again for a fresh take.</div>
        <div class="ai-note" id="af-ctx" style="margin-top:6px;display:none"></div>
      </div>
    `);

    const aside = document.querySelector("aside.controls") || document.querySelector("aside") || document.querySelector(".controls");
    const anchor = (aside && aside.querySelector(".lay-fields")) || (aside && aside.firstElementChild);
    if (aside && anchor) aside.insertBefore(block, anchor);
    else if (aside) aside.appendChild(block);
    else document.body.appendChild(block);

    const btn    = document.getElementById("af-go");
    const note   = document.getElementById("af-note");
    const ctxBox = document.getElementById("af-ctx");
    const liveSw = document.getElementById("af-live");

    let useLive = true;
    liveSw.addEventListener("click", () => {
      useLive = !useLive;
      liveSw.classList.toggle("on", useLive);
      if (!useLive) ctxBox.style.display = "none";
    });

    /* ---------- live context puller (mirrors korvus_promo_studio.html) ---------- */
    function rankImp(imp) {
      imp = String(imp || "").toLowerCase();
      return imp === "high" ? 3 : imp === "med" ? 2 : imp === "low" ? 1 : 0;
    }

    async function pullContext() {
      const ctx = {};
      // top scored news item
      try {
        const r = await fetch("/api/news", { cache: "no-store", credentials: "same-origin" });
        if (r.ok) {
          const d = await r.json();
          const list = (d.items || []).filter(n => n && n.noise !== true);
          list.sort((a, b) => {
            const k = rankImp(b.impact) - rankImp(a.impact);
            if (k) return k;
            return new Date(b.time || 0) - new Date(a.time || 0);
          });
          const t = list[0];
          if (t) {
            ctx.headline    = t.headline || "";
            ctx.summary     = t.summary || "";
            ctx.impact      = String(t.impact || "").toUpperCase();
            ctx.direction   = t.dir || "neut";
            ctx.confidence  = t.conf || 0;
            ctx.instruments = t.inst || [];
            ctx.time        = t.time || "";
          }
        }
      } catch (e) { /* offline / not logged in -> evergreen */ }

      // SMT divergence read (optional, non-fatal)
      try {
        const r = await fetch("/api/smt", { cache: "no-store", credentials: "same-origin" });
        if (r.ok) {
          const d = await r.json();
          if (d && d.verdict) {
            ctx.smt = (d.verdict.title ? d.verdict.title + " — " : "") + (d.verdict.note || "");
          }
        }
      } catch (e) { /* ignore */ }

      return ctx;
    }

    /* ---------- map the returned fields onto S, then redraw ---------- */
    function applyFill(fields) {
      if (!fields || typeof fields !== "object") return;
      Object.keys(fields).forEach(k => {
        if (k === "e1" || k === "e2") {
          if (fields[k] && typeof fields[k] === "object") S[k] = Object.assign({}, S[k], fields[k]);
        } else if (k in S) {
          S[k] = fields[k];
        }
      });
      syncInputs();
      render();
    }

    /* ---------- the click: pull context -> generate -> apply ---------- */
    let filledOnce = false;
    btn.addEventListener("click", async function () {
      btn.disabled = true;
      note.className = "ai-note";
      btn.textContent = "Thinking\u2026";
      note.textContent = "Drafting a full fill\u2026";

      try {
        let context = null;
        if (useLive) {
          note.textContent = "Reading the live engine feed\u2026";
          context = await pullContext();
          if (context && context.headline) {
            const h = context.headline.length > 80 ? context.headline.slice(0, 77) + "\u2026" : context.headline;
            ctxBox.style.display = "";
            ctxBox.innerHTML = "live \u2192 " + (context.impact ? "[" + esc(context.impact) + "] " : "") + esc(h);
          } else {
            ctxBox.style.display = "none";
            note.textContent = "No live items right now \u2014 writing evergreen copy\u2026";
            context = null;
          }
        } else {
          ctxBox.style.display = "none";
        }

        const res = await fetch("/api/promo-fill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ layout: S.layout, context: context })
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));

        applyFill(data.fields || data);
        filledOnce = true;
        note.className = "ai-note";
        note.textContent = data.grounded ? "Filled from the live feed. Press \u21BB for a different take."
                                         : "Done. Press \u21BB to regenerate a fresh take.";
        btn.textContent = "\u21BB Regenerate";
      } catch (err) {
        note.className = "ai-note";
        note.textContent = "Couldn\u2019t auto-fill: " + (err.message || err);
        btn.textContent = filledOnce ? "\u21BB Regenerate" : "\u2726 Auto-fill this layout with AI";
      } finally {
        btn.disabled = false;
      }
    });
  });
})();

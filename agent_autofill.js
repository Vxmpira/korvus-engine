/* ============================================================================
   Korvus Promo Studio — AI Auto-fill add-on
   ----------------------------------------------------------------------------
   Served at /agent_autofill.js by korvus_fill_api.py; injected before </body>
   by patch_autofill.py. Runs AFTER agent.html's inline script, so it uses the
   globals that script defines: S, syncInputs, render, and (optionally)
   window.korvusBg from agent_backgrounds.js.

   Adds "✦ Auto-fill this layout with AI":
     • Korvus brand  -> fills in the Korvus voice; if "live" is on, pulls the
       top scored item from /api/news (+ /api/smt) and grounds the mockup.
     • Other brands  -> the server reads that brand's front page and writes the
       ad to match it. ("live" toggle becomes "Pull from the brand's site".)
     • Every fill also applies a suggested background + accent (mood match).
     • Press again -> a fresh take (button becomes ↻ Regenerate).
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
  function normUrl(u) {
    u = String(u || "").trim();
    if (!u) return "";
    if (!/^https?:\/\//i.test(u)) u = "https://" + u;
    return u;
  }

  ready(function () {
    if (typeof S === "undefined" || typeof syncInputs !== "function" || typeof render !== "function") {
      console.warn("[autofill] agent.html globals not found — load this AFTER the page script.");
      return;
    }

    /* ---------- UI ---------- */
    const block = el(`
      <div class="grp" id="af-grp">
        <span class="lbl">AI auto-fill</span>
        <button class="mini" id="af-go" style="margin-bottom:8px">\u2726 Auto-fill this layout with AI</button>
        <div class="toggle" style="margin-bottom:8px">
          <span id="af-live-label">Use live engine feed</span>
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

    const btn = document.getElementById("af-go");
    const note = document.getElementById("af-note");
    const ctxBox = document.getElementById("af-ctx");
    const liveSw = document.getElementById("af-live");
    const liveLabel = document.getElementById("af-live-label");

    let useLive = true;
    liveSw.addEventListener("click", () => {
      useLive = !useLive;
      liveSw.classList.toggle("on", useLive);
      if (!useLive) ctxBox.style.display = "none";
    });

    function updateLiveLabel() {
      const isK = (S.brand || "korvus") === "korvus";
      liveLabel.textContent = isK ? "Use live engine feed" : "Pull from the brand's site";
    }
    updateLiveLabel();
    // refresh the label after the page's own brand handler runs
    const brandSeg = document.getElementById("seg-brand");
    if (brandSeg) brandSeg.addEventListener("click", () => setTimeout(updateLiveLabel, 0));

    /* ---------- Korvus live context (engine feed) ---------- */
    function rankImp(imp) {
      imp = String(imp || "").toLowerCase();
      return imp === "high" ? 3 : imp === "med" ? 2 : imp === "low" ? 1 : 0;
    }
    async function pullKorvusContext() {
      const ctx = {};
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
            ctx.headline = t.headline || "";
            ctx.summary = t.summary || "";
            ctx.impact = String(t.impact || "").toUpperCase();
            ctx.direction = t.dir || "neut";
            ctx.confidence = t.conf || 0;
            ctx.instruments = t.inst || [];
            ctx.time = t.time || "";
          }
        }
      } catch (e) { /* offline -> evergreen */ }
      try {
        const r = await fetch("/api/smt", { cache: "no-store", credentials: "same-origin" });
        if (r.ok) {
          const d = await r.json();
          if (d && d.verdict) ctx.smt = (d.verdict.title ? d.verdict.title + " — " : "") + (d.verdict.note || "");
        }
      } catch (e) { /* ignore */ }
      return ctx;
    }

    /* ---------- apply returned fields + bg/accent ---------- */
    function applyFill(data) {
      const fields = (data && (data.fields || data)) || {};
      if (data && data.accent && ["indigo", "gold", "crimson"].indexOf(data.accent) >= 0) {
        S.accent = data.accent;
      }
      Object.keys(fields).forEach(k => {
        if (k === "e1" || k === "e2") {
          if (fields[k] && typeof fields[k] === "object") S[k] = Object.assign({}, S[k], fields[k]);
        } else if (k in S) {
          S[k] = fields[k];
        }
      });
      syncInputs();
      render();
      if (data && data.bg && window.korvusBg && typeof window.korvusBg.apply === "function") {
        window.korvusBg.apply(data.bg);
      }
    }

    /* ---------- click: gather context -> generate -> apply ---------- */
    let filledOnce = false;
    btn.addEventListener("click", async function () {
      btn.disabled = true;
      note.className = "ai-note";
      btn.textContent = "Thinking\u2026";
      note.textContent = "Drafting a full fill\u2026";

      const brand = S.brand || "korvus";
      const isKorvus = brand === "korvus";
      let context = null;
      let brandUrl = null;

      try {
        if (useLive && isKorvus) {
          note.textContent = "Reading the live engine feed\u2026";
          context = await pullKorvusContext();
          if (context && context.headline) {
            const h = context.headline.length > 80 ? context.headline.slice(0, 77) + "\u2026" : context.headline;
            ctxBox.style.display = "";
            ctxBox.innerHTML = "live \u2192 " + (context.impact ? "[" + esc(context.impact) + "] " : "") + esc(h);
          } else {
            ctxBox.style.display = "none";
            note.textContent = "No live items right now \u2014 writing evergreen copy\u2026";
            context = null;
          }
        } else if (useLive && !isKorvus) {
          brandUrl = normUrl(S.url);
          const host = brandUrl ? brandUrl.replace(/^https?:\/\//i, "").replace(/\/.*$/, "") : "";
          ctxBox.style.display = host ? "" : "none";
          if (host) ctxBox.innerHTML = "reading site \u2192 " + esc(host);
          note.textContent = "Reading the brand's front page\u2026";
        } else {
          ctxBox.style.display = "none";
        }

        const res = await fetch("/api/promo-fill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ layout: S.layout, brand: brand, context: context, brand_url: brandUrl })
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));

        applyFill(data);
        filledOnce = true;
        note.className = "ai-note";
        if (!isKorvus) {
          note.textContent = data.grounded
            ? "Filled from " + esc(data.brand_name || "the site") + ". Press \u21BB for another take."
            : "Couldn't read the site \u2014 wrote evergreen brand copy. Press \u21BB to retry.";
        } else {
          note.textContent = data.grounded
            ? "Filled from the live feed. Press \u21BB for a different take."
            : "Done. Press \u21BB to regenerate a fresh take.";
        }
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

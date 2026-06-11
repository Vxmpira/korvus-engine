/* ============================================================================
   Korvus Promo Studio — Background add-on
   ----------------------------------------------------------------------------
   Served at /agent_backgrounds.js by korvus_fill_api.py; injected before </body>
   by patch_autofill.py. Runs AFTER agent.html's inline script.

   Adds a "Background" selector with 8 professionally-tuned presets that tint to
   the current accent, render crisply at any size, and export cleanly in the PNG
   (applied as inline background on #creative, so DOM-snapshot export keeps them).

   Exposes window.korvusBg.apply('mesh'|'glow'|...) so Auto-fill can set the
   AI-suggested background. Keys are kept in sync with korvus_fill_api.py.
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

  // accent -> base rgb (tasteful, tuned for dark canvases)
  const ACCENT_RGB = { indigo: [93, 108, 255], gold: [216, 167, 60], crimson: [209, 31, 23] };
  function rgba(rgb, a) { return "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + a + ")"; }

  // each preset returns inline style props for #creative; tinted by accent rgb
  const PRESETS = {
    none: function () {
      return { backgroundColor: "", backgroundImage: "", backgroundSize: "", backgroundPosition: "", backgroundRepeat: "" };
    },
    mesh: function (c) {
      return {
        backgroundColor: "#07090c",
        backgroundImage:
          "radial-gradient(at 18% 20%, " + rgba(c, .26) + " 0px, transparent 55%)," +
          "radial-gradient(at 82% 12%, rgba(120,90,255,.14) 0px, transparent 50%)," +
          "radial-gradient(at 72% 88%, " + rgba(c, .18) + " 0px, transparent 55%)",
        backgroundSize: "", backgroundPosition: "", backgroundRepeat: "no-repeat"
      };
    },
    glow: function (c) {
      return {
        backgroundColor: "#07090c",
        backgroundImage:
          "radial-gradient(120% 80% at 50% 116%, " + rgba(c, .30) + " 0%, transparent 60%)," +
          "radial-gradient(80% 50% at 50% -12%, rgba(255,255,255,.04), transparent 60%)",
        backgroundSize: "", backgroundPosition: "", backgroundRepeat: "no-repeat"
      };
    },
    grid: function (c) {
      return {
        backgroundColor: "#08090d",
        backgroundImage:
          "linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px)," +
          "linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px)," +
          "radial-gradient(120% 90% at 50% 0%, " + rgba(c, .12) + ", transparent 55%)",
        backgroundSize: "46px 46px, 46px 46px, 100% 100%",
        backgroundPosition: "", backgroundRepeat: ""
      };
    },
    aurora: function (c) {
      return {
        backgroundColor: "#070a10",
        backgroundImage:
          "linear-gradient(135deg, " + rgba(c, .22) + " 0%, transparent 40%)," +
          "linear-gradient(300deg, rgba(120,90,255,.16) 0%, transparent 44%)," +
          "radial-gradient(90% 60% at 82% 8%, " + rgba(c, .16) + ", transparent 60%)",
        backgroundSize: "", backgroundPosition: "", backgroundRepeat: "no-repeat"
      };
    },
    dots: function (c) {
      return {
        backgroundColor: "#08090d",
        backgroundImage:
          "radial-gradient(rgba(255,255,255,.06) 1.2px, transparent 1.4px)," +
          "radial-gradient(140% 90% at 50% 0%, " + rgba(c, .12) + ", transparent 55%)",
        backgroundSize: "22px 22px, 100% 100%",
        backgroundPosition: "0 0, 0 0", backgroundRepeat: ""
      };
    },
    graphite: function () {
      return {
        backgroundColor: "#0a0b0e",
        backgroundImage:
          "linear-gradient(180deg, rgba(255,255,255,.045), transparent 28%)," +
          "radial-gradient(100% 60% at 50% 122%, rgba(255,255,255,.05), transparent 60%)," +
          "linear-gradient(90deg, rgba(255,255,255,.013) 1px, transparent 1px)",
        backgroundSize: "100% 100%, 100% 100%, 3px 100%",
        backgroundPosition: "", backgroundRepeat: ""
      };
    },
    topo: function (c) {
      return {
        backgroundColor: "#07090c",
        backgroundImage:
          "repeating-radial-gradient(circle at 28% 32%, " + rgba(c, .06) + " 0 1px, transparent 1px 19px)," +
          "radial-gradient(120% 90% at 50% 0%, " + rgba(c, .10) + ", transparent 55%)",
        backgroundSize: "", backgroundPosition: "", backgroundRepeat: ""
      };
    }
  };
  const ORDER = ["none", "mesh", "glow", "grid", "aurora", "dots", "graphite", "topo"];
  const LABELS = {
    none: "None (layout default)", mesh: "Indigo mesh", glow: "Accent glow", grid: "Carbon grid",
    aurora: "Aurora", dots: "Dot matrix", graphite: "Graphite", topo: "Topographic"
  };

  ready(function () {
    if (typeof S === "undefined" || typeof render !== "function") {
      console.warn("[backgrounds] agent.html globals not found — load this AFTER the page script.");
      return;
    }

    let current = "none";

    function accentRgb() {
      return ACCENT_RGB[(S && S.accent) || "indigo"] || ACCENT_RGB.indigo;
    }
    function applyBg() {
      const cv = document.getElementById("creative");
      if (!cv) return;
      const def = (PRESETS[current] || PRESETS.none)(accentRgb());
      cv.style.backgroundColor = def.backgroundColor || "";
      cv.style.backgroundImage = def.backgroundImage || "";
      cv.style.backgroundSize = def.backgroundSize || "";
      cv.style.backgroundPosition = def.backgroundPosition || "";
      cv.style.backgroundRepeat = def.backgroundRepeat || "";
    }

    /* ---- UI: a Background select in the sidebar ---- */
    const block = el(
      '<div class="grp" id="bg-grp"><span class="lbl">Background</span>' +
      '<div class="field" style="margin:0"><select id="bg-select"></select></div>' +
      '<div class="ai-note" style="margin-top:7px">Tints to your accent. Auto-fill can pick one for you.</div></div>'
    );
    const aside = document.querySelector("aside.controls") || document.querySelector("aside") || document.querySelector(".controls");
    // place just after the Accent group if we can find it, else near the top
    let placed = false;
    if (aside) {
      const groups = aside.querySelectorAll(".grp");
      for (const g of groups) {
        const lbl = g.querySelector(".lbl");
        if (lbl && /accent/i.test(lbl.textContent || "")) {
          g.insertAdjacentElement("afterend", block);
          placed = true;
          break;
        }
      }
      if (!placed) {
        const af = document.getElementById("af-grp");
        if (af) { af.insertAdjacentElement("afterend", block); placed = true; }
      }
      if (!placed) aside.insertBefore(block, aside.firstElementChild);
    } else {
      document.body.appendChild(block);
    }

    const sel = document.getElementById("bg-select");
    sel.innerHTML = ORDER.map(k => '<option value="' + k + '">' + LABELS[k] + "</option>").join("");
    sel.value = current;
    sel.addEventListener("change", function () { current = sel.value; applyBg(); });

    /* ---- keep the bg applied across re-renders (accent/layout changes) ---- */
    if (typeof render === "function") {
      const _orig = render;
      render = function () {
        const r = _orig.apply(this, arguments);
        try { applyBg(); } catch (e) {}
        return r;
      };
      window.render = render;
    }

    /* ---- public hook for Auto-fill's AI suggestion ---- */
    window.korvusBg = {
      apply: function (key) {
        if (key && PRESETS[key]) {
          current = key;
          if (sel) sel.value = key;
          applyBg();
        }
      },
      get current() { return current; },
      keys: ORDER.slice()
    };

    applyBg(); // initial
  });
})();

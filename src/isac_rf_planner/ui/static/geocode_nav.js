/**
 * Shared address lookup UI → GET /api/geocode → navigate / set TX.
 * Used by 2D (app.js) and 3D (planner_3d.js).
 */
(function (global) {
  "use strict";

  async function fetchGeocode(query, limit) {
    const url = `/api/geocode?q=${encodeURIComponent(query)}&limit=${encodeURIComponent(String(limit || 5))}`;
    const r = await fetch(url);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = body.detail != null
        ? (typeof body.detail === "object" ? JSON.stringify(body.detail) : String(body.detail))
        : r.statusText;
      throw new Error(detail || `HTTP ${r.status}`);
    }
    if (body.status === "not_found" || !body.results || !body.results.length) {
      throw new Error(`No results for "${query}"`);
    }
    return body;
  }

  function bindAddressLookup(opts) {
    const input = document.getElementById(opts.inputId || "address-lookup-input");
    const goBtn = document.getElementById(opts.goButtonId || "address-lookup-go");
    const modeEl = document.getElementById(opts.modeSelectId || "address-lookup-mode");
    const resultsEl = document.getElementById(opts.resultsId || "address-lookup-results");
    if (!input || !goBtn) return;

    const setStatus = typeof opts.setStatus === "function" ? opts.setStatus : () => {};
    const onNavigate = typeof opts.onNavigate === "function" ? opts.onNavigate : () => {};

    let pendingResults = [];

    function renderResults(results) {
      if (!resultsEl) return;
      pendingResults = results || [];
      if (!pendingResults.length) {
        resultsEl.innerHTML = "";
        resultsEl.hidden = true;
        return;
      }
      if (pendingResults.length === 1) {
        resultsEl.innerHTML = "";
        resultsEl.hidden = true;
        return;
      }
      resultsEl.hidden = false;
      resultsEl.innerHTML = "";
      const label = document.createElement("div");
      label.className = "rf-sidebar-muted-sm";
      label.textContent = `${pendingResults.length} matches — pick one:`;
      resultsEl.appendChild(label);
      const list = document.createElement("div");
      list.className = "rf-address-results-list";
      pendingResults.forEach((res, idx) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "rf-address-result-btn";
        btn.textContent = res.formatted_address || `${res.lat}, ${res.lon}`;
        btn.addEventListener("click", () => applyResult(res));
        list.appendChild(btn);
      });
      resultsEl.appendChild(list);
    }

    function applyResult(result) {
      const mode = String(modeEl?.value || "navigate");
      onNavigate(Number(result.lat), Number(result.lon), result, mode);
      renderResults([]);
    }

    async function runLookup() {
      const q = String(input.value || "").trim();
      if (!q) {
        setStatus("Enter an address or lat,lon to go there.");
        input.focus();
        return;
      }
      goBtn.disabled = true;
      setStatus("Looking up address…");
      try {
        const body = await fetchGeocode(q, 5);
        renderResults(body.results);
        if (body.results.length === 1) {
          applyResult(body.results[0]);
          setStatus(`Go: ${body.results[0].formatted_address || q} (${body.provider})`);
        } else {
          setStatus(`Pick a match (${body.provider}).`);
        }
      } catch (err) {
        renderResults([]);
        setStatus(`Address lookup failed: ${err.message || err}`);
      } finally {
        goBtn.disabled = false;
      }
    }

    goBtn.addEventListener("click", () => runLookup());
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        runLookup();
      }
    });
  }

  global.RFAddressLookup = { bindAddressLookup, fetchGeocode };
})(typeof window !== "undefined" ? window : globalThis);

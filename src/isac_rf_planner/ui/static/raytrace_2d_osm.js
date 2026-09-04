(() => {
  const ui = {
    placeTxBtn: document.getElementById('placeTxBtn'),
    steerBtn: document.getElementById('steerBtn'),
    autoAimBtn: document.getElementById('autoAimBtn'),
    clearBtn: document.getElementById('clearBtn'),
    launchBtn: document.getElementById('launchBtn'),
    refreshBtn: document.getElementById('refreshBtn'),
    rayCount: document.getElementById('rayCount'),
    rayCountVal: document.getElementById('rayCountVal'),
    spreadDeg: document.getElementById('spreadDeg'),
    spreadDegVal: document.getElementById('spreadDegVal'),
    maxBounces: document.getElementById('maxBounces'),
    maxBouncesVal: document.getElementById('maxBouncesVal'),
    maxRange: document.getElementById('maxRange'),
    maxRangeVal: document.getElementById('maxRangeVal'),
    rxRadius: document.getElementById('rxRadius'),
    rxRadiusVal: document.getElementById('rxRadiusVal'),
    fetchPad: document.getElementById('fetchPad'),
    fetchPadVal: document.getElementById('fetchPadVal'),
    statsBox: document.getElementById('statsBox'),
    sceneBox: document.getElementById('sceneBox'),
    angleStat: document.getElementById('angleStat'),
    overlayCanvas: document.getElementById('overlayCanvas'),
    mapContainer: document.getElementById('mapContainer'),
  };

  const state = {
    mode: 'tx',
    map: null,
    ctx: ui.overlayCanvas.getContext('2d'),
    tx: null,
    rx: null,
    txUsed: null,
    rxUsed: null,
    beamAngleRad: 0,
    buildings: [],
    traces: [],
    summary: null,
    fetching: false,
    lastFetchError: null,
    fetchController: null,
  };

  function setMode(mode) {
    state.mode = mode;
    ui.placeTxBtn.classList.toggle('active', mode === 'tx');
    ui.steerBtn.classList.toggle('active', mode === 'steer');
  }

  function syncValueLabels() {
    ui.rayCountVal.textContent = ui.rayCount.value;
    ui.spreadDegVal.textContent = ui.spreadDeg.value;
    ui.maxBouncesVal.textContent = ui.maxBounces.value;
    ui.maxRangeVal.textContent = ui.maxRange.value;
    ui.rxRadiusVal.textContent = ui.rxRadius.value;
    ui.fetchPadVal.textContent = ui.fetchPad.value;
  }

  function deg(rad) {
    return (rad * 180 / Math.PI + 360) % 360;
  }

  function updateAngleStat() {
    ui.angleStat.innerHTML = `Steer angle: <code>${deg(state.beamAngleRad).toFixed(1)}°</code>`;
  }

  function haversineMeters(a, b) {
    const r = 6371000;
    const p1 = a.lat * Math.PI / 180;
    const p2 = b.lat * Math.PI / 180;
    const dLat = (b.lat - a.lat) * Math.PI / 180;
    const dLon = (b.lng - a.lng) * Math.PI / 180;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dLon / 2) ** 2;
    return 2 * r * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
  }

  function updateSceneBox(extra = '') {
    const count = state.buildings.length;
    const txLabel = state.tx ? `${state.tx.lat.toFixed(6)}, ${state.tx.lng.toFixed(6)}` : 'unset';
    const rxLabel = state.rx ? `${state.rx.lat.toFixed(6)}, ${state.rx.lng.toFixed(6)}` : 'unset';
    const parts = [
      `Buildings loaded: <code>${count}</code>`,
      `Tx: <code>${txLabel}</code>`,
      `Rx: <code>${rxLabel}</code>`
    ];
    if (extra) parts.push(extra);
    if (state.lastFetchError) parts.push(`<span style="color:#ffb86b">${state.lastFetchError}</span>`);
    ui.sceneBox.innerHTML = parts.join('<br/>');
  }

  function updateStatsBox() {
    if (!state.summary) {
      ui.statsBox.innerHTML = 'No trace yet.';
      return;
    }
    const s = state.summary;
    ui.statsBox.innerHTML = [
      `Launched: <code>${s.num_launched}</code>`,
      `Hit Rx: <code>${s.num_hit_rx}</code>`,
      `Hit bounce range: <code>${String(s.min_bounces_among_hits)}</code> to <code>${String(s.max_bounces_among_hits)}</code>`,
      `Corner stops: <code>${s.num_corner_hit}</code>, max-bounce stops: <code>${s.num_max_bounces}</code>, max-range stops: <code>${s.num_max_path}</code>`,
      `Buildings used for trace: <code>${state.traceBuildingsUsed ?? 0}</code>`
    ].join('<br/>');
  }

  function resizeCanvas() {
    const rect = ui.mapContainer.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    ui.overlayCanvas.width = Math.round(rect.width * dpr);
    ui.overlayCanvas.height = Math.round(rect.height * dpr);
    ui.overlayCanvas.style.width = `${rect.width}px`;
    ui.overlayCanvas.style.height = `${rect.height}px`;
    state.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function project(lat, lon) {
    const pt = state.map.latLngToContainerPoint([lat, lon]);
    return { x: pt.x, y: pt.y };
  }

  function drawBuildings() {
    const ctx = state.ctx;
    ctx.save();
    ctx.fillStyle = 'rgba(180, 180, 180, 0.42)';
    ctx.strokeStyle = 'rgba(120, 120, 120, 0.95)';
    ctx.lineWidth = 1.0;
    for (const building of state.buildings) {
      const geom = Array.isArray(building.geometry) ? building.geometry : [];
      if (geom.length < 3) continue;
      let first = true;
      ctx.beginPath();
      for (const node of geom) {
        const pt = project(Number(node.lat), Number(node.lon));
        if (first) {
          ctx.moveTo(pt.x, pt.y);
          first = false;
        } else {
          ctx.lineTo(pt.x, pt.y);
        }
      }
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawBeamGuide() {
    if (!state.tx) return;
    const ctx = state.ctx;
    const txPt = project(state.tx.lat, state.tx.lng);
    const spread = Number(ui.spreadDeg.value) * Math.PI / 180;
    const guideRadiusPx = 110;
    ctx.save();
    ctx.strokeStyle = 'rgba(126,203,255,0.45)';
    ctx.fillStyle = 'rgba(126,203,255,0.10)';
    ctx.lineWidth = 1.0;
    ctx.beginPath();
    ctx.moveTo(txPt.x, txPt.y);
    ctx.arc(txPt.x, txPt.y, guideRadiusPx, state.beamAngleRad - spread / 2, state.beamAngleRad + spread / 2);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  function drawTraces() {
    const ctx = state.ctx;
    for (const trace of state.traces) {
      const pts = Array.isArray(trace.points) ? trace.points : [];
      if (pts.length < 2) continue;
      ctx.save();
      ctx.strokeStyle = trace.hit_rx ? '#2bb24c' : '#ff8c42';
      ctx.lineWidth = trace.hit_rx ? 2.2 : 1.2;
      ctx.globalAlpha = trace.hit_rx ? 0.85 : 0.42;
      ctx.beginPath();
      pts.forEach((p, idx) => {
        const pt = project(Number(p.lat), Number(p.lon));
        if (idx === 0) ctx.moveTo(pt.x, pt.y);
        else ctx.lineTo(pt.x, pt.y);
      });
      ctx.stroke();
      ctx.restore();
    }
  }

  function drawMarkers() {
    const ctx = state.ctx;
    const txToDraw = state.txUsed || state.tx;
    const rxToDraw = state.rxUsed || state.rx;
    if (rxToDraw) {
      const pt = project(rxToDraw.lat, rxToDraw.lng);
      const meters = Number(ui.rxRadius.value);
      const edgeLatLng = state.map.layerPointToLatLng(L.point(state.map.latLngToLayerPoint([rxToDraw.lat, rxToDraw.lng]).x + 12, state.map.latLngToLayerPoint([rxToDraw.lat, rxToDraw.lng]).y));
      let pxRadius = 12;
      if (edgeLatLng) {
        const metersPer12px = haversineMeters({ lat: rxToDraw.lat, lng: rxToDraw.lng }, { lat: edgeLatLng.lat, lng: edgeLatLng.lng });
        if (metersPer12px > 0) pxRadius = meters / (metersPer12px / 12);
      }
      ctx.save();
      ctx.strokeStyle = '#1565c0';
      ctx.fillStyle = 'rgba(21,101,192,0.12)';
      ctx.lineWidth = 2.0;
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, Math.max(4, pxRadius), 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }
    if (txToDraw) {
      const pt = project(txToDraw.lat, txToDraw.lng);
      ctx.save();
      ctx.fillStyle = '#d32f2f';
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }
  }

  function draw() {
    const ctx = state.ctx;
    const rect = ui.mapContainer.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    drawBuildings();
    drawBeamGuide();
    drawTraces();
    drawMarkers();
  }

  function setBeamToward(latlng) {
    if (!state.tx || !latlng) return;
    const a = state.map.latLngToContainerPoint(state.tx);
    const b = state.map.latLngToContainerPoint(latlng);
    state.beamAngleRad = Math.atan2(b.y - a.y, b.x - a.x);
    updateAngleStat();
    draw();
  }

  function centroidDistanceKey(tx, building) {
    const geom = Array.isArray(building.geometry) ? building.geometry : [];
    if (!geom.length) return 0;
    let lat = 0;
    let lon = 0;
    for (const node of geom) {
      lat += Number(node.lat);
      lon += Number(node.lon);
    }
    const c = { lat: lat / geom.length, lng: lon / geom.length };
    return haversineMeters(tx, c);
  }

  function filterBuildingsForLaunch() {
    if (!state.tx) return [];
    const maxRange = Number(ui.maxRange.value) + 100;
    const subset = state.buildings.filter((building) => {
      const geom = Array.isArray(building.geometry) ? building.geometry : [];
      if (!geom.length) return false;
      for (const node of geom) {
        if (haversineMeters(state.tx, { lat: Number(node.lat), lng: Number(node.lon) }) <= maxRange) {
          return true;
        }
      }
      return centroidDistanceKey(state.tx, building) <= maxRange;
    });
    subset.sort((a, b) => centroidDistanceKey(state.tx, a) - centroidDistanceKey(state.tx, b));
    return subset.slice(0, 2200);
  }

  async function fetchBuildings() {
    if (!state.map) return;
    if (state.fetchController) state.fetchController.abort();
    state.fetchController = new AbortController();
    const bounds = state.map.getBounds();
    const qs = new URLSearchParams({
      min_lat: String(bounds.getSouth()),
      min_lon: String(bounds.getWest()),
      max_lat: String(bounds.getNorth()),
      max_lon: String(bounds.getEast()),
      pad_m: String(Number(ui.fetchPad.value)),
      max_features: '2500',
    });
    state.fetching = true;
    state.lastFetchError = null;
    updateSceneBox('Loading OSM buildings…');
    try {
      const resp = await fetch(`/api/osm/buildings-bbox?${qs.toString()}`, {
        signal: state.fetchController.signal,
        cache: 'no-store',
      });
      const payload = await resp.json();
      if (!resp.ok || payload.status !== 'ok') {
        throw new Error(payload.detail || payload.error || `HTTP ${resp.status}`);
      }
      state.buildings = Array.isArray(payload.buildings) ? payload.buildings : [];
      updateSceneBox(`Viewport buildings: <code>${state.buildings.length}</code>`);
      draw();
    } catch (err) {
      if (err.name === 'AbortError') return;
      state.lastFetchError = `OSM fetch failed: ${err.message}`;
      updateSceneBox();
    } finally {
      state.fetching = false;
    }
  }

  async function launchRays() {
    if (!state.tx || !state.rx) {
      ui.statsBox.innerHTML = 'Set both Tx and Rx first.';
      return;
    }
    const buildings = filterBuildingsForLaunch();
    const payload = {
      tx: { lat: state.tx.lat, lon: state.tx.lng },
      rx: { lat: state.rx.lat, lon: state.rx.lng },
      buildings,
      num_rays: Number(ui.rayCount.value),
      spread_deg: Number(ui.spreadDeg.value),
      max_bounces: Number(ui.maxBounces.value),
      max_range_m: Number(ui.maxRange.value),
      rx_capture_radius_m: Number(ui.rxRadius.value),
      bearing_deg: deg(state.beamAngleRad),
    };
    ui.launchBtn.disabled = true;
    ui.statsBox.innerHTML = 'Launching…';
    try {
      const resp = await fetch('/api/raytrace_2d_forward_osm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok || data.status !== 'ok') {
        throw new Error(data.detail || data.error || `HTTP ${resp.status}`);
      }
      state.traces = Array.isArray(data.traces) ? data.traces : [];
      state.summary = data.summary || null;
      state.traceBuildingsUsed = Number(data.buildings_used || buildings.length || 0);
      if (data.tx_used) state.txUsed = { lat: Number(data.tx_used.lat), lng: Number(data.tx_used.lon) };
      if (data.rx_used) state.rxUsed = { lat: Number(data.rx_used.lat), lng: Number(data.rx_used.lon) };
      updateStatsBox();
      draw();
    } catch (err) {
      state.summary = null;
      state.traces = [];
      ui.statsBox.innerHTML = `Launch failed: ${err.message}`;
      draw();
    } finally {
      ui.launchBtn.disabled = false;
    }
  }

  function clearTraces() {
    state.traces = [];
    state.summary = null;
    state.txUsed = null;
    state.rxUsed = null;
    state.traceBuildingsUsed = 0;
    updateStatsBox();
    draw();
  }

  function initMap() {
    const map = L.map('map', { zoomControl: true, attributionControl: true }).setView([37.7749, -122.4194], 15);
    state.map = map;
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 20,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);
    L.control.scale({ metric: true, imperial: false, maxWidth: 160 }).addTo(map);

    map.on('click', (e) => {
      if (e.originalEvent && e.originalEvent.shiftKey) {
        state.rx = { lat: e.latlng.lat, lng: e.latlng.lng };
        state.rxUsed = null;
        clearTraces();
        updateSceneBox();
        draw();
        return;
      }
      if (state.mode === 'steer' && state.tx) {
        setBeamToward(e.latlng);
        return;
      }
      state.tx = { lat: e.latlng.lat, lng: e.latlng.lng };
      state.txUsed = null;
      clearTraces();
      if (!state.rx) {
        const rxGuess = L.latLng(e.latlng.lat, e.latlng.lng + 0.0012);
        state.rx = { lat: rxGuess.lat, lng: rxGuess.lng };
      }
      updateSceneBox();
      draw();
    });

    map.on('mousemove', (e) => {
      if (state.mode === 'steer' && state.tx) {
        setBeamToward(e.latlng);
      }
    });

    map.on('moveend', fetchBuildings);
    map.on('zoomend', () => {
      resizeCanvas();
      draw();
    });
    window.addEventListener('resize', resizeCanvas);
    resizeCanvas();
    fetchBuildings();
  }

  ui.placeTxBtn.addEventListener('click', () => setMode('tx'));
  ui.steerBtn.addEventListener('click', () => setMode('steer'));
  ui.autoAimBtn.addEventListener('click', () => {
    if (!state.tx || !state.rx) return;
    setBeamToward(state.rx);
  });
  ui.clearBtn.addEventListener('click', clearTraces);
  ui.launchBtn.addEventListener('click', launchRays);
  ui.refreshBtn.addEventListener('click', fetchBuildings);
  [ui.rayCount, ui.spreadDeg, ui.maxBounces, ui.maxRange, ui.rxRadius, ui.fetchPad].forEach((el) => {
    el.addEventListener('input', () => {
      syncValueLabels();
      if (el === ui.fetchPad) fetchBuildings();
      draw();
    });
  });

  syncValueLabels();
  setMode('tx');
  updateAngleStat();
  updateSceneBox();
  initMap();
})();

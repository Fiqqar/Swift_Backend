(function () {
  "use strict";

  var map = L.map('map').setView([-6.8048, 110.8385], 13);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var routePane = map.createPane('route');
  routePane.style.zIndex = 460;

  function onViewportResize() { map.invalidateSize(); }
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', onViewportResize);
    window.visualViewport.addEventListener('scroll', onViewportResize);
  } else {
    window.addEventListener('resize', onViewportResize);
  }

  var sheetEl = document.getElementById('route-sheet');
  if (sheetEl) {
    L.DomEvent.disableClickPropagation(sheetEl);
    L.DomEvent.disableScrollPropagation(sheetEl);
  }

  function $(id) { return document.getElementById(id); }
  function log(msg) {
    $('log').textContent = (new Date().toLocaleTimeString()) + " " + msg;
  }

  var loadingTimer = null;
  function showLoading(msg) {
    if (loadingTimer) clearInterval(loadingTimer);
    $('loading-text').textContent = msg || 'Menghubungi server...';
    var start = Date.now();
    $('loading-time').textContent = '0 dtk';
    $('loading').className = 'loading show';
    loadingTimer = setInterval(function () {
      var s = Math.floor((Date.now() - start) / 1000);
      $('loading-time').textContent = s + ' dtk';
    }, 1000);
  }
  function hideLoading() {
    if (loadingTimer) { clearInterval(loadingTimer); loadingTimer = null; }
    $('loading').className = 'loading';
  }

  function showNotice(msg) {
    if (msg) { $('notice').textContent = msg; $('notice').className = 'show'; }
    else hideNotice();
  }
  function hideNotice() { $('notice').textContent = ''; $('notice').className = ''; }

  function showToast(msg, type) {
    var container = $('toasts');
    if (!container) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (type || 'info');
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(function () {
      el.classList.add('fade-out');
      setTimeout(function () { el.remove(); }, 300);
    }, 4000);
  }

  function setBadge(id, label, state) {
    var el = $(id);
    el.textContent = label;
    el.className = 'badge ' + (state || '');
  }

  function haversineDistance(coord1, coord2) {
    var R = 6371000; // Earth radius in meters
    var lat1 = coord1[0] * Math.PI / 180;
    var lon1 = coord1[1] * Math.PI / 180;
    var lat2 = coord2[0] * Math.PI / 180;
    var lon2 = coord2[1] * Math.PI / 180;
    var dLat = lat2 - lat1;
    var dLon = lon2 - lon1;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1) * Math.cos(lat2) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    var c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    return R * c;
  }

  async function parseJson(resp) {
    var ct = (resp.headers.get('content-type') || '').toLowerCase();
    if (ct.indexOf('application/json') === -1) {
      var text = await resp.text();
      throw new Error('Server ' + resp.status + ' bukan JSON: ' + (text || '').slice(0, 300));
    }
    return resp.json();
  }

  function fmt(coord) {
    return coord ? coord[0].toFixed(5) + ', ' + coord[1].toFixed(5) : '-';
  }

  function formatEta(secs) {
    secs = Math.round(secs);
    if (secs >= 3600) {
      var h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
      return h + ' jam ' + m + ' mnt';
    }
    if (secs >= 60) {
      var m = Math.floor(secs / 60), s = secs % 60;
      return m + ' mnt ' + s + ' dtk';
    }
    return secs + ' dtk';
  }

  var hub = null, curPos = null, deviating = false;
  var hubMarker = null, posMarker = null;
  var stopMarkers = [];
  var legLines = [];
  var routeLayer = null;
  var state = null;   // { stops, legs }
  var activeIndex = 0;
  var navWs = null, navRouteId = null, navSimTimer = null, navSimIdx = 0;
  var navRouteCoords = null, navRouteLayer = null, navStatus = { enabled: false };
  var navData = null;
  var navConnecting = false;
  var auth = { token: localStorage.getItem('delivery_token') || '', kurir: null };
  var posWs = null, geoWatchId = null, webhookActive = false;

  // Test Mode: Off-Route Auto-Reroute
  var testRerouteMode = false;
  var testRerouteCircle = null;
  var testRerouteLastClick = 0;
  var testRerouteDebounceMs = 1500;
  var navSimPaused = false;

  var iconColors = {
    hub: '#0f2a43',
    regular: '#1e7e34',
    express: '#b3372f'
  };

  function numberedIcon(text, bg) {
    return L.divIcon({
      html: '<div style="background:' + bg + ';border:2px solid #fff;border-radius:50%;width:22px;height:22px;color:#fff;font-weight:700;font-size:11px;line-height:18px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.5);">' + text + '</div>',
      className: '', iconSize: [22, 22], iconAnchor: [11, 11]
    });
  }

  // --- Test Mode: Off-Route Auto-Reroute Helpers -------------------
  function enableTestRerouteMode() {
    if (!navWs || navWs.readyState !== WebSocket.OPEN) {
      showNotice('Navigasi WS belum terhubung. Klik "Mulai Navigasi" dulu.');
      $('inp-mode-hub').checked = true; // Reset to hub
      return false;
    }
    testRerouteMode = true;
    navSimPaused = false;
    if (navSimTimer) {
      clearInterval(navSimTimer);
      navSimTimer = null;
      navSimPaused = true;
    }
    $('reroute-test-status').style.display = 'block';
    // Add circle overlay at current position
    if (curPos) addTestRerouteCircle(curPos[0], curPos[1]);
    // Enable test mode radio
    $('inp-mode-reroute_test').disabled = false;
    log('Test mode aktif: klik peta untuk trigger off-route auto-reroute');
    return true;
  }

  function disableTestRerouteMode() {
    testRerouteMode = false;
    if (testRerouteCircle) {
      map.removeLayer(testRerouteCircle);
      testRerouteCircle = null;
    }
    $('reroute-test-status').style.display = 'none';
    $('inp-mode-reroute_test').disabled = true;
    // Resume simulation from last position
    if (navSimPaused && navRouteCoords && navSimIdx < navRouteCoords.length) {
      navSimulate();
      navSimTimer = setInterval(navSimulate, 4000);
      navSimPaused = false;
    }
    log('Test mode nonaktif: simulasi dilanjutkan');
  }

  function addTestRerouteCircle(lat, lon) {
    if (testRerouteCircle) map.removeLayer(testRerouteCircle);
    var radius = 50; // meters
    testRerouteCircle = L.circle([lat, lon], {
      radius: radius,
      color: '#ff6b35',
      fillColor: '#ff6b35',
      fillOpacity: 0.15,
      weight: 2,
      dashArray: '6 4',
      className: 'reroute-circle'
    }).addTo(map);
  }

  function updateTestRerouteCircle(lat, lon) {
    if (testRerouteCircle) {
      testRerouteCircle.setLatLng([lat, lon]);
    }
  }

  function removeTestRerouteCircle() {
    if (testRerouteCircle) {
      map.removeLayer(testRerouteCircle);
      testRerouteCircle = null;
    }
  }

  function sendTestReroutePosition(lat, lon) {
    if (!navWs || navWs.readyState !== WebSocket.OPEN) return;
    var now = Date.now();
    if (now - testRerouteLastClick < testRerouteDebounceMs) return;
    testRerouteLastClick = now;

    // Update visual marker (orange for test mode)
    if (posMarker) map.removeLayer(posMarker);
    posMarker = L.marker([lat, lon], {
      icon: L.divIcon({
        html: '<div style="background:#ff6b35;border:2px solid #fff;border-radius:50%;width:16px;height:16px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>',
        className: '', iconSize: [16, 16], iconAnchor: [8, 8]
      })
    }).addTo(map).bindTooltip('Test: Off-Route Position').openTooltip();

    // Update circle overlay
    updateTestRerouteCircle(lat, lon);

    // Send location_update to navigation WS
    navWs.send(JSON.stringify({
      type: 'location_update',
      lat: lat, lng: lon,
      current_route_id: navRouteId,
      test_mode: true
    }));
    log('Test off-route: kirim position (' + lat.toFixed(5) + ', ' + lon.toFixed(5) + ') ke nav WS');
  }

  function placeHub(latlng) {
    hub = [latlng.lat, latlng.lng];
    if (hubMarker) map.removeLayer(hubMarker);
    hubMarker = L.marker(latlng, {
      icon: numberedIcon('H', iconColors.hub)
    }).addTo(map).bindTooltip('Hub').openTooltip();
    // Jangan buat posMarker di hub - posMarker hanya untuk posisi kurir real (dari geolokasi/WS)
    $('txt-hub').textContent = fmt(hub);
    $('txt-pos').textContent = fmt(curPos);
    updateOptimizeBtn();
  }

  function canOptimize() {
    return (hub || curPos) && parseDeliveries().length > 0;
  }

  function updateOptimizeBtn() {
    $('btn-optimize').disabled = !canOptimize();
  }

  function authHeaders() {
    return auth.token ? { 'Authorization': 'Bearer ' + auth.token } : {};
  }

  function renderAuthUI() {
    var logged = !!auth.token;
    $('btn-login').style.display = logged ? 'none' : '';
    $('btn-logout').style.display = logged ? '' : 'none';
    var who = (auth.kurir && auth.kurir.nama) ||
      (auth.kurir && auth.kurir.id != null ? 'kurir #' + auth.kurir.id : '');
    $('login-status').textContent = logged
      ? ('Login OK: ' + (who || 'kurir') +
         '. Webhook-first aktif bila posisi di-stream ke WS.')
      : 'Belum login. Titik awal fallback: courier_position / hub.';
  }

  async function doLogin() {
    var u = $('inp-username').value.trim();
    var p = $('inp-password').value;
    if (!u || !p) { log('Login: username & password wajib diisi.'); return; }
    $('btn-login').disabled = true;
    log('Login kurir...');
    try {
      var resp = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: u, password: p }),
        signal: AbortSignal.timeout(15000)
      });
      var data = await parseJson(resp);
      if (!resp.ok || !data.data || !data.data.token) {
        throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
      }
      auth.token = data.data.token;
      auth.kurir = data.data.kurir || null;
      localStorage.setItem('delivery_token', auth.token);
      renderAuthUI();
      log('Login OK: ' + ((auth.kurir && auth.kurir.nama) || 'kurir') + '.');
    } catch (err) {
      log('Login gagal: ' + ((err && err.message) ? err.message : String(err)));
      showNotice('Login gagal: ' + ((err && err.message) ? err.message : String(err)));
    } finally {
      $('btn-login').disabled = false;
    }
  }

  function doLogout() {
    auth.token = ''; auth.kurir = null;
    localStorage.removeItem('delivery_token');
    stopPositionStream();
    renderAuthUI();
    log('Logout. Titik awal fallback: courier_position / hub.');
  }

  async function verifyToken() {
    if (!auth.token) return;
    try {
      var resp = await fetch('/api/v1/auth/me', { headers: authHeaders() });
      var data = await parseJson(resp);
      if (resp.ok && data.data) {
        auth.kurir = data.data;
      } else {
        auth.token = ''; auth.kurir = null;
        localStorage.removeItem('delivery_token');
      }
    } catch (e) { /* token disimpan, tunggu login ulang bila perlu */ }
    renderAuthUI();
  }

  function sendPositionToWs(lat, lon) {
    if (posWs && posWs.readyState === WebSocket.OPEN) {
      posWs.send(JSON.stringify({ type: 'position', lat: lat, lon: lon }));
    }
  }

  function sendPositionNow() {
    if (!curPos) {
      showNotice('Belum ada posisi kurir. Klik "Gunakan Lokasi Saya" atau klik peta (mode deviasi).');
      return;
    }
    if (posWs && posWs.readyState === WebSocket.OPEN) {
      posWs.send(JSON.stringify({ type: 'position', lat: curPos[0], lon: curPos[1] }));
      log('Posisi dikirim manual ke WS: ' + fmt(curPos));
    } else {
      showNotice('WS posisi tidak terhubung. Aktifkan streaming dulu atau login.');
    }
  }

  function updateSendPosBtn() {
    var btn = $('btn-send-pos');
    if (btn) {
      btn.disabled = !(curPos && posWs && posWs.readyState === WebSocket.OPEN);
    }
  }

  function setCourierPosition(lat, lon, opts) {
    var dev = !!(opts && opts.deviate);
    var size = dev ? 16 : 14;
    curPos = [lat, lon];
    if (posMarker) map.removeLayer(posMarker);
    posMarker = L.marker(L.latLng(lat, lon), {
      icon: L.divIcon({
        html: '<div style="background:' + (dev ? '#b3372f' : '#0f6dc1') +
          ';border:2px solid #fff;border-radius:50%;width:' + size + 'px;height:' + size +
          'px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>',
        className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2]
      })
    }).addTo(map)
      .bindTooltip(dev ? 'Posisi kurir (deviasi)' : 'Posisi kurir')
      .openTooltip();
    $('txt-pos').textContent = fmt(curPos);
    sendPositionToWs(lat, lon);
    updateOptimizeBtn();
    updateSendPosBtn();
  }

  function stopPositionStream() {
    if (posWs) { try { posWs.close(); } catch (e) {} posWs = null; }
    if (geoWatchId != null) {
      navigator.geolocation.clearWatch(geoWatchId);
      geoWatchId = null;
    }
    webhookActive = false;
    $('chk-stream').checked = false;
    $('ws-status').textContent = 'WS posisi: nonaktif';
    $('ws-status').style.color = '';
    updateSendPosBtn();
  }

  function startPositionStream() {
    if (!auth.token) {
      showNotice('Login dulu untuk stream posisi (webhook-first).');
      $('chk-stream').checked = false;
      return;
    }
    if (!navigator.geolocation) {
      showNotice('Geolocation tidak didukung browser.');
      $('chk-stream').checked = false;
      return;
    }
    stopPositionStream();
    $('chk-stream').checked = true;
    $('ws-status').textContent = 'WS posisi: menghubungkan...';
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    posWs = new WebSocket(proto + location.host +
      '/api/v1/ws/driver/position?token=' + encodeURIComponent(auth.token));
    posWs.onopen = function () {
      $('ws-status').textContent = 'WS posisi: terhubung (posisi dari geolokasi dikirim)';
      updateSendPosBtn();
    };
    posWs.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.type === 'ack' && msg.ok) {
        webhookActive = !!msg.stored;
        $('ws-status').textContent = msg.stored
          ? 'WS posisi: tersimpan di Redis (webhook-first)'
          : 'WS posisi: terhubung tapi Redis tidak menyimpan' +
            (msg.warning ? ' (' + msg.warning + ')' : '');
        $('ws-status').style.color = msg.stored ? '#1e7e34' : '#b3372f';
      }
      if (msg.type === 'error') {
        $('ws-status').textContent = 'WS posisi: error (' + (msg.detail || '') + ')';
        $('ws-status').style.color = '#b3372f';
      }
    };
    posWs.onclose = function () {
      webhookActive = false;
      if ($('chk-stream').checked) {
        $('ws-status').textContent = 'WS posisi: terputus (ditutup server / token invalid)';
        $('ws-status').style.color = '#b3372f';
      }
    };
    posWs.onerror = function () {
      $('ws-status').textContent = 'WS posisi: koneksi gagal';
      $('ws-status').style.color = '#b3372f';
    };
    geoWatchId = navigator.geolocation.watchPosition(function (pos) {
      setCourierPosition(pos.coords.latitude, pos.coords.longitude);
    }, function (err) {
      log('ERROR geolokasi (' + err.code + '): ' + err.message);
    }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 });
  }

  function vehicleMode() {
    var el = document.querySelector('input[name="vehicle"]:checked');
    return el ? el.value : 'motorcycle';
  }

  function parseCoords(s) {
    var m = s.match(/^(-?[\d.]+)\s*,\s*(-?[\d.]+)$/);
    if (!m) return null;
    return { latitude: parseFloat(m[1]), longitude: parseFloat(m[2]) };
  }

  function parseDeliveries() {
    var raw = $('txt-deliveries').value.trim();
    if (!raw) return [];
    var out = [];
    raw.split('\n').forEach(function (line) {
      line = line.trim();
      if (!line) return;
      var parts = line.split('|').map(function (s) { return s.trim(); });
      var alamat = parts[0];
      var svc = 'REGULAR';
      if (parts.length > 1 && parts[1]) {
        svc = parts[1].toUpperCase().indexOf('EXPRESS') >= 0 ? 'EXPRESS' : 'REGULAR';
      }
      if (!alamat) return;
      var coords = null;
      if (parts.length > 2 && parts[2]) {
        coords = parseCoords(parts[2]);
      } else {
        coords = parseCoords(alamat);
        if (coords) alamat = alamat + ' (koordinat)';
      }
      if (!coords) {
        out.push({ alamat: alamat, service_type: svc });
      } else {
        out.push({
          alamat: alamat,
          service_type: svc,
          latitude: coords.latitude,
          longitude: coords.longitude
        });
      }
    });
    return out;
  }

  function clearMapLayers() {
    stopMarkers.forEach(function (m) { map.removeLayer(m); });
    stopMarkers = [];
    legLines.forEach(function (l) { map.removeLayer(l); });
    legLines = [];
    if (routeLayer) { map.removeLayer(routeLayer); routeLayer = null; }
    $('route-sheet').className = 'route-sheet';
    $('sheet-list').innerHTML = '';
  }

  // Clear only navigation-related polylines (keep markers and route sheet)
  function clearOldPolylines() {
    legLines.forEach(function (l) { map.removeLayer(l); });
    legLines = [];
    if (navRouteLayer) { map.removeLayer(navRouteLayer); navRouteLayer = null; }
    if (routeLayer) { map.removeLayer(routeLayer); routeLayer = null; }
  }

  // Draw reordered route with proper active/remaining leg styling.
  // SINGLE executor for map rendering on reorder — never calls clearMapLayers/drawOverview.
  function drawReorderedRoute(data) {
    clearOldPolylines();

    var legs = data.legs || [];
    var stops = data.stops || [];
    // Dynamic active leg index from payload, fallback to stored state, then 0.
    var activeLegIndex = data.active_leg_index ?? (state && state.activeLegIndex) ?? 0;

    var group = L.layerGroup().addTo(map);
    var allBounds = null;

    // Draw ALL legs unconditionally — never nested inside a conditional,
    // otherwise route disappears when current_position is null.
    legs.forEach(function (leg, i) {
      var coords = decodePolyline(leg.geometry || '');
      if (!coords.length) return;
      var isActive = (i === activeLegIndex);   // Red dashed
      var isDone = (i < activeLegIndex);       // Gray dashed
      // i > activeLegIndex → upcoming → Blue solid

      var poly = L.polyline(coords, {
        color: isActive ? '#b3372f' : (isDone ? '#8b9aab' : '#0f6dc1'),
        weight: isActive ? 6 : (isDone ? 3 : 5),
        opacity: isActive ? 0.95 : (isDone ? 0.6 : 0.9),
        dashArray: isActive ? '8 6' : (isDone ? '6 6' : null),
        pane: 'route'
      }).addTo(group);
      legLines.push(poly);
      if (!allBounds) allBounds = poly.getBounds();
      else allBounds.extend(poly.getBounds());
    });

    // Draw stop markers with updated stop_order numbering.
    stops.forEach(function (s) {
      var color = s.service_type === 'EXPRESS' ? iconColors.express : iconColors.regular;
      var m = L.marker([s.latitude, s.longitude], {
        icon: numberedIcon(String(s.stop_order), color)
      }).addTo(group)
        .bindTooltip((s.recipient_name || ('Paket ' + (s.package_id || s.stop_order))) +
          (s.service_type === 'EXPRESS' ? ' · EXPRESS' : ''));
      stopMarkers.push(m);
      if (!allBounds) allBounds = m.getBounds();
      else allBounds.extend(m.getBounds());
    });

    if (allBounds) map.fitBounds(allBounds, { padding: [40, 40] });

    // Atomic state sync BEFORE rendering UI list components.
    state.stops = stops;
    state.legs = legs;
    state.activeLegIndex = activeLegIndex;
    activeIndex = activeLegIndex; // Sync global used by renderRouteSheet/renderNav

    // Sync navigation simulation to the NEW active leg geometry so the
    // courier position simulator continues along the reordered route,
    // not the stale pre-reorder polyline.
    var activeLeg = legs[activeLegIndex];
    if (activeLeg && activeLeg.geometry) {
      navRouteCoords = decodePolyline(activeLeg.geometry);
      navSimIdx = 0;
    }

    renderRouteSheet({ stops: stops, legs: legs });
    renderNav({ stops: stops, legs: legs });
  }


// Original drawOverview function for backward compatibility.
// NOTE: uses & syncs GLOBAL activeIndex — do not shadow it locally, otherwise
// renderRouteSheet/renderNav (which close over the global) highlight wrongly.
  function drawOverview(data) {
    clearMapLayers();
    var group = L.layerGroup().addTo(map);
    var allBounds = null;

    // Sync global activeIndex so downstream UI renders the correct active stop.
    if (data.active_leg_index != null) {
      activeIndex = data.active_leg_index;
    }

    data.legs.forEach(function (leg, i) {
      var coords = decodePolyline(leg.geometry || '');
      if (!coords.length) return;
      var isActive = (i === activeIndex);
      var isDone = (i < activeIndex);
      var poly = L.polyline(coords, {
        color: isDone ? '#8b9aab' : (isActive ? '#0f6dc1' : '#b7c3ce'),
        weight: isDone ? 3 : (isActive ? 7 : 4),
        opacity: isDone ? 0.6 : (isActive ? 0.95 : 0.85),
        dashArray: isDone ? '6 6' : null,
        pane: 'route'
      }).addTo(group);
      legLines.push(poly);
      if (!allBounds) allBounds = poly.getBounds();
      else allBounds.extend(poly.getBounds());
    });

    data.stops.forEach(function (s, i) {
      var color = s.service_type === 'EXPRESS' ? iconColors.express : iconColors.regular;
      var m = L.marker([s.latitude, s.longitude], {
        icon: numberedIcon(String(s.stop_order), color)
      }).addTo(group)
        .bindTooltip((s.recipient_name || ('Paket ' + (s.package_id || s.stop_order))) +
          (s.service_type === 'EXPRESS' ? ' · EXPRESS' : ''));
      stopMarkers.push(m);
    });

    routeLayer = group;
    if (allBounds) map.fitBounds(allBounds, { padding: [40, 40] });
    renderRouteSheet(data);
    renderNav(data);
  }



function renderRouteSheet(data) {
    var listEl = $('sheet-list');
    var list = '';
    data.stops.forEach(function (s, i) {
      var leg = data.legs[i] || {};
      var badge = s.service_type === 'EXPRESS' ? ' <span class="incident-badge">EXPRESS</span>' : '';
      var cls = i === activeIndex ? ' selected' : (i < activeIndex ? ' done' : '');
      list += '<div class="sheet-card' + cls + '">'
        + '<span class="swatch ' + (i === activeIndex ? 'best' : 'alt') + '"></span>'
        + '<div class="card-main">'
        + '<div class="card-title">Stop ' + s.stop_order + ' — ' +
          (s.recipient_name || ('Paket ' + (s.package_id || s.stop_order))) + badge + '</div>'
        + '<div class="card-sub">' + (s.service_type || '') + '</div>'
        + '</div>'
        + '<div class="card-meta">'
        + (leg.distance_km != null ? leg.distance_km.toLocaleString('id-ID') + ' km' : '-')
        + '<span class="delta">' + (leg.duration_mins != null ? leg.duration_mins.toLocaleString('id-ID') + ' mnt' : '-') + '</span>'
        + '</div>'
        + '</div>';
    });
    listEl.innerHTML = list;
    $('route-sheet').className = 'route-sheet show';
  }

  function renderNav(data) {
    $('nav-controls').style.display = 'block';
    var card = $('nav-card');
    var leg = data.legs[activeIndex];
    if (activeIndex >= data.stops.length) {
      $('nav-status').textContent = 'Semua paket terkirim ✅';
      card.textContent = 'Rute selesai.';
      $('btn-deliver').disabled = true;
      $('btn-recalc').disabled = true;
      $('btn-nav-start').disabled = true;
      navInd('Navigasi: selesai');
      return;
    }
    var s = data.stops[activeIndex];
    $('nav-status').textContent = 'Stop ' + (activeIndex + 1) + ' / ' + data.stops.length;
    card.innerHTML = '<b>' + (s.recipient_name || ('Paket ' + (s.package_id || s.stop_order))) + '</b>'
      + '<div>' + (s.service_type || '') + ' · '
      + (leg.distance_km != null ? leg.distance_km + ' km' : '-') + ' · '
      + (leg.duration_mins != null ? leg.duration_mins + ' mnt' : '-') + '</div>'
      + '<div class="coords">' + s.latitude.toFixed(5) + ', ' + s.longitude.toFixed(5) + '</div>';
    $('btn-deliver').disabled = true;
    $('btn-recalc').disabled = !(deviating && curPos);
    $('btn-nav-start').disabled = !(navRouteId && navStatus.enabled);
    navInd(navRouteId
      ? (navStatus.enabled ? 'Navigasi: siap (leg ' + (activeIndex + 1) + ')' : 'Navigasi: nonaktif (ENABLE_LIVE_NAVIGATION=0)')
      : 'Navigasi: nonaktif (server tidak set route_id)');
    checkGeofence(s);
  }

  /* --- Real-time navigation (WS /api/v1/ws/navigation) ------------------ */
  function navInd(msg, state) {
    var el = $('nav-ind');
    el.textContent = msg;
    el.className = 'nav-ind' + (state ? ' ' + state : '');
  }

  function setNavProgress(data) {
    $('nav-progress-wrap').style.display = 'block';
    if (data.progress_pct != null) {
      $('nav-progress-fill').style.width = Math.max(0, Math.min(100, data.progress_pct)) + '%';
    }
    $('nav-remaining').textContent = data.remaining_distance_m != null
      ? 'sisa ' + Math.round(data.remaining_distance_m).toLocaleString('id-ID') + ' m'
      : '-';
    $('nav-eta').textContent = data.remaining_time_s != null
      ? 'ETA ' + formatEta(data.remaining_time_s)
      : '-';

    // NEW: Current speed
    if (data.current_speed_kmh != null) {
      $('nav-speed').textContent = Math.round(data.current_speed_kmh) + ' km/h';
    }
    // NEW: Average speed
    if (data.average_speed_kmh != null) {
      $('nav-avg-speed').textContent = 'Avg: ' + Math.round(data.average_speed_kmh) + ' km/h';
    }
    // NEW: Traffic level indicator
    if (data.traffic_level != null) {
      var trafficEl = $('nav-traffic');
      if (trafficEl) {
        trafficEl.textContent = data.traffic_level.charAt(0).toUpperCase() + data.traffic_level.slice(1);
        trafficEl.className = 'nav-traffic ' + data.traffic_level;
      }
    }
    // NEW: Leg context for multi-stop
    if (data.leg_context != null) {
      var legCtx = data.leg_context;
      var legInfoEl = $('nav-leg-info');
      var recipientEl = $('nav-recipient');
      if (legInfoEl) {
        legInfoEl.style.display = 'block';
        legInfoEl.textContent = 'Stop ' + legCtx.stop_sequence + ' / ' + legCtx.total_legs;
      }
      if (recipientEl) {
        recipientEl.textContent = legCtx.recipient_name || '';
      }
    }
    // NEW: Next maneuver
    if (data.next_maneuver != null) {
      showNextManeuver(data.next_maneuver);
    }
  }

  function showNextManeuver(maneuver) {
    var panel = $('nav-maneuver');
    var textEl = $('nav-maneuver-text');
    var distEl = $('nav-maneuver-distance');
    var iconEl = panel.querySelector('.nav-maneuver-icon');

    if (!panel || !textEl || !distEl) return;

    panel.classList.add('show');
    textEl.textContent = maneuver.instruction || 'Lurus';

    var dist = maneuver.distance_m;
    if (dist != null) {
      distEl.textContent = dist >= 1000
        ? (dist / 1000).toFixed(1) + ' km'
        : Math.round(dist) + ' m';
    } else {
      distEl.textContent = '-';
    }

    // Set turn icon based on maneuver type
    var iconHtml = '';
    switch (maneuver.type) {
      case 'turn_left':
        iconHtml = '↰';
        break;
      case 'turn_right':
        iconHtml = '↱';
        break;
      case 'turn_slight_left':
        iconHtml = '↖';
        break;
      case 'turn_slight_right':
        iconHtml = '↗';
        break;
      case 'continue':
        iconHtml = '↑';
        break;
      case 'roundabout_exit':
        iconHtml = '↻';
        break;
      case 'uturn':
        iconHtml = '↺';
        break;
      case 'arrive':
        iconHtml = '🏁';
        break;
      default:
        iconHtml = '↑';
    }
    iconEl.innerHTML = iconHtml;
    iconEl.style.fontSize = '20px';
  }

  function redrawNavPolyline(encoded) {
    if (navRouteLayer) map.removeLayer(navRouteLayer);
    var coords = decodePolyline(encoded || '');
    if (!coords.length) return;
    navRouteLayer = L.polyline(coords.map(function (p) { return [p[0], p[1]]; }), {
      color: '#b3372f', weight: 6, opacity: 0.95, dashArray: '8 6', pane: 'route'
    }).addTo(map);
    map.fitBounds(navRouteLayer.getBounds(), { padding: [40, 40] });
  }

  function sendNav(msg) {
    if (navWs && navWs.readyState === WebSocket.OPEN) navWs.send(JSON.stringify(msg));
  }

  function navSimulate() {
    if (!navRouteCoords || navSimIdx >= navRouteCoords.length) return;
    var p = navRouteCoords[Math.floor(navSimIdx)];
    navSimIdx += 2;
    sendNav({ type: 'location_update', lat: p[0], lng: p[1], current_route_id: navRouteId });
  }

  function stopNavigation() {
    if (testRerouteMode) disableTestRerouteMode();
    if (navSimTimer) { clearInterval(navSimTimer); navSimTimer = null; }
    if (navWs) { try { navWs.close(); } catch (e) {} navWs = null; }
    if (navRouteLayer) { map.removeLayer(navRouteLayer); navRouteLayer = null; }
    // JANGAN reset navRouteId & navRouteCoords di sini - biarkan sampai ack/error diterima
    navSimIdx = 0;
    navConnecting = false;
    $('btn-nav-start').textContent = 'Mulai Navigasi (Leg Aktif)';
    $('btn-nav-start').disabled = !(navRouteId && navStatus.enabled);
    $('btn-nav-apply').style.display = 'none';
    $('nav-progress-wrap').style.display = 'none';
    $('nav-progress-fill').style.width = '0%';
    navInd(navStatus.enabled ? 'Navigasi: siap' : 'Navigasi: nonaktif (ENABLE_LIVE_NAVIGATION=0)');
  }

  function startNavigation() {
    if (!navRouteId || navConnecting) return;
    if (!auth.token) {
      showNotice('Login dulu untuk navigasi (perlu token autentikasi).');
      return;
    }
    // Hentikan navigasi lama kalau ada, tapi jangan reset navRouteId
    if (navSimTimer || (navWs && navWs.readyState === WebSocket.OPEN)) {
      if (navSimTimer) { clearInterval(navSimTimer); navSimTimer = null; }
      if (navWs) { try { navWs.close(); } catch (e) {} navWs = null; }
      if (navRouteLayer) { map.removeLayer(navRouteLayer); navRouteLayer = null; }
      navSimIdx = 0;
    }
    navConnecting = true;
    navInd('Navigasi: menghubungkan...');
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    navWs = new WebSocket(proto + location.host + '/api/v1/ws/navigation?token=' + encodeURIComponent(auth.token));
    navWs.onopen = function () {
      sendNav({ type: 'start_navigation', route_id: navRouteId, leg_index: activeIndex });
    };
    navWs.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.type === 'error') {
        navConnecting = false;
        navInd('Navigasi: error (' + msg.detail + ')', 'warn');
        log('NAV error: ' + msg.detail);
        // JANGAN panggil stopNavigation() - biarkan navRouteId tetap ada
        // User bisa klik tombol lagi untuk retry
        $('btn-nav-start').textContent = 'Mulai Navigasi (Leg Aktif)';
        $('btn-nav-start').disabled = false;
        return;
      }
      if (msg.type === 'ack' && msg.route_id != null) {
        navConnecting = false;
        navInd('Navigasi: leg ' + (msg.leg_index + 1) + ' aktif', 'active');
        navRouteCoords = decodePolyline(msg.polyline || '');
        navSimIdx = 0;
        $('btn-nav-start').textContent = 'Berhenti Navigasi';
        $('btn-nav-start').disabled = false;
        if (navSimTimer) clearInterval(navSimTimer);
        navSimulate();
        navSimTimer = setInterval(navSimulate, 4000);
        checkNavStatus();
        return;
      }
      if (msg.type === 'route_progress') setNavProgress(msg);
      if (msg.type === 'turn_by_turn') {
        var maneuver = msg.maneuver;
        if (maneuver) showNextManeuver(maneuver);
      }
      if (msg.type === 'off_route_warning') {
        var dist = Math.round(msg.distance_m);
        var thr = msg.threshold_m;
        navInd('Di luar rute (' + dist + ' m > ' + thr + ' m)', 'offroute');
        if (testRerouteMode) {
          var statusEl = $('reroute-test-status');
          if (statusEl) {
            statusEl.textContent = 'Mode Test: Off-route ' + dist + 'm (threshold ' + thr + 'm). Coba klik lebih jauh untuk trigger auto-reroute.';
            statusEl.style.color = dist > thr ? '#b3372f' : '#f0ad4e';
          }
        }
      }
      if (msg.type === 'stops_reordered') {
        // Update package queue state from reordered payload.
        if (msg.new_stop_order && state) {
          // Rebuild stops array in the NEW order from backend.
          var newStops = msg.new_stop_order.map(function(s) {
            return {
              stop_order: s.stop_order,
              package_id: s.package_id,
              recipient_name: s.recipient_name,
              service_type: s.service_type || 'REGULAR',
              latitude: s.dest[0],
              longitude: s.dest[1],
              alamat: ''
            };
          });

          // Use full legs geometry when available; otherwise build minimal
          // straight-line segments between consecutive stops so the map
          // still renders a connected route (never falls back to drawOverview
          // which would clearMapLayers() and wipe the rerouted polyline).
          // Distance/duration dihitung nyata via haversine ÷ speed default,
          // bukan hardcoded 0 — agar route sheet tidak menampilkan "0 mnt".
          var FALLBACK_SPEED_KMH = 40;
          function legMetrics(prev, dest) {
            var distM = haversineDistance(prev, dest);
            var durMin = distM / (FALLBACK_SPEED_KMH / 3.6) / 60.0;
            return {
              distance_km: Math.round(distM / 10.0) / 100.0, // 2 desimal
              duration_mins: Math.round(durMin * 10.0) / 10.0, // 1 desimal
              estimated_time_seconds: Math.round(durMin * 60.0 * 10.0) / 10.0
            };
          }

          var newLegs = (msg.legs && msg.legs.length > 0)
            ? msg.legs
            : newStops.map(function(s, i) {
                if (i === 0) return { leg_index: 0, geometry: '', distance_km: 0, duration_mins: 0, estimated_time_seconds: null };
                var prev = [newStops[i - 1].latitude, newStops[i - 1].longitude];
                var m = legMetrics(prev, [s.latitude, s.longitude]);
                return Object.assign({
                  leg_index: i,
                  geometry: encodePolyline([prev, [s.latitude, s.longitude]], 5)
                }, m);
              });

          // Atomic state sync BEFORE rendering — keeps UI list & map consistent.
          state.stops = newStops;
          state.legs = newLegs;
          activeIndex = msg.active_leg_index ?? 0;

          // drawReorderedRoute is the SINGLE executor of map rendering here.
          drawReorderedRoute({
            legs: newLegs,
            stops: newStops,
            active_leg_index: msg.active_leg_index ?? 0,
            current_position: msg.current_position
          });
          log('Stops re-ordered (' + (msg.reorder_reason || 'unknown') + '): ' +
              msg.new_stop_order.map(function(s) { return s.package_id; }).join(' → '));
        }
        
        // Handle active stop switch (major re-order)
        if (msg.active_stop_switched) {
          var switchedTo = msg.switched_to;
          var switchedFrom = msg.switched_from;
          
          // Show prominent notification with sound
          var notifMsg = '🔄 ACTIVE STOP SWITCHED: ' + 
            (switchedFrom?.recipient_name || 'Stop ' + activeIndex) + ' → ' + 
            (switchedTo?.recipient_name || 'New Stop');
          
          showToast(notifMsg, 'error'); // error type = red, prominent
          
          // Play notification sound if available
          try {
            var audio = new Audio('data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQQAAAD//w=='); // Short beep
            audio.volume = 0.5;
            audio.play().catch(function() {}); // Ignore autoplay restrictions
          } catch (e) {}
          
          // Update nav status indicator
          navInd('Active stop switched: ' + (switchedTo?.recipient_name || 'New stop'), 'warn');
          
          // Log detailed switch info
          log('ACTIVE STOP SWITCHED: ' + (switchedFrom?.recipient_name || 'Unknown') + 
              ' → ' + (switchedTo?.recipient_name || 'Unknown') + 
              ' (reason: ' + (msg.reorder_reason || 'off_route_major_switch_active') + ')');
          
          // Update activeIndex to match new active leg
          // The backend already incremented leg_index, so activeIndex should match
          if (state && state.stops.length > 0) {
            // Find the index of the new active stop
            var newActiveIdx = state.stops.findIndex(function(s) { 
              return s.package_id === switchedTo?.package_id; 
            });
            if (newActiveIdx >= 0) {
              activeIndex = newActiveIdx;
              // drawReorderedRoute already rendered the correct route, no need to call drawOverview
            }
          }
        } else if (msg.reorder_reason === 'off_route_closer_to_next_stop') {
          showToast('Urutan stop diubah otomatis: ' + (msg.reorder_reason || 'off_route_closer_to_next_stop'), 'warning');
        } else {
          showToast('Urutan stop diubah otomatis: ' + (msg.reorder_reason || 'off_route_closer_to_next_stop'), 'warning');
        }
      }
      if (msg.type === 'auto_rerouted' || msg.type === 'reroute_available') {
        navInd('Rute baru leg ' + (msg.leg_index + 1) + ' (hemat ' + Math.round(msg.saving_s) + ' dtk)', 'active');
        redrawNavPolyline(msg.polyline);
        navRouteCoords = decodePolyline(msg.polyline || '');
        navSimIdx = 0;
        if (msg.applied) {
          log('Auto-reroute leg aktif diterapkan (hemat ' + Math.round(msg.saving_s) + ' dtk).');
        } else {
          $('btn-nav-apply').style.display = '';
          $('btn-nav-apply').dataset.polyline = msg.polyline || '';
        }
      }
    };
    navWs.onclose = function () {
      if (navSimTimer) { clearInterval(navSimTimer); navSimTimer = null; }
      if (navConnecting) {
        // Koneksi tertutup saat masih connecting = gagal connect
        navConnecting = false;
        navInd('Navigasi: gagal terhubung ke server', 'warn');
        log('NAV: koneksi tertutup saat connecting');
        $('btn-nav-start').textContent = 'Mulai Navigasi (Leg Aktif)';
        $('btn-nav-start').disabled = false;
      } else {
        navInd('Navigasi: nonaktif');
      }
    };
    navWs.onerror = function () {
      navConnecting = false;
      navInd('Navigasi: koneksi gagal', 'warn');
    };
  }

  $('btn-nav-start').addEventListener('click', function () {
    if (navConnecting) return; // Prevent double-click while connecting
    if (navSimTimer || (navWs && navWs.readyState === WebSocket.OPEN)) {
      stopNavigation();
    } else {
      startNavigation();
    }
  });

  $('btn-nav-apply').addEventListener('click', function () {
    var polyline = $('btn-nav-apply').dataset.polyline;
    if (!polyline) return;
    $('btn-nav-apply').style.display = 'none';
    redrawNavPolyline(polyline);
    navRouteCoords = decodePolyline(polyline);
    navSimIdx = 0;
    log('Rute baru leg aktif diterapkan manual.');
  });

  async function checkNavStatus() {
    try {
      var resp = await fetch('/api/v1/ws/navigation/status');
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      navStatus = data;
    } catch (err) {
      navStatus = { enabled: false };
    }
    navInd(navStatus.enabled ? 'Navigasi: siap' : 'Navigasi: nonaktif (ENABLE_LIVE_NAVIGATION=0)');
    // Enable test mode radio only when navigation WS is connected
    var testRadio = $('inp-mode-reroute_test');
    if (testRadio) {
      // Use local session state: navRouteId exists AND navWs is connected
      var hasActiveRoute = !!(navRouteId && navWs && navWs.readyState === WebSocket.OPEN);
      testRadio.disabled = !(navStatus.enabled && hasActiveRoute);
    }
  }

  function buildPayload(remaining) {
    var payload = {
      deliveries: remaining.map(function (s) {
        return {
          package_id: s.package_id,
          recipient_name: s.recipient_name,
          service_type: s.service_type,
          alamat: s.alamat || '',
          latitude: s.latitude,
          longitude: s.longitude
        };
      }),
      mode: vehicleMode(),
      last_mile_precision: true,
      skip_traffic: $('chk-skip-traffic').checked,
      return_to_hub: false
    };
    if (curPos) {
      payload.courier_position = { latitude: curPos[0], longitude: curPos[1] };
    } else if (hub) {
      payload.hub_origin = { latitude: hub[0], longitude: hub[1] };
    }
    return payload;
  }

  function buildOptimizePayload(deliveries) {
    var payload = {
      deliveries: deliveries.map(function (d) {
        var item = { alamat: d.alamat, service_type: d.service_type, recipient_name: d.alamat.split(',')[0] };
        if (d.latitude != null && d.longitude != null) {
          item.latitude = d.latitude;
          item.longitude = d.longitude;
        }
        return item;
      }),
      mode: vehicleMode(),
      last_mile_precision: true,
      skip_traffic: $('chk-skip-traffic').checked,
      return_to_hub: $('chk-return').checked
    };
    if (curPos) {
      payload.courier_position = { latitude: curPos[0], longitude: curPos[1] };
    }
    if (hub) {
      payload.hub_origin = { latitude: hub[0], longitude: hub[1] };
    }
    return payload;
  }

  function remainingStops() {
    return state ? state.stops.slice(activeIndex) : [];
  }

  var GEOFENCE_RADIUS_M = 30;
  var geoOk = false;
  var geoSeq = 0;

  async function checkGeofence(s) {
    var el = $('nav-geofence');
    var seq = ++geoSeq;
    $('btn-deliver').disabled = true;
    if (!curPos || !s || s.latitude == null || s.longitude == null) {
      geoOk = false;
      if (seq === geoSeq) el.textContent = 'Geofence: lokasi stop tidak tersedia';
      return;
    }
    el.textContent = 'Cek geofence…';
    try {
      var resp = await fetch('/api/v1/pathfinding/geofence-check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          current: { latitude: curPos[0], longitude: curPos[1] },
          target: { latitude: s.latitude, longitude: s.longitude },
          radius_m: GEOFENCE_RADIUS_M
        }),
        signal: AbortSignal.timeout(10000)
      });
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      if (seq !== geoSeq) return;
      geoOk = !!data.within_radius;
      el.textContent = (data.within_radius ? '✅ Dalam radius geofence (' +
        data.distance_m + ' m)' : '⛔ Di luar geofence (' + data.distance_m +
        ' m > ' + GEOFENCE_RADIUS_M + ' m)');
      el.style.color = data.within_radius ? '#1e7e34' : '#b3372f';
      if (data.within_radius) $('btn-deliver').disabled = false;
    } catch (err) {
      if (seq !== geoSeq) return;
      geoOk = false;
      el.textContent = 'Geofence gagal: ' + ((err && err.message) ? err.message : String(err));
      el.style.color = '#b3372f';
    }
  }

  function normalizeData(data, isRecalc) {
    if (isRecalc) {
      // hitung ulang: gabungkan sisa dengan hub_pos sebagai awal
      var offset = activeIndex;
      var stops = state.stops.slice(offset).map(function (s, i) {
        return { stop_order: i + 1, package_id: s.package_id,
                 recipient_name: s.recipient_name, service_type: s.service_type,
                 latitude: s.latitude, longitude: s.longitude, alamat: s.alamat || '' };
      });
      var legs = data.legs.map(function (l) {
        return { leg_index: l.leg_index, stop_sequence_number: l.stop_sequence_number,
                 package_id: l.package_id, recipient_name: l.recipient_name,
                 service_type: l.service_type, geometry: l.geometry,
                 distance_km: l.distance_km, duration_mins: l.duration_mins };
      });
      return { stops: stops, legs: legs };
    }
    return data;
  }

  async function runOptimize() {
    if (!(hub || curPos)) return;
    var deliveries = parseDeliveries();
    if (!deliveries.length) return;
    var btn = $('btn-optimize');
    btn.disabled = true;
    log('Mengoptimasi rute multi-stop...');
    showLoading('Optimasi rute pengantaran... (geocode + routing per leg, bisa butuh waktu)');
    var controller = new AbortController();
    var timeoutId = setTimeout(function () { controller.abort(); }, 600000);
    try {
      var resp = await fetch('/api/v1/pathfinding/find-optimized-delivery-route', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify(buildOptimizePayload(deliveries)),
        signal: controller.signal
      });
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      state = normalizeData(data, false);
      activeIndex = 0;
      deviating = false;
      navRouteId = data.route_id || null;
      if (navSimTimer || (navWs && navWs.readyState === WebSocket.OPEN)) stopNavigation();
      renderResult(data);
      drawOverview(state);
      log('Rute OK: ' + data.total_distance_km + ' km, ' + data.total_duration_mins +
          ' mnt, ' + data.total_legs + ' leg, ' + data.stops.length + ' stop (sumber=' + data.source + ')' +
          (data.start_source ? ' · titik awal=' + data.start_source : '') +
          (navRouteId ? ' · route_id=' + navRouteId : ''));
    } catch (err) {
      var friendly = (err && err.name === 'AbortError')
        ? 'Waktu habis (10 mnt). Area mungkin di luar cakupan peta.'
        : ((err && err.message) ? err.message : String(err));
      log('ERROR: ' + friendly);
      renderResult({ status: 'error: ' + friendly });
      showNotice(friendly);
    } finally {
      clearTimeout(timeoutId);
      hideLoading();
      btn.disabled = !canOptimize();
    }
  }

  async function runRecalc() {
    if (!state || !deviating || !curPos) return;
    var remain = remainingStops();
    if (!remain.length) return;
    var btn = $('btn-recalc');
    btn.disabled = true;
    log('Menghitung ulang sisa rute dari posisi kurir...');
    showLoading('Menghitung ulang sisa rute...');
    try {
      var resp = await fetch('/api/v1/pathfinding/find-optimized-delivery-route', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify(buildPayload(remain)),
        signal: AbortSignal.timeout(600000)
      });
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      state = normalizeData(data, true);
      activeIndex = 0;
      deviating = false;
      navRouteId = data.route_id || null;
      if (navSimTimer || (navWs && navWs.readyState === WebSocket.OPEN)) stopNavigation();
      renderResult({ status: 'success', total_distance_km: data.total_distance_km,
                     total_duration_mins: data.total_duration_mins,
                     total_legs: data.total_legs, source: data.source, warning: data.warning,
                     start_source: data.start_source });
      drawOverview(state);
      log('Sisa rute dioptimasi ulang: ' + data.total_distance_km + ' km, ' +
          data.total_duration_mins + ' mnt, ' + data.stops.length + ' stop.' +
          (data.start_source ? ' · titik awal=' + data.start_source : '') +
          (navRouteId ? ' · route_id=' + navRouteId : ''));
    } catch (err) {
      var friendly = (err && err.message) ? err.message : String(err);
      log('ERROR recalc: ' + friendly);
      showNotice(friendly);
    } finally {
      hideLoading();
      btn.disabled = true;
    }
  }

  function renderResult(data) {
    $('r-status').textContent = data.status || '-';
    $('r-dist').textContent = data.total_distance_km != null ? (data.total_distance_km.toLocaleString('id-ID') + ' km') : '-';
    $('r-eta').textContent = data.total_duration_mins != null ? (data.total_duration_mins.toLocaleString('id-ID') + ' mnt') : '-';
    $('r-legs').textContent = data.total_legs != null ? data.total_legs : '-';
    $('r-source').textContent = data.source || '-';
    $('r-warning').textContent = data.warning || '-';
    $('txt-start-source').textContent = data.start_source || '-';
    var src = data.source || '';
    if (src) setBadge('b-source', 'sumber: ' + src, 'ok');
    if (src && data.warning) showNotice('Peringatan: ' + data.warning);
  }

  map.on('click', function (e) {
    var mode = document.querySelector('input[name="mode"]:checked').value;
    if (mode === 'hub') {
      placeHub(e.latlng);
    } else if (mode === 'reroute_test') {
      if (!testRerouteMode) {
        if (!enableTestRerouteMode()) return;
      }
      if (!navRouteId) {
        showNotice('Belum ada rute aktif. Optimasi rute dulu.');
        disableTestRerouteMode();
        return;
      }
      sendTestReroutePosition(e.latlng.lat, e.latlng.lng);
    } else {
      deviating = true;
      setCourierPosition(e.latlng.lat, e.latlng.lng, { deviate: true });
      log('Deviasi: kurir kini di ' + fmt(curPos));
      if (state && activeIndex < state.stops.length) checkGeofence(state.stops[activeIndex]);
      if (state) $('btn-recalc').disabled = false;
    }
  });

  $('btn-login').addEventListener('click', doLogin);
  $('btn-logout').addEventListener('click', doLogout);
  $('btn-geo').addEventListener('click', function () {
    if (!navigator.geolocation) { log('ERROR: geolocation tidak didukung browser.'); return; }
    log('Mengambil lokasi realtime...');
    navigator.geolocation.getCurrentPosition(function (pos) {
      var lat = pos.coords.latitude;
      var lon = pos.coords.longitude;
      // Set sebagai posisi kurir (kirim ke WS jika terhubung via setCourierPosition)
      setCourierPosition(lat, lon);
      // Set sebagai hub/titik awal
      hub = [lat, lon];
      if (hubMarker) map.removeLayer(hubMarker);
      hubMarker = L.marker([lat, lon], {
        icon: numberedIcon('H', iconColors.hub)
      }).addTo(map).bindTooltip('Hub (dari GPS)').openTooltip();
      $('txt-hub').textContent = fmt(hub);
      updateOptimizeBtn();
      log('Hub & posisi kurir diset ke: ' + fmt([lat, lon]) + ' (±' + Math.round(pos.coords.accuracy) + ' m)');
    }, function (err) {
      log('ERROR lokasi (' + err.code + '): ' + err.message);
    }, { enableHighAccuracy: true, timeout: 15000 });
  });

  $('chk-stream').addEventListener('change', function () {
    if (this.checked) startPositionStream();
    else stopPositionStream();
  });

  $('btn-send-pos').addEventListener('click', sendPositionNow);

  $('btn-optimize').addEventListener('click', runOptimize);
  $('btn-recalc').addEventListener('click', runRecalc);

  $('btn-deliver').addEventListener('click', function () {
    if (!state) return;
    if (!geoOk) {
      showNotice('Kurir belum berada dalam radius geofence (≤ ' + GEOFENCE_RADIUS_M + ' m) dari stop aktif.');
      log('Konfirmasi ditolak: di luar radius geofence.');
      return;
    }
    if (activeIndex < state.stops.length) {
      activeIndex += 1;
      log('Paket dikonfirmasi terkirim. Lanjut ke stop ' + (activeIndex + 1) + '.');
      if (navWs && navWs.readyState === WebSocket.OPEN) {
        sendNav({ type: 'start_navigation', route_id: navRouteId, leg_index: activeIndex });
        navSimIdx = 0;
      }
    }
    drawOverview(state);
  });

  $('txt-deliveries').addEventListener('input', updateOptimizeBtn);

  $('btn-reset').addEventListener('click', function () {
    stopNavigation();
    stopPositionStream();
    hideLoading();
    hub = curPos = null; deviating = false; activeIndex = 0; state = null;
    geoOk = false; geoSeq++;
    [hubMarker, posMarker].forEach(function (m) { if (m) map.removeLayer(m); });
    hubMarker = posMarker = null;
    clearMapLayers();
    $('nav-controls').style.display = 'none';
    $('txt-hub').textContent = '-'; $('txt-pos').textContent = '-';
    $('txt-start-source').textContent = '-';
    updateOptimizeBtn();
    $('nav-geofence').textContent = 'Cek geofence…';
    $('nav-geofence').style.color = '';
    $('r-status').textContent = '-'; $('r-dist').textContent = '-';
    $('r-eta').textContent = '-'; $('r-legs').textContent = '-';
    $('r-source').textContent = '-'; $('r-warning').textContent = '-';
    setBadge('b-source', 'sumber: ?', '');
    hideNotice();
    log('Peta direset.');
  });

  $('sheet-close').addEventListener('click', function () {
    $('route-sheet').className = 'route-sheet';
  });

  // Test mode radio button handler
  var testRadio = $('inp-mode-reroute_test');
  if (testRadio) {
    testRadio.addEventListener('change', function () {
      if (this.checked) {
        if (!enableTestRerouteMode()) {
          this.checked = false;
          $('inp-mode-hub').checked = true;
        }
      } else {
        disableTestRerouteMode();
      }
    });
  }

  // Hub/Deviate radio buttons - disable test mode when selected
  var hubRadio = $('inp-mode-hub');
  var deviateRadio = $('inp-mode-deviate');
  if (hubRadio) {
    hubRadio.addEventListener('change', function () {
      if (this.checked && testRerouteMode) disableTestRerouteMode();
    });
  }
  if (deviateRadio) {
    deviateRadio.addEventListener('change', function () {
      if (this.checked && testRerouteMode) disableTestRerouteMode();
    });
  }

  async function boot() {
    renderAuthUI();
    checkNavStatus();
    verifyToken();
    log('Memuat status server...');
    showLoading('Memeriksa status server...');
    try {
      var resp = await fetch('/health');
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      setBadge('b-server', 'Server: OK', 'ok');
      log('Server OK. Login kurir (opsional), klik peta untuk Hub / gunakan lokasi, isi alamat penerima, lalu Optimasi.');
    } catch (err) {
      setBadge('b-server', 'Server: ERROR', 'bad');
      log('ERROR server: ' + err.message);
    } finally {
      hideLoading();
    }
  }

  boot();
})();

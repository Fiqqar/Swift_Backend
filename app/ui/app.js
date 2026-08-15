(function () {
  "use strict";

  var map = L.map('map').setView([-6.8048, 110.8385], 14);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var trafficPane = map.createPane('traffic');
  trafficPane.style.zIndex = 430;
  var routePane = map.createPane('route');
  routePane.style.zIndex = 460;

  var sheetEl = document.getElementById('route-sheet');
  if (sheetEl) {
    L.DomEvent.disableClickPropagation(sheetEl);
    L.DomEvent.disableScrollPropagation(sheetEl);
  }

  var icons = {
    origin: L.divIcon({ html: '<div style="background:#1e7e34;border:2px solid #fff;border-radius:50%;width:16px;height:16px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>', className: '', iconSize: [16, 16] }),
    dest:   L.divIcon({ html: '<div style="background:#b3372f;border:2px solid #fff;border-radius:50%;width:16px;height:16px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>', className: '', iconSize: [16, 16] }),
    live:   L.divIcon({ html: '<div style="background:#0f6dc1;border:2px solid #fff;border-radius:50%;width:14px;height:14px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>', className: '', iconSize: [14, 14] })
  };

  var origin = null, dest = null;
  var originMarker = null, destMarker = null;
  var routeLayer = null, trackLayer = null, liveMarker = null;
  var watchId = null;
  var trafficLayer = null, trafficTimer = null;
  var trafficEnabledServer = false;
  var routeOptions = null, selectedRouteId = 1;
  var routeLines = {}, incidentLayer = null, incidentPopups = [];
  var routeInfoPopups = [];

  function $(id) { return document.getElementById(id); }
  function log(msg) {
    $('log').textContent = (new Date().toLocaleTimeString()) + " " + msg;
  }

  async function parseJson(resp) {
    var ct = (resp.headers.get('content-type') || '').toLowerCase();
    if (ct.indexOf('application/json') === -1) {
      var text = await resp.text();
      throw new Error('Server ' + resp.status + ' bukan JSON: ' + (text || '').slice(0, 300));
    }
    return resp.json();
  }

  function setBadge(id, label, state) {
    var el = $(id);
    el.textContent = label;
    el.className = 'badge ' + (state || '');
  }

  function showNotice(msg) {
    if (msg) {
      $('notice').textContent = msg;
      $('notice').className = 'show';
    } else {
      hideNotice();
    }
  }

  function hideNotice() {
    $('notice').textContent = '';
    $('notice').className = '';
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

  function isPbf(source) {
    return source === 'pbf' || (typeof source === 'string' && source.indexOf('pbf:') === 0);
  }

  function pbfName(source) {
    if (!isPbf(source) || source === 'pbf') return '';
    return source.slice(4);
  }

  function sourceInfo(source) {
    if (isPbf(source)) {
      var name = pbfName(source);
      return { label: name ? ('Lokal (PBF: ' + name + ')') : 'Lokal (PBF)', state: 'ok' };
    }
    if (source === 'osm') return { label: 'API Eksternal (Overpass)', state: 'info' };
    return { label: 'Tidak tersedia (offline)', state: 'bad' };
  }

  function applySource(source) {
    var info = sourceInfo(source);
    setBadge('b-source', 'sumber: ' + info.label, info.state);
    $('r-source').textContent = info.label;
    if (isPbf(source) || source === 'osm') {
      hideNotice();
    } else {
      showNotice('Data peta tidak tersedia: PBF lokal tidak ada & API eksternal tidak terjangkau. Hasil bersifat terbatas (offline).');
    }
  }

  function fmt(coord) {
    return coord ? coord[0].toFixed(5) + ', ' + coord[1].toFixed(5) : '-';
  }

  function placePoint(type, latlng) {
    if (type === 'origin') {
      origin = [latlng.lat, latlng.lng];
      if (originMarker) map.removeLayer(originMarker);
      originMarker = L.marker(latlng, { icon: icons.origin }).addTo(map).bindTooltip('Awal').openTooltip();
      $('txt-origin').textContent = fmt(origin);
    } else {
      dest = [latlng.lat, latlng.lng];
      if (destMarker) map.removeLayer(destMarker);
      destMarker = L.marker(latlng, { icon: icons.dest }).addTo(map).bindTooltip('Tujuan').openTooltip();
      $('txt-dest').textContent = fmt(dest);
    }
    $('btn-route').disabled = !(origin && dest);
  }

  function mode() {
    return document.querySelector('input[name="mode"]:checked').value;
  }

  function vehicleMode() {
    var el = document.querySelector('input[name="vehicle"]:checked');
    return el ? el.value : 'car';
  }

  map.on('click', function (e) {
    placePoint(mode(), e.latlng);
  });

  function formatEta(secs) {
    secs = Math.round(secs);
    if (secs >= 86400) {
      var d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600);
      return d + ' hari' + (h ? ' ' + h + ' jam' : '');
    }
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

  function routeTrafficColor(multiplier, closure) {
    if (closure || multiplier >= 4) return '#d81b1b';
    if (multiplier >= 2.5) return '#f0741f';
    return '#f4c20d';
  }

  function drawRouteTraffic(group, coords, trafficSegments) {
    if (!group || !coords || !trafficSegments || !trafficSegments.length) return;
    trafficSegments.forEach(function (seg) {
      if (!seg.multiplier || seg.multiplier <= 1.0) return;
      var start = Math.max(0, seg.start_index);
      var end = Math.min(coords.length - 1, seg.end_index);
      if (start >= end) return;
      var pts = [];
      for (var j = start; j <= end; j++) pts.push([coords[j][0], coords[j][1]]);
      var closed = seg.multiplier >= 4;
      var dash = closed ? '8 6' : null;
      L.polyline(pts, {
        color: '#1f2933', weight: 11, opacity: 0.9,
        pane: 'route', dashArray: dash
      }).addTo(group);
      L.polyline(pts, {
        color: routeTrafficColor(seg.multiplier, closed),
        weight: 7, opacity: 0.95,
        pane: 'route', dashArray: dash
      }).addTo(group);
    });
  }

  function drawRoute(payload) {
    if (routeLayer) map.removeLayer(routeLayer);
    var coords = decodePolyline(payload.route_coordinates || '');
    var group = L.layerGroup().addTo(map);
    var main = L.polyline(coords.map(function (p) { return [p[0], p[1]]; }), {
      color: '#0f6dc1', weight: 5, opacity: 0.85, pane: 'route'
    });
    main.addTo(group);
    drawRouteTraffic(group, coords, payload.traffic_segments);
    routeLayer = group;
    map.fitBounds(main.getBounds(), { padding: [30, 30] });
    if (payload.estimated_time_seconds != null) {
      main.bindPopup('Estimasi waktu: ' + formatEta(payload.estimated_time_seconds));
      var mid = coords[Math.floor(coords.length / 2)];
      if (mid) main.openPopup(mid);
    }
  }

  function pushToast(type, title, sub) {
    var box = document.createElement('div');
    box.className = 'toast ' + (type || '');
    box.innerHTML = '<div class="toast-title">' + title + '</div>' +
      (sub ? '<div class="toast-sub">' + sub + '</div>' : '');
    $('toasts').appendChild(box);
    setTimeout(function () { box.remove(); }, 7000);
  }

  function clearIncidents() {
    if (incidentLayer) { map.removeLayer(incidentLayer); incidentLayer = null; }
    incidentPopups.forEach(function (p) { map.removeLayer(p); });
    incidentPopups = [];
  }

  function clearRouteInfoPopups() {
    routeInfoPopups.forEach(function (p) { map.removeLayer(p); });
    routeInfoPopups = [];
  }

  function showRouteInfoPopup(route) {
    clearRouteInfoPopups();
    if (!route) return;
    var coords = decodePolyline(route.route_coordinates || '');
    if (!coords.length) return;
    var mid = coords[Math.floor(coords.length / 2)];
    if (!mid) return;
    var isBest = route.is_best || route.route_id === 1;
    var lines = ['Rute ' + route.route_id + (isBest ? ' (Terbaik)' : '')];
    if (route.summary) lines.push(route.summary);
    var meta = [];
    if (route.distance_km != null) meta.push(route.distance_km.toLocaleString('id-ID') + ' km');
    if (route.duration_mins != null) meta.push(route.duration_mins.toLocaleString('id-ID') + ' mnt');
    if (meta.length) lines.push(meta.join(' · '));
    var popup = L.popup({ autoClose: false, closeOnClick: false, className: 'route-info-popup' })
      .setLatLng([mid[0], mid[1]])
      .setContent(lines.join('<br>'))
      .openOn(map);
    routeInfoPopups.push(popup);
  }

  function showIncidents(route) {
    clearIncidents();
    if (!route || !route.incidents || !route.incidents.length) return;
    incidentLayer = L.layerGroup().addTo(map);
    route.incidents.forEach(function (inc) {
      var ll = [inc.location[0], inc.location[1]];
      var closed = inc.type === 'road_closure';
      var color = closed ? '#d81b1b' : '#f0741f';
      if (inc.coordinates) {
        var pts = decodePolyline(inc.coordinates);
        if (pts.length >= 2) {
          L.polyline(pts, {
            color: color, weight: 6, opacity: 0.95,
            dashArray: closed ? '8 6' : null, pane: 'traffic'
          }).addTo(incidentLayer);
        }
      }
      var popup = L.popup({ autoClose: false, closeOnClick: false })
        .setLatLng(ll)
        .setContent(inc.description || 'Insiden')
        .openOn(map);
      incidentPopups.push(popup);
      pushToast(closed ? 'danger' : 'warn',
        closed ? 'Penutupan jalan' : 'Kemacetan',
        (closed ? 'Jalan ditutup di ' + fmt(ll) : (inc.description || '')));
    });
  }

  function drawRoutes(data) {
    if (routeLayer) map.removeLayer(routeLayer);
    routeOptions = data.routes || [];
    routeLines = {};
    clearIncidents();
    clearRouteInfoPopups();
    if (!routeOptions.length) return;
    routeLayer = L.layerGroup().addTo(map);
    routeOptions.forEach(function (opt) {
      var coords = decodePolyline(opt.route_coordinates || '');
      var isBest = opt.is_best || opt.route_id === 1;
      var pts = coords.map(function (p) { return [p[0], p[1]]; });
      var poly = L.polyline(pts, {
        color: isBest ? '#0f6dc1' : '#8b9aab',
        weight: isBest ? 7 : 4,
        opacity: isBest ? 0.9 : 0.8,
        pane: 'route'
      });
      poly.on('click', function () { selectRoute(opt.route_id); });
      poly.addTo(routeLayer);
      routeLines[opt.route_id] = poly;
      if (isBest) {
        drawRouteTraffic(routeLayer, coords, opt.traffic_segments);
        map.fitBounds(poly.getBounds(), { padding: [30, 30] });
      }
    });
    selectedRouteId = routeOptions[0].route_id || 1;
    renderRouteSheet(routeOptions);
    selectRoute(selectedRouteId);
  }

  function renderRouteSheet(routes) {
    var sheet = $('route-sheet');
    var listEl = $('sheet-list');
    if (!routes || !routes.length) {
      sheet.className = 'route-sheet';
      listEl.innerHTML = '';
      return;
    }
    var best = routes[0];
    var bestDur = best.duration_mins != null ? best.duration_mins : null;
    var list = '';
    routes.forEach(function (opt) {
      var isBest = opt.is_best || opt.route_id === 1;
      var delta = '';
      if (!isBest && bestDur != null && opt.duration_mins != null) {
        var d = Math.round(opt.duration_mins - bestDur);
        if (d > 0) delta = ' +' + d + ' mnt';
      }
      var incCount = (opt.incidents || []).length;
      var badge = '';
      if (isBest) badge += ' <span class="best-tag">Terbaik</span>';
      if (incCount) badge += ' <span class="incident-badge">' + incCount + ' insiden</span>';
      list += '<div class="sheet-card' + (opt.route_id === selectedRouteId ? ' selected' : '') + '" data-route="' + opt.route_id + '">'
        + '<span class="swatch ' + (isBest ? 'best' : 'alt') + '"></span>'
        + '<div class="card-main">'
        + '<div class="card-title">Rute ' + opt.route_id + badge + '</div>'
        + '<div class="card-sub">' + (opt.summary || '') + '</div>'
        + '</div>'
        + '<div class="card-meta">'
        + (opt.distance_km != null ? opt.distance_km.toLocaleString('id-ID') + ' km' : '-')
        + '<span class="delta">' + (opt.duration_mins != null ? opt.duration_mins.toLocaleString('id-ID') + ' mnt' : '-') + delta + '</span>'
        + '</div>'
        + '</div>';
    });
    listEl.innerHTML = list;
    sheet.className = 'route-sheet show';
    Array.prototype.forEach.call(listEl.querySelectorAll('.sheet-card'), function (card) {
      card.addEventListener('click', function () {
        selectRoute(parseInt(card.getAttribute('data-route'), 10));
      });
    });
  }

  function selectRoute(routeId) {
    selectedRouteId = routeId;
    if (!routeOptions || !routeOptions.length) return;
    routeOptions.forEach(function (opt) {
      var poly = routeLines[opt.route_id];
      if (!poly) return;
      var isBest = opt.is_best || opt.route_id === 1;
      if (opt.route_id === routeId) {
        poly.setStyle({ weight: isBest ? 9 : 6, opacity: 1 });
      } else {
        poly.setStyle({ weight: isBest ? 7 : 4, opacity: 0.4 });
      }
    });
    Array.prototype.forEach.call($('sheet-list').querySelectorAll('.sheet-card'), function (card) {
      card.className = 'sheet-card' +
        (parseInt(card.getAttribute('data-route'), 10) === routeId ? ' selected' : '');
    });
    var sel = null;
    routeOptions.forEach(function (opt) { if (opt.route_id === routeId) sel = opt; });
    showRouteInfoPopup(sel);
    showIncidents(sel);
  }

  function trafficColor(multiplier, closure) {
    if (closure || multiplier >= 4) return '#d81b1b';
    if (multiplier >= 2.5) return '#f0741f';
    if (multiplier >= 1.4) return '#f4c20d';
    return '#2e9e4f';
  }

  function drawTraffic(segments) {
    if (trafficLayer) map.removeLayer(trafficLayer);
    trafficLayer = L.layerGroup().addTo(map);
    if (!segments || !segments.length) {
      log('Traffic: tidak ada segmen macet saat ini.');
      return;
    }
    segments.forEach(function (seg) {
      var pts = decodePolyline(seg.coordinates || '');
      var closed = seg.closure || seg.multiplier >= 4;
      L.polyline(pts, {
        color: trafficColor(seg.multiplier, seg.closure),
        weight: closed ? 6 : 4,
        opacity: 0.9,
        dashArray: closed ? '8 6' : null,
        pane: 'traffic'
      }).addTo(trafficLayer);
    });
  }

  async function refreshTraffic() {
    try {
      var resp = await fetch('/api/v1/traffic/map');
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      drawTraffic(data.segments);
      $('traffic-ind').textContent = 'Traffic: aktif · ' + data.segments.length + ' segmen' +
        (trafficEnabledServer ? '' : ' (server nonaktif)');
    } catch (err) {
      $('traffic-ind').textContent = 'Traffic: error (' + (err.message || 'gagal') + ')';
      log('ERROR traffic: ' + (err.message || err));
    }
  }

  function clearTraffic() {
    if (trafficTimer) { clearInterval(trafficTimer); trafficTimer = null; }
    if (trafficLayer) { map.removeLayer(trafficLayer); trafficLayer = null; }
  }

  $('chk-traffic').addEventListener('change', function () {
    if (this.checked) {
      refreshTraffic();
      trafficTimer = setInterval(refreshTraffic, 30000);
    } else {
      clearTraffic();
      $('traffic-ind').textContent = trafficEnabledServer
        ? 'Traffic: server aktif (tampilan mati)'
        : 'Traffic: nonaktif';
    }
  });

  function renderRouteResult(data) {
    var best = (data.routes && data.routes.length) ? data.routes[0] : data;
    $('r-status').textContent = data.status || '-';
    $('r-dist').textContent = best.total_distance_meters != null ? (best.total_distance_meters.toLocaleString('id-ID') + ' m') : '-';
    $('r-points').textContent = best.route_coordinates ? decodePolyline(best.route_coordinates).length : '-';
    $('r-eta').textContent = best.estimated_time_seconds != null ? formatEta(best.estimated_time_seconds) : '-';
    $('r-radius').textContent = data.graph_radius_meters != null ? ((data.graph_radius_meters / 1000).toLocaleString('id-ID') + ' km') : '-';
    $('r-traffic').textContent = best.traffic_segments ? (best.traffic_segments.length + ' segmen' + (data.routes ? ' · ' + data.routes.length + ' opsi' : '')) : '-';
    $('r-warning').textContent = data.warning || '-';
    var src = data.source || '';
    if (src) {
      applySource(src);
    } else {
      $('r-source').textContent = '-';
    }
    if ((isPbf(src) || src === 'osm') && data.warning) {
      showNotice('Peringatan: ' + data.warning);
    }
  }

  $('btn-route').addEventListener('click', async function () {
    if (!origin || !dest) return;
    var btn = $('btn-route');
    btn.disabled = true;
    log('Mencari rute...');
    showLoading('Mencari rute... (memuat data peta, bisa butuh waktu)');
    var controller = new AbortController();
    var timeoutId = setTimeout(function () { controller.abort(); }, 300000);
    try {
      var resp = await fetch('/api/v1/pathfinding/find-route-options', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin: { latitude: origin[0], longitude: origin[1] },
          destination: { latitude: dest[0], longitude: dest[1] },
          mode: vehicleMode(),
          last_mile_precision: $('chk-lastmile').checked,
          dynamic_rerouting: $('chk-reroute').checked
        }),
        signal: controller.signal
      });
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      if (routeLayer) map.removeLayer(routeLayer);
      if (data.routes && data.routes.length) {
        drawRoutes(data);
      } else {
        drawRoute(data);
      }
      renderRouteResult(data);
      var best = (data.routes && data.routes.length) ? data.routes[0] : data;
      if (isPbf(data.source) || data.source === 'osm') {
        log('Rute OK: ' + best.total_distance_meters.toLocaleString('id-ID') + ' m, ' + (data.routes ? data.routes.length + ' opsi, ' : '') + decodePolyline(best.route_coordinates || '').length + ' titik (' + vehicleMode() + ', sumber=' + data.source + ')' + (data.warning ? ' - peringatan: ' + data.warning : ''));
      } else {
        log('Rute dihitung tanpa data peta (offline)');
      }
    } catch (err) {
      var friendly;
      if (err && err.name === 'AbortError') {
        friendly = 'Waktu habis (5 mnt). Area mungkin di luar cakupan peta yang dimuat — lakukan pre-warm di server dulu.';
      } else if (err && err.message === 'Failed to fetch') {
        friendly = 'Koneksi terputus ke server (tidak ada respons). Coba lagi, atau pastikan server berjalan.';
      } else {
        friendly = (err && err.message) ? err.message : String(err);
      }
      log('ERROR: ' + friendly);
      renderRouteResult({ status: 'error: ' + friendly });
    } finally {
      clearTimeout(timeoutId);
      hideLoading();
      btn.disabled = !(origin && dest);
    }
  });

  $('btn-geo').addEventListener('click', function () {
    if (!navigator.geolocation) { log('ERROR: geolocation tidak didukung browser ini.'); return; }
    log('Mengambil lokasi...');
    navigator.geolocation.getCurrentPosition(function (pos) {
      var ll = [pos.coords.latitude, pos.coords.longitude];
      placePoint('origin', L.latLng(ll[0], ll[1]));
      log('Lokasi: ' + fmt(ll) + ' (±' + Math.round(pos.coords.accuracy) + ' m)');
      if ($('chk-autoroute').checked && dest) $('btn-route').click();
    }, function (err) {
      log('ERROR lokasi (' + err.code + '): ' + err.message);
    }, { enableHighAccuracy: true, timeout: 15000 });
  });

  $('btn-track').addEventListener('click', function () {
    var btn = $('btn-track');
    if (watchId != null) {
      navigator.geolocation.clearWatch(watchId);
      watchId = null;
      if (liveMarker) { map.removeLayer(liveMarker); liveMarker = null; }
      if (trackLayer) { map.removeLayer(trackLayer); trackLayer = null; }
      btn.textContent = 'Mulai Lacak Posisi';
      btn.classList.remove('red'); btn.classList.add('green');
      log('Pelacakan dihentikan.');
      return;
    }
    if (!navigator.geolocation) { log('ERROR: geolocation tidak didukung browser ini.'); return; }
    var trail = [];
    btn.textContent = 'Berhenti Lacak';
    btn.classList.remove('green'); btn.classList.add('red');
    log('Mulai melacak posisi...');
    watchId = navigator.geolocation.watchPosition(function (pos) {
      var ll = L.latLng(pos.coords.latitude, pos.coords.longitude);
      trail.push(ll);
      if (!liveMarker) {
        liveMarker = L.marker(ll, { icon: icons.live }).addTo(map).bindTooltip('Posisi saya');
      } else {
        liveMarker.setLatLng(ll);
      }
      liveMarker.setTooltipContent('Posisi saya (±' + Math.round(pos.coords.accuracy) + ' m)');
      if (trackLayer) map.removeLayer(trackLayer);
      if (trail.length > 1) {
        trackLayer = L.polyline(trail, { color: '#1e7e34', weight: 3, dashArray: '6 6' }).addTo(map);
      }
      log('Lacak: ' + fmt([ll.lat, ll.lng]));
    }, function (err) {
      log('ERROR lacak (' + err.code + '): ' + err.message);
    }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 });
  });

  $('btn-reset').addEventListener('click', function () {
    if (watchId != null) { navigator.geolocation.clearWatch(watchId); watchId = null; $('btn-track').textContent = 'Mulai Lacak Posisi'; }
    hideLoading();
    clearTraffic();
    $('chk-traffic').checked = false;
    origin = null; dest = null;
    [originMarker, destMarker, liveMarker].forEach(function (m) { if (m) map.removeLayer(m); });
    originMarker = destMarker = liveMarker = null;
    [routeLayer, trackLayer].forEach(function (l) { if (l) map.removeLayer(l); });
    routeLayer = trackLayer = null;
    clearIncidents();
    clearRouteInfoPopups();
    routeOptions = null; routeLines = {};
    $('route-sheet').className = 'route-sheet';
    $('sheet-list').innerHTML = '';
    $('toasts').innerHTML = '';
    $('txt-origin').textContent = '-'; $('txt-dest').textContent = '-';
    $('btn-route').disabled = true;
    $('r-status').textContent = '-'; $('r-dist').textContent = '-'; $('r-points').textContent = '-'; $('r-radius').textContent = '-'; $('r-source').textContent = '-'; $('r-eta').textContent = '-'; $('r-traffic').textContent = '-'; $('r-warning').textContent = '-';
    var veh = document.querySelector('input[name="vehicle"][value="car"]');
    if (veh) veh.checked = true;
    $('chk-lastmile').checked = true;
    $('chk-reroute').checked = false;
    setBadge('b-source', 'sumber: ?', '');
    hideNotice();
    log('Peta direset.');
  });

  async function boot() {
    log('Memuat status server...');
    showLoading('Memeriksa status server...');
    try {
      var resp = await fetch('/health');
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      setBadge('b-server', 'Server: OK', 'ok');
      setBadge('b-osm', 'osmnx: ' + (data.osmnx_available ? 'tersedia' : 'tidak'), data.osmnx_available ? 'ok' : 'warn');
      setBadge('b-overpass', 'Overpass: ' + (data.overpass_reachable ? 'terjangkau' : 'terblokir/lambat'), data.overpass_reachable ? 'ok' : 'warn');
      setBadge('b-pbf', 'PBF: ' + (data.pbf_available ? 'aktif' : 'tidak ada'), data.pbf_available ? 'ok' : 'warn');
      if (data.osmnx_error) log('INFO osmnx: ' + data.osmnx_error);
      if (data.route_test && data.route_test.ok) {
        var info = sourceInfo(data.route_test.source);
        setBadge('b-source', 'sumber: ' + info.label, info.state);
        log('Tes rute OK: ' + data.route_test.total_distance_meters + ' m, ' + data.route_test.route_points + ' titik (' + info.label.toLowerCase() + ', area ' + Math.round(data.route_test.graph_radius_meters / 1000) + ' km). Klik peta lalu Cari Rute.');
        if ((isPbf(data.route_test.source) || data.route_test.source === 'osm') && data.route_test.warning) {
          showNotice('Peringatan: ' + data.route_test.warning);
        } else if (!isPbf(data.route_test.source) && data.route_test.source !== 'osm') {
          showNotice('Data peta tidak tersedia: PBF lokal tidak ada & API eksternal tidak terjangkau. Hasil bersifat terbatas (offline).');
        }
      } else {
        setBadge('b-server', 'Server: ERROR', 'bad');
        log('Tes rute gagal: ' + ((data.route_test && data.route_test.error) || 'response tidak valid'));
      }
    } catch (err) {
      setBadge('b-server', 'Server: ERROR', 'bad');
      log('ERROR server: ' + err.message);
    } finally {
      hideLoading();
    }
    checkTrafficStatus();
  }

  async function checkTrafficStatus() {
    try {
      var resp = await fetch('/api/v1/traffic/status');
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      trafficEnabledServer = !!data.enabled;
      var ind = $('traffic-ind');
      if (data.enabled) {
        ind.textContent = 'Traffic: server aktif · ' + data.penalty_count + ' segmen';
      } else {
        ind.textContent = 'Traffic: server nonaktif';
      }
      if (data.poller_running) log('INFO traffic: poller real-time berjalan.');
    } catch (err) {
      $('traffic-ind').textContent = 'Traffic: server tak terjangkau';
    }
  }

  placePoint('origin', L.latLng(-6.8048, 110.8385));
  placePoint('dest', L.latLng(-6.8100, 110.8500));
  boot();
})();

(function () {
  "use strict";

  var map = L.map('map').setView([-6.1754, 106.8272], 14);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var icons = {
    origin: L.divIcon({ html: '<div style="background:#1e7e34;border:2px solid #fff;border-radius:50%;width:16px;height:16px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>', className: '', iconSize: [16, 16] }),
    dest:   L.divIcon({ html: '<div style="background:#b3372f;border:2px solid #fff;border-radius:50%;width:16px;height:16px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>', className: '', iconSize: [16, 16] }),
    live:   L.divIcon({ html: '<div style="background:#0f6dc1;border:2px solid #fff;border-radius:50%;width:14px;height:14px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>', className: '', iconSize: [14, 14] })
  };

  var origin = null, dest = null;
  var originMarker = null, destMarker = null;
  var routeLayer = null, trackLayer = null, liveMarker = null;
  var watchId = null;

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

  map.on('click', function (e) {
    placePoint(mode(), e.latlng);
  });

  function drawRoute(payload) {
    routeLayer = L.polyline(payload.route_coordinates.map(function (p) { return [p[0], p[1]]; }), {
      color: '#0f6dc1', weight: 5, opacity: 0.85
    }).addTo(map);
    map.fitBounds(routeLayer.getBounds(), { padding: [30, 30] });
  }

  function renderRouteResult(data) {
    $('r-status').textContent = data.status || '-';
    $('r-dist').textContent = data.total_distance_meters != null ? (data.total_distance_meters.toLocaleString('id-ID') + ' m') : '-';
    $('r-points').textContent = data.route_coordinates ? data.route_coordinates.length : '-';
    $('r-radius').textContent = data.graph_radius_meters != null ? ((data.graph_radius_meters / 1000).toLocaleString('id-ID') + ' km') : '-';
    $('r-source').textContent = data.source || '-';
    setBadge('b-source', 'source: ' + (data.source || '-'), data.source === 'osm' ? 'ok' : 'warn');
    if (data.warning) {
      showNotice('Peringatan: ' + data.warning);
    } else {
      hideNotice();
    }
  }

  $('btn-route').addEventListener('click', async function () {
    if (!origin || !dest) return;
    var btn = $('btn-route');
    btn.disabled = true;
    log('Mencari rute...');
    try {
      var resp = await fetch('/api/v1/pathfinding/find-route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin: { latitude: origin[0], longitude: origin[1] },
          destination: { latitude: dest[0], longitude: dest[1] }
        })
      });
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      if (routeLayer) map.removeLayer(routeLayer);
      drawRoute(data);
      renderRouteResult(data);
      log('Rute OK: ' + data.total_distance_meters.toLocaleString('id-ID') + ' m, ' + data.route_coordinates.length + ' titik (source=' + data.source + ')' + (data.warning ? ' [demo]' : ''));
    } catch (err) {
      log('ERROR: ' + err.message);
      renderRouteResult({ status: 'error: ' + err.message });
    } finally {
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
    origin = null; dest = null;
    [originMarker, destMarker, liveMarker].forEach(function (m) { if (m) map.removeLayer(m); });
    originMarker = destMarker = liveMarker = null;
    [routeLayer, trackLayer].forEach(function (l) { if (l) map.removeLayer(l); });
    routeLayer = trackLayer = null;
    $('txt-origin').textContent = '-'; $('txt-dest').textContent = '-';
    $('btn-route').disabled = true;
    $('r-status').textContent = '-'; $('r-dist').textContent = '-'; $('r-points').textContent = '-'; $('r-radius').textContent = '-'; $('r-source').textContent = '-';
    log('Peta direset.');
  });

  async function boot() {
    log('Memuat status server...');
    try {
      var resp = await fetch('/health');
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      setBadge('b-server', 'Server: OK', 'ok');
      setBadge('b-osm', 'osmnx: ' + (data.osmnx_available ? 'tersedia' : 'tidak'), data.osmnx_available ? 'ok' : 'warn');
      setBadge('b-overpass', 'Overpass: ' + (data.overpass_reachable ? 'terjangkau' : 'terblokir/lambat'), data.overpass_reachable ? 'ok' : 'warn');
      if (data.osmnx_error) log('INFO osmnx: ' + data.osmnx_error);
      if (data.route_test && data.route_test.ok) {
        setBadge('b-source', 'source: ' + data.route_test.source, data.route_test.source === 'osm' ? 'ok' : 'warn');
        log('Tes rute OK: ' + data.route_test.total_distance_meters + ' m, ' + data.route_test.route_points + ' titik (source=' + data.route_test.source + ', area ' + Math.round(data.route_test.graph_radius_meters / 1000) + ' km). Klik peta lalu Cari Rute.');
        if (data.route_test.warning) showNotice('Peringatan: ' + data.route_test.warning);
      } else {
        setBadge('b-server', 'Server: ERROR', 'bad');
        log('Tes rute gagal: ' + ((data.route_test && data.route_test.error) || 'response tidak valid'));
      }
    } catch (err) {
      setBadge('b-server', 'Server: ERROR', 'bad');
      log('ERROR server: ' + err.message);
    }
  }

  placePoint('origin', L.latLng(-6.1754, 106.8272));
  placePoint('dest', L.latLng(-6.1830, 106.8360));
  boot();
})();

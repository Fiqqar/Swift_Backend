(function () {
  "use strict";

  var map = L.map('map').setView([-6.8048, 110.8385], 13);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var routePane = map.createPane('route');
  routePane.style.zIndex = 460;

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

  function setBadge(id, label, state) {
    var el = $(id);
    el.textContent = label;
    el.className = 'badge ' + (state || '');
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

  function placeHub(latlng) {
    hub = [latlng.lat, latlng.lng];
    curPos = hub;
    if (hubMarker) map.removeLayer(hubMarker);
    hubMarker = L.marker(latlng, {
      icon: numberedIcon('H', iconColors.hub)
    }).addTo(map).bindTooltip('Hub').openTooltip();
    if (posMarker) map.removeLayer(posMarker);
    posMarker = L.marker(latlng, {
      icon: L.divIcon({
        html: '<div style="background:#0f6dc1;border:2px solid #fff;border-radius:50%;width:14px;height:14px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>',
        className: '', iconSize: [14, 14], iconAnchor: [7, 7]
      })
    }).addTo(map).bindTooltip('Posisi kurir');
    $('txt-hub').textContent = fmt(hub);
    $('txt-pos').textContent = fmt(curPos);
    $('btn-optimize').disabled = !(hub && parseDeliveries().length > 0);
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
      if (alamat) out.push({ alamat: alamat, service_type: svc });
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

  function drawOverview(data) {
    clearMapLayers();
    var group = L.layerGroup().addTo(map);
    var allBounds = null;

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
      return;
    }
    var s = data.stops[activeIndex];
    $('nav-status').textContent = 'Stop ' + (activeIndex + 1) + ' / ' + data.stops.length;
    card.innerHTML = '<b>' + (s.recipient_name || ('Paket ' + (s.package_id || s.stop_order))) + '</b>'
      + '<div>' + (s.service_type || '') + ' · '
      + (leg.distance_km != null ? leg.distance_km + ' km' : '-') + ' · '
      + (leg.duration_mins != null ? leg.duration_mins + ' mnt' : '-') + '</div>'
      + '<div class="coords">' + s.latitude.toFixed(5) + ', ' + s.longitude.toFixed(5) + '</div>';
    $('btn-deliver').disabled = false;
    $('btn-recalc').disabled = !(deviating && curPos);
  }

  function buildPayload(remaining) {
    return {
      hub_origin: { latitude: curPos[0], longitude: curPos[1] },
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
      mode: 'motorcycle',
      last_mile_precision: true,
      skip_traffic: $('chk-skip-traffic').checked,
      return_to_hub: false
    };
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

  function renderNav(data) {
    $('nav-controls').style.display = 'block';
    var card = $('nav-card');
    var leg = data.legs[activeIndex];
    if (activeIndex >= data.stops.length) {
      $('nav-status').textContent = 'Semua paket terkirim ✅';
      card.textContent = 'Rute selesai.';
      $('btn-deliver').disabled = true;
      $('btn-recalc').disabled = true;
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
    checkGeofence(s);
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
    if (!hub) return;
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
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          hub_origin: { latitude: hub[0], longitude: hub[1] },
          deliveries: deliveries.map(function (d) {
            return { alamat: d.alamat, service_type: d.service_type, recipient_name: d.alamat.split(',')[0] };
          }),
          mode: 'motorcycle',
          last_mile_precision: true,
          skip_traffic: $('chk-skip-traffic').checked,
          return_to_hub: $('chk-return').checked
        }),
        signal: controller.signal
      });
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      state = normalizeData(data, false);
      activeIndex = 0;
      deviating = false;
      renderResult(data);
      drawOverview(state);
      log('Rute OK: ' + data.total_distance_km + ' km, ' + data.total_duration_mins +
          ' mnt, ' + data.total_legs + ' leg, ' + data.stops.length + ' stop (sumber=' + data.source + ')');
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
      btn.disabled = !(hub && deliveries.length > 0);
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
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildPayload(remain)),
        signal: AbortSignal.timeout(600000)
      });
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      state = normalizeData(data, true);
      activeIndex = 0;
      deviating = false;
      renderResult({ status: 'success', total_distance_km: data.total_distance_km,
                     total_duration_mins: data.total_duration_mins,
                     total_legs: data.total_legs, source: data.source, warning: data.warning });
      drawOverview(state);
      log('Sisa rute dioptimasi ulang: ' + data.total_distance_km + ' km, ' +
          data.total_duration_mins + ' mnt, ' + data.stops.length + ' stop.');
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
    var src = data.source || '';
    if (src) setBadge('b-source', 'sumber: ' + src, 'ok');
    if (src && data.warning) showNotice('Peringatan: ' + data.warning);
  }

  map.on('click', function (e) {
    var mode = document.querySelector('input[name="mode"]:checked').value;
    if (mode === 'hub') {
      placeHub(e.latlng);
    } else {
      deviating = true;
      curPos = [e.latlng.lat, e.latlng.lng];
      if (posMarker) map.removeLayer(posMarker);
      posMarker = L.marker(e.latlng, {
        icon: L.divIcon({
          html: '<div style="background:#b3372f;border:2px solid #fff;border-radius:50%;width:16px;height:16px;box-shadow:0 1px 4px rgba(0,0,0,.5);"></div>',
          className: '', iconSize: [16, 16], iconAnchor: [8, 8]
        })
      }).addTo(map).bindTooltip('Posisi kurir (deviasi)').openTooltip();
      $('txt-pos').textContent = fmt(curPos);
      log('Deviasi: kurir kini di ' + fmt(curPos));
      if (state && activeIndex < state.stops.length) checkGeofence(state.stops[activeIndex]);
      if (state) $('btn-recalc').disabled = false;
    }
  });

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
    }
    drawOverview(state);
  });

  $('txt-deliveries').addEventListener('input', function () {
    $('btn-optimize').disabled = !(hub && parseDeliveries().length > 0);
  });

  $('btn-reset').addEventListener('click', function () {
    hideLoading();
    hub = curPos = null; deviating = false; activeIndex = 0; state = null;
    geoOk = false; geoSeq++;
    [hubMarker, posMarker].forEach(function (m) { if (m) map.removeLayer(m); });
    hubMarker = posMarker = null;
    clearMapLayers();
    $('nav-controls').style.display = 'none';
    $('txt-hub').textContent = '-'; $('txt-pos').textContent = '-';
    $('btn-optimize').disabled = true;
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

  async function boot() {
    log('Memuat status server...');
    showLoading('Memeriksa status server...');
    try {
      var resp = await fetch('/health');
      var data = await parseJson(resp);
      if (!resp.ok) throw new Error(data.detail || data.error || ('HTTP ' + resp.status));
      setBadge('b-server', 'Server: OK', 'ok');
      log('Server OK. Klik peta untuk menetapkan Hub, isi alamat penerima, lalu Optimasi.');
    } catch (err) {
      setBadge('b-server', 'Server: ERROR', 'bad');
      log('ERROR server: ' + err.message);
    } finally {
      hideLoading();
    }
  }

  boot();
})();

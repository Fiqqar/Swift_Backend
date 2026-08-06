import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.v1.router import api_router

app = FastAPI(title="TEST2 API Engine", version="1.0.0")

app.include_router(api_router, prefix="/api/v1")

_DEMO_ORIGIN = (-6.1754, 106.8272)  # Monas, Jakarta
_DEMO_DESTINATION = (-6.1830, 106.8360)


@app.get("/health")
def health():
    try:
        return _build_health()
    except Exception as exc:
        return JSONResponse({
            "status": "error",
            "app": app.title,
            "version": app.version,
            "error": f"{type(exc).__name__}: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, status_code=500)


def _build_health():
    osmnx_available = False
    osmnx_error = None
    try:
        import osmnx  # noqa: F401
        osmnx_available = True
    except Exception as exc:
        osmnx_error = f"{type(exc).__name__}: {exc}"

    overpass_url = os.environ.get("OSMNX_OVERPASS_URL", "https://overpass-api.de/api").rstrip("/")
    overpass_reachable = False
    try:
        import httpx
        response = httpx.get(
            f"{overpass_url}/status",
            timeout=8,
            headers={"User-Agent": "Test2HealthCheck/1.0 (pathfinding)"},
        )
        overpass_reachable = response.status_code == 200
    except Exception:
        overpass_reachable = False

    route_test = None
    try:
        from app.services.pathfinding.core_a_star import run_a_star
        from app.services.pathfinding.graph_loader import (
            load_osm_graph_by_point,
            find_nearest_node,
            auto_radius,
        )

        dist_meters = auto_radius(
            _DEMO_ORIGIN[0], _DEMO_ORIGIN[1],
            _DEMO_DESTINATION[0], _DEMO_DESTINATION[1],
        )
        graph, locations, source, warning = load_osm_graph_by_point(
            lat=_DEMO_ORIGIN[0], lon=_DEMO_ORIGIN[1], dist_meters=dist_meters
        )
        start = find_nearest_node(_DEMO_ORIGIN[0], _DEMO_ORIGIN[1], locations)
        goal = find_nearest_node(_DEMO_DESTINATION[0], _DEMO_DESTINATION[1], locations)
        if start is None or goal is None:
            raise ValueError("Titik terdekat tidak ditemukan")
        node_path, total_distance = run_a_star(graph, locations, start, goal)
        route_test = {
            "ok": node_path is not None,
            "total_distance_meters": round(total_distance, 2) if node_path else None,
            "route_points": len(node_path) if node_path else 0,
            "source": source,
            "warning": warning,
            "graph_radius_meters": dist_meters,
        }
    except Exception as exc:
        route_test = {"ok": False, "error": str(exc)}

    return JSONResponse({
        "status": "ok",
        "app": app.title,
        "version": app.version,
        "osmnx_available": osmnx_available,
        "osmnx_error": osmnx_error,
        "overpass_reachable": overpass_reachable,
        "route_test": route_test,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse(_HOME_PAGE)


_HOME_PAGE = """
<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cek Sistem - TEST2 Pathfinding</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: #f4f6f8; color: #1f2933; }
  header { background: #0f2a43; color: #fff; padding: 14px 20px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; }
  .badges { display: flex; gap: 8px; flex-wrap: wrap; }
  .badge { padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #334e6b; color: #d7e3f1; }
  .badge.ok { background: #1e7e34; color: #eafff0; }
  .badge.bad { background: #b3372f; color: #ffeaea; }
  .badge.warn { background: #b7791f; color: #fff7e0; }
  #notice { display: none; margin: 10px 14px 0; padding: 10px 12px; border-radius: 8px; background: #fff4d6; border: 1px solid #e0b84e; color: #6b4d00; font-size: 13px; }
  #notice.show { display: block; }
  main { display: flex; gap: 0; height: calc(100vh - 56px); }
  #map { flex: 1; height: 100%; min-width: 0; }
  #panel { width: 340px; max-width: 40%; overflow-y: auto; background: #fff; padding: 16px; border-left: 1px solid #d7dde3; }
  section { margin-bottom: 18px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: #5b6b7b; margin: 0 0 8px; }
  .mode-row { display: flex; gap: 8px; margin-bottom: 10px; }
  .mode-row label { flex: 1; text-align: center; padding: 8px 4px; border: 1.5px solid #c9d2da; border-radius: 8px; cursor: pointer; font-size: 13px; }
  .mode-row input { display: none; }
  .mode-row input:checked + span { color: #0f2a43; font-weight: 700; }
  .mode-row label:has(input:checked) { border-color: #0f2a43; background: #eaf1f8; }
  .btn { width: 100%; margin-top: 6px; padding: 10px; border: 0; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .btn.primary { background: #0f6dc1; color: #fff; }
  .btn.primary:hover { background: #0b5aa3; }
  .btn.green { background: #1e7e34; color: #fff; }
  .btn.red { background: #b3372f; color: #fff; }
  .btn.ghost { background: #e7ebef; color: #1f2933; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .coords { font-size: 12px; color: #5b6b7b; font-family: ui-monospace, Consolas, monospace; }
  .row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed #e3e8ed; font-size: 13px; }
  .row b { text-align: right; font-weight: 600; }
  #result { min-height: 60px; }
  #log { min-height: 40px; font-family: ui-monospace, Consolas, monospace; font-size: 12px; background: #0d1b26; color: #7ff59b; padding: 10px; border-radius: 8px; white-space: pre-wrap; word-break: break-word; }
  .leaflet-container { font: inherit; }
</style>
</head>
<body>
<header>
  <h1>TES2 API Engine — Cek Sistem &amp; Pathfinding</h1>
  <div class="badges">
    <span class="badge" id="b-server">Server: ?</span>
    <span class="badge" id="b-osm">osmnx: ?</span>
    <span class="badge" id="b-overpass">Overpass: ?</span>
    <span class="badge" id="b-source">source: ?</span>
  </div>
</header>
<div id="notice"></div>
<main>
  <div id="map"></div>
  <div id="panel">
    <section>
      <h2>Atur Titik</h2>
      <div class="mode-row">
        <label><input type="radio" name="mode" value="origin" checked><span>Klik = Titik Awal</span></label>
        <label><input type="radio" name="mode" value="dest"><span>Klik = Tujuan</span></label>
      </div>
      <div class="row"><span>Origin</span><b id="txt-origin">-</b></div>
      <div class="row"><span>Destination</span><b id="txt-dest">-</b></div>
      <button class="btn primary" id="btn-route" disabled>Cari Rute</button>
    </section>

    <section>
      <h2>Geolokasi Real-time</h2>
      <label style="font-size:12px; display:block; margin-bottom:6px;">
        <input type="checkbox" id="chk-autoroute"> Auto cari rute setelah ambil lokasi
      </label>
      <button class="btn green" id="btn-geo">Gunakan Lokasi Saya</button>
      <button class="btn red" id="btn-track">Mulai Lacak Posisi</button>
    </section>

    <section>
      <h2>Hasil Rute</h2>
      <div id="result">
        <div class="row"><span>Status</span><b id="r-status">-</b></div>
        <div class="row"><span>Jarak total</span><b id="r-dist">-</b></div>
        <div class="row"><span>Titik rute</span><b id="r-points">-</b></div>
        <div class="row"><span>Area peta</span><b id="r-radius">-</b></div>
        <div class="row"><span>Sumber data</span><b id="r-source">-</b></div>
      </div>
      <button class="btn ghost" id="btn-reset" style="margin-top:8px;">Reset Peta</button>
    </section>

    <section>
      <h2>Log</h2>
      <div id="log">Siap. Klik peta atau jalankan tes...</div>
    </section>
  </div>
</main>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
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
</script>
</body>
</html>
"""

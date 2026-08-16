Tolong buatkan modul backend dan handler WebSocket untuk Real-time Navigation & Auto-Rerouting pada project FastAPI/Python saya.

**Spesifikasi Detail:**
1. **Service (`app/services/navigation.py`):**
   - Buat `NavSession` (dataclass) untuk menyimpan state navigasi kurir (`kurir_id`, `ws`, `route_id`, `leg_index`, `coords`, status `off_route`, `cooldown_until`, `lock`, dll).
   - Buat `NavRegistry` untuk mengelola dict `kurir_id` -> `NavSession` dengan `asyncio.Lock`.
   - Implementasikan `point_to_polyline_distance_m(lat, lon, coords)` dengan proyeksi planar `cos(lat)` untuk menghitung jarak presisi posisi GPS ke segmen polyline rute.
   - Implementasikan `remaining_progress(session, lat, lon)` untuk menghitung sisa jarak (meter), sisa ETA (detik), dan progress %.
   - Buat `navigation_worker(app)` background loop yang mengevaluasi session tiap 30 detik. Jika ada penghematan ETA >= 120 detik (`TRAFFIC_REROUTE_MIN_SAVING_SECONDS`) dan cooldown 30 detik (`REROUTE_COOLDOWN_SECONDS`) sudah lewat, panggil `compute_reroute()` dan pemicu auto-reroute.
2. **Endpoint WebSocket (`app/api/v1/endpoints/navigation.py`):**
   - Pasang endpoint WS `/api/v1/ws/navigation` dengan validasi Auth JWT token (4401 jika tanpa token).
   - Handling event masuk: `start_navigation` (baca snapshot rute dari Redis), `location_update` (rate limit, update posisi, push progress, cek off-route > 40m via `OFF_ROUTE_THRESHOLD_M`, trigger reroute instan jika off-route), dan `ping`.
   - Event keluar: `route_progress`, `off_route_warning`, `reroute_available`, `auto_rerouted`, `ack`, `error`. Semua koordinat wajib dikirim dalam bentuk Encoded Polyline (Precision 5) agar payload < 2 KB.
Tolong buatkan modul backend dan handler WebSocket untuk **Real-time Navigation & Auto-Rerouting berbasis AI Agent (Google Gemini)** pada project FastAPI/Python saya.

**Spesifikasi Detail:**

1. **Service (`app/services/navigation.py`):**
   - `NavSession` (state navigasi kurir: `kurir_id`, `ws`, `route_id`, `leg_index`, `coords`, `off_route`, `cooldown_until`, `lock`, dll).
   - `NavRegistry` (dict `kurir_id -> NavSession` dengan `asyncio.Lock`).
   - Helper geometri deterministik yang TETAP dipakai (sebagai tool & validasi, bukan pembuat keputusan): `point_to_polyline_distance_m(lat, lon, coords)` (proyeksi planar `cos(lat)`) dan `remaining_progress(session, lat, lon)` (sisa jarak/ETA/progress %).
   - `navigation_worker(app)` background loop tiap 30 detik mengevaluasi session aktif: keputusan auto-reroute traffic diambil lewat **AI Agent** (`decide_reroute(..., hint="traffic")`).

2. **AI Agent (`app/services/ai_agent.py`) — [BARU]:**
   - Memakai **Google Gemini** (`google-genai`) dengan **Function Calling**; env `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash`), `AI_REROUTE_ENABLED` (default `False`), `GEMINI_REROUTE_TIMEOUT_S` (default `2.0` detik agar responsif di WebSocket real-time).
   - Fungsi `decide_reroute(context, decision_hint, *, app, redis, session)` dijalankan via `run_in_threadpool` + `asyncio.wait_for`; mengembalikan `{"action": "apply"|"ignore"|"defer", "reason": "..."}` atau **`None`** sebagai sinyal *fallback*.
   - Tool Calling membungkus pipeline eksisting: `get_remaining_progress`, `compute_reroute`, `get_traffic_penalties`, `get_route_options`, `apply_reroute`.
   - **Hardening operasional**:
     - **Semaphore konkuransi** (`GEMINI_MAX_CONCURRENT`, default 3): batasi panggilan LLM paralel global; antrean yang melampaui `GEMINI_REROUTE_TIMEOUT_S` → fallback deterministik.
     - **Circuit breaker** (`GEMINI_BREAKER_THRESHOLD` default 3, `GEMINI_BREAKER_RESET_S` default 30): setelah N kegagalan beruntun (error/timeout/kedua key 429), AI di-skip sementara, lalu coba lagi setelah jendela reset; keberhasilan mereset penghitung.
     - **Sanitasi `reason`**: strip karakter kontrol, batasi 500 karakter, aman masuk payload WS/log.
   - **Graceful fallback**: bila `AI_REROUTE_ENABLED=False`, tanpa `GEMINI_API_KEY`, circuit breaker terbuka, error, atau timeout → kembali ke logika threshold deterministik (`TRAFFIC_REROUTE_MIN_SAVING_SECONDS`, `OFF_ROUTE_THRESHOLD_M`) tanpa mengganggu alur WebSocket.

3. **Endpoint WebSocket (`app/api/v1/endpoints/navigation.py`):**
   - WS `/api/v1/ws/navigation`, auth JWT (kode 4401 tanpa token).
   - Pesan masuk: `start_navigation` (baca snapshot rute dari Redis `driver:nav:{kurir_id}`), `location_update` (rate limit, update posisi, push progress, **deteksi off-route → keputusan AI** `decide_reroute(..., hint="off_route")`), `ping`.
   - Keputusan AI pada `location_update`: `apply` → warning + auto-reroute; `ignore` → dianggap GPS noise, peringatan off-route diabaikan; `defer` → tunda reroute; error/disabled → perilaku deterministik lama.
   - Pesan keluar: `ack`, `error`, `route_progress`, `off_route_warning`, `reroute_available`, `auto_rerouted` (dengan field `reason` natural-language dari AI). Semua geometri memakai `encode_polyline(..., 5)`.
   - Guard anti-flicker & anti-spam tetap dipertahankan: `REROUTE_COOLDOWN_SECONDS`, `off_route_active`, rate-limit posisi.

4. **Konfigurasi lingkungan (.env):**
   - `AI_REROUTE_ENABLED` (0/1), `GEMINI_API_KEY`, `GEMINI_API_KEY_2` (opsional, cadangan rate-limit HTTP 429), `GEMINI_MODEL`, `GEMINI_REROUTE_TIMEOUT_S`, `GEMINI_MAX_CONCURRENT`, `GEMINI_BREAKER_THRESHOLD`, `GEMINI_BREAKER_RESET_S`.
   - Saat key primer kena rate-limit (429), server otomatis memakai key cadangan `GEMINI_API_KEY_2`; bila keduanya gagal → fallback deterministik.
   - Status health AI (enabled, backup key, circuit breaker, slot konkuransi, timeout) tersedia via `GET /api/v1/ws/navigation/status` (field `ai`).

5. **Pengujian (`tests/test_ai_agent.py`):**
   - Verifikasi skenario `apply`, `ignore`, dan *graceful fallback* saat timeout/error/disabled (`decide_reroute` mengembalikan `None`).
   - Tambahan: cadangan key saat rate-limit, semaphore dilepas setelah panggilan, circuit breaker buka/tutup, sanitasi `reason`, dan blok `ai` pada status endpoint.
Tolong tambahkan modul optimasi pathfinding dan integrasi snapshot Redis pada sistem pathfinding eksisting (`app/api/v1/endpoints/pathfinding.py` & `app/services/tracking.py`).

**Spesifikasi Detail:**
1. **Redis Session Snapshot (`app/services/tracking.py`):**
   - Buat fungsi helper `set_nav_route(redis, kurir_id, nav: dict)`, `get_nav_route()`, dan `clear_nav_route()` dengan format key `driver:nav:{kurir_id}` dan TTL 3600 detik (`KURIR_NAV_TTL_SECONDS`).
2. **Pathfinding Endpoint Update (`app/api/v1/endpoints/pathfinding.py`):**
   - Pada `find_route`: Generate unique `route_id` (counter/UUID), simpan snapshot rute (legs, encoded polyline, total ETA/distance) ke Redis, dan sertakan `route_id` di response.
   - Pada `find_optimized_delivery_route` (Multi-stop TSP/VRP Solver): Urutkan urutan stop paling efisien, simpan snapshot multi-leg ke Redis, dan kembalikan `route_id`.
   - Tambahkan opsi pencarian 1–3 rute alternatif (Yen's K-Shortest Path) dengan tumpang-tindih rute maksimal 70%.
3. **Pydantic Schema Update (`app/schemas/pathfinding.py`):**
   - Tambahkan field opsional `route_id: int | None = None` pada `RouteResponse` dan `OptimizedDeliveryRouteResponse` (pastikan kompatibel dengan cache lama via `RouteResponse(**cached)`).
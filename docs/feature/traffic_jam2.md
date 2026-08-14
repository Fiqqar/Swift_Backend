Berikut adalah prompt modular yang sudah disesuaikan dengan Strategi Smart Hybrid Origin–Destination yang kamu usulkan. Prompt ini siap kamu salin dan berikan langsung ke AI Agent/Developer:

Markdown

# Task: Smart Hybrid Real-time Traffic Integration (TomTom O-D + Internal Report)

## Configuration

- Toggle Variable: `ENABLE_REALTIME_TRAFFIC = True | False`
- Provider Mode: `TRAFFIC_PROVIDER_MODE = "smart_hybrid"` # Options: "smart_hybrid", "internal_only", "full_tomtom"
- TomTom Radius Parameter: `TOMTOM_PROBE_RADIUS_KM = 5.0` (Default: 5 km dari Origin & Destination)
- Cache TTL Parameter: `TRAFFIC_REDIS_TTL_SECONDS = 300` (Default: 5 menit)

## Requirements

1. Jika `ENABLE_REALTIME_TRAFFIC = True` dan `TRAFFIC_PROVIDER_MODE = "smart_hybrid"`:
   - **Origin & Destination Probe (TomTom API):**
     - Saat `find_route` dipanggil, ambil koordinat Origin dan Destination.
     - Cek Redis Cache terlebih dahulu untuk koordinat Origin & Destination (gunakan pembulatan geohash/grid 500m).
     - Jika cache miss, panggil TomTom Flow Segment API **HANYA** untuk 2 area:
       1. Max 1-2 probe point di jalan arteri/persimpangan utama dalam radius `TOMTOM_PROBE_RADIUS_KM` dari **Origin**.
       2. Max 1-2 probe point di jalan arteri/persimpangan utama dalam radius `TOMTOM_PROBE_RADIUS_KM` dari **Destination**.
     - Simpan hasil respons TomTom ke Redis dengan TTL `TRAFFIC_REDIS_TTL_SECONDS`.
   - **Mid-Route Segment (Internal Incident Service):**
     - Untuk ruas jalan di luar radius Origin/Destination (segmen tengah rute), **JANGAN** panggil TomTom API.
     - Gunakan data dari `Internal Incident Service` (laporan penutupan jalan/banjir/kemacetan dari admin atau telemetry data armada internal).

   - **Weight Adjustments in Routing Engine:**
     - Terapkan _traffic multiplier_ pada edge graf yang terpengaruh data TomTom (O/D) maupun Internal Service (Mid-route).
     - Formula multiplier: `ratio = freeFlowSpeed / max(currentSpeed, 1)`, clamp `[1, 10]`.
     - Jika `roadClosure = True` atau ada laporan internal jalan ditutup, set weight = `INFINITY`.

2. Jika `ENABLE_REALTIME_TRAFFIC = False`:
   - Gunakan bobot Haversine distance murni tanpa pengecekan API TomTom maupun Redis Incident Cache

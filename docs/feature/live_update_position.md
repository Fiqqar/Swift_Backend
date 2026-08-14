# Task: Real-time Driver GPS Position Ingestion

## Configuration
- Toggle Variable: `ENABLE_LIVE_TRACKING` (Default: `False`)

## Requirements
1. Jika `ENABLE_LIVE_TRACKING = True`:
   - Sediakan endpoint WebSocket atau HTTP POST `/driver/position` untuk menerima koordinat terbaru driver `(lat, lon, bearing, speed)`.
   - Lakukan snapping posisi koordinat mentah driver ke edge jalan terdekat (*map matching*) untuk memperbarui lokasi driver pada map UI secara mulus.
2. Jika `ENABLE_LIVE_TRACKING = False`:
   - Abaikan ingestion data lokasi dinamis driver.
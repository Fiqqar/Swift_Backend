# Task: Auto Dynamic Rerouting on Deviation

## Configuration
- Toggle Variable: `ENABLE_DYNAMIC_REROUTING` (Default: `False`)
- Threshold Parameter: `MAX_OFF_ROUTE_DISTANCE_METERS` (Default: `30`)

## Requirements
1. Jika `ENABLE_DYNAMIC_REROUTING = True`:
   - Bandingkan posisi real-time driver dengan jalur rute aktif.
   - Jika jarak tegak lurus driver dari jalur rute melebihi `MAX_OFF_ROUTE_DISTANCE_METERS`, hitung ulang rute baru dari lokasi driver saat ini ke destinasi akhir secara otomatis dalam `< 100ms`.
2. Jika `ENABLE_DYNAMIC_REROUTING = False`:
   - Jangan kalkulasi ulang rute secara otomatis meskipun driver keluar jalur (hanya kalkulasi ulang jika dipicu manual oleh user).
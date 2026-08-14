# Task: Vehicle Transport Mode Filtering

## Configuration
- Toggle Parameter: `VEHICLE_MODE` (Default: `"car"`)

## Requirements
1. Filter kriteria akses jalan pada `graph_loader.py` dan Rust engine berdasarkan opsi `VEHICLE_MODE`:
   - **`motorcycle`:** Sertakan jalan sekunder/pemukiman (`residential`), blokir akses jalan tol (`motorway`).
   - **`car`:** Izinkan akses jalan tol dan jalan utama, blokir jalan khusus pejalan kaki/gang sangat sempit.
   - **`truck`:** Batasi HANYA pada jalan nasional, arteri, dan jalan tol (`motorway`, `trunk`, `primary`). Blokir area pemukiman (`residential`).
2. Kirim parameter ini pada request API `/route?mode={VEHICLE_MODE}`.
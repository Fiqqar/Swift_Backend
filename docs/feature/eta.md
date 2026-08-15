# Task: Advanced ETA & Delivery Overhead Calculation

## Configuration
- Toggle Variable: `ENABLE_CUSTOM_ETA` (Default: `False`)
- Overhead Parameter: `SERVICE_TIME_MINUTES` (Default: `3`)

## Requirements
1. Jika `ENABLE_CUSTOM_ETA = True`:
   - Hitung total ETA dengan formula: `Total ETA = (Jarak / Kecepatan Rata-rata Moda) + SERVICE_TIME_MINUTES`.
   - Tambahkan faktor penalti waktu untuk persimpangan, lampu merah, dan belokan tajam.
2. Jika `ENABLE_CUSTOM_ETA = False`:
   - Gunakan kalkulasi waktu murni (*pure travel time*) berdasarkan jarak dipagi kecepatan standar jalan.
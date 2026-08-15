# Task: High-Precision Last-Mile Endpoint Snapping

## Configuration
- Toggle Variable: `ENABLE_LAST_MILE_PRECISION` (Default: `True`)

## Requirements
1. Jika `ENABLE_LAST_MILE_PRECISION = True`:
   - Gunakan helper proyeksi segmen tegak lurus (`_snap_endpoint`) untuk mengikat titik origin dan destination langsung ke pinggir segmen jalan lokal terdekat, bukan sekadar persimpangan/node terdekat.
   - Sesuaikan jarak total rute dengan menambahkan selisih jarak proyeksi dari titik target ke jalan.
2. Jika `ENABLE_LAST_MILE_PRECISION = False`:
   - Lakukan snap standar langsung ke ID node graf terdekat (*closest node*) tanpa koreksi proyeksi garis jalan.
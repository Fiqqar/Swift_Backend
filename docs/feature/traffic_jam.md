# Task: Real-time Traffic Jam Integration

## Configuration

- Toggle Variable: `ENABLE_REALTIME_TRAFFIC` (Default: `False`)

## Requirements

1. Jika `ENABLE_REALTIME_TRAFFIC = True`:
   - Integrasikan data arus lalu lintas (TomTom / HERE / Internal Incident Service).
   - Terapkan penalti bobot (_weight multiplier_) pada edge/ruas jalan yang terdeteksi macet pada engine routing di Rust/Python.
   - Apabila terdapat laporan penutupan jalan total, beri bobot tak terhingga (infinity) agar jalan dihindari.
2. Jika `ENABLE_REALTIME_TRAFFIC = False`:
   - Gunakan bobot standar (_default speed limit_ dari OSM tags) tanpa pemrosesan data traffic eksternal.

# Task: Integrasi Redis untuk Caching Data Graph dan Live Traffic Penalties

## Konteks & Tujuan
Kita ingin mengintegrasikan Redis ke dalam backend FastAPI + Rust untuk:
1. Mempercepat loading time data Graph saat server startup.
2. Menyimpan dan membaca penalti lalu lintas dinamis (Traffic Overlays).
3. Melakukan caching pada rute yang sering diakses (Route Caching).

## Persyaratan Teknis

1. **Client Library**
   - Gunakan `redis-py` versi async (`redis.asyncio`) di FastAPI.
   - Sambungkan koneksi Redis melalui `Lifespan` context manager di `app/main.py` dan simpan client di `app.state.redis`.

2. **Fitur Caching Rute**
   - Buat helper/service `app/services/cache_service.py`.
   - Sebelum memanggil Rust Pathfinding Engine, buat Redis Key berbasis Hash dari koordinat: `route:{start_node}:{goal_node}`.
   - Jika key ditemukan di Redis, langsung return data JSON tanpa menjalankan Rust Engine. Jika tidak, jalankan Rust Engine dan simpan hasilnya ke Redis dengan TTL 300 detik (5 menit).

3. **Live Traffic Weight Overlay**
   - Sediakan endpoint `POST /api/v1/traffic/update-weight` untuk menerima penalti jalan (misal: `{ "edge_id": 1052, "multiplier": 3.0 }`).
   - Simpan data penalti ini di Redis Hash Key `traffic:penalties`.
   - Oper data penalti ini ke Rust Engine saat kalkulasi rute dilakukan.

## Instruksi Pembuatan Kode

1. Buatkan file `app/core/redis.py` untuk mengelola koneksi Redis pool (async).
2. Buatkan file `app/services/cache_service.py` untuk penanganan cache get/set rute.
3. Update `app/main.py` untuk inisialisasi Redis connection pool pada lifespan event.
4. Update `app/api/v1/endpoints/pathfinding.py` agar mengonsumsi `cache_service` sebelum dan sesudah eksekusi rute.
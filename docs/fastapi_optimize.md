# Task: Optimisasi Backend FastAPI untuk Pathfinding Engine Performa Tinggi

## Konteks & Tujuan
Kita sedang membangun backend pathfinding engine berbasis FastAPI. Untuk menangani request rute dengan latency sub-milidetik, FastAPI harus dioptimasi agar tidak terjadi I/O bottleneck, event-loop blocking, atau overhead serialisasi JSON.

## Panduan Arsitektur & Optimisasi yang Harus Diterapkan

1. **Lifespan / Startup Event Graph Loading (In-Memory)**
   - Jangan pernah membaca data graph dari database atau file disk di dalam fungsi HTTP Request handler.
   - Load seluruh data Graph OSM/CH yang sudah dipadatkan ke dalam RAM saat server pertama kali dinyalakan menggunakan async lifespan context manager FastAPI (`@asynccontextmanager`).
   - Simpan data graph di `app.state.graph` agar accessible secara global oleh seluruh router.

2. **Penggunaan Async & Threading (Non-blocking Event Loop)**
   - Algoritma pathfinding adalah proses CPU-bound.
   - Jika dipanggil dari Python standar, jalankan panggilan fungsi pathfinding menggunakan `anyio.to_thread.run_sync()` atau `run_in_threadpool` agar tidak mengunci (*freeze*) Asyncio Event Loop FastAPI.
   - Pastikan fungsi endpoint didefinisikan sebagai `async def`.

3. **Serialisasi JSON Performa Tinggi (`orjson`)**
   - Hasil response rute berisi ribuan array koordinat `[lat, lon]`.
   - Gunakan `ORJSONResponse` dari `fastapi.responses` sebagai `default_response_class` pada FastAPI app untuk mempercepat proses encoding JSON hingga 5x lebih cepat dari `json` standar.

4. **Validasi Input Ringan & Pydantic Optimization**
   - Pastikan `RouteRequest` dan `RouteResponse` menggunakan Pydantic V2 (`pydantic.BaseModel`).
   - Gunakan `tuple` alih-alih `list` untuk koordinat jika memungkinkan, guna menghemat alokasi memori.

## Instruksi Pembuatan Kode
Terapkan optimisasi di atas pada file-file berikut sesuai struktur project `TEST2`:

1. `app/main.py`: Update initialization app dengan `lifespan`, `ORJSONResponse`, dan CORS configuration.
2. `app/schemas/pathfinding.py`: Update schema Pydantic V2 untuk validasi cepat.
3. `app/api/v1/endpoints/pathfinding.py`: Refactor controller agar mengonsumsi graph dari `app.state` dan mengeksekusi komputasi secara non-blocking.

Buatkan seluruh file di atas dengan *clean code*, lengkap dengan penanganan error (`HTTPException` untuk koordinat di luar jangkauan/rute tidak ditemukan).
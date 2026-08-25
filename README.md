# Swift Backend — Pathfinding & Delivery Engine

Backend untuk **Swift**, sistem rekomendasi rute pengiriman *last mile* yang membantu kurir menentukan urutan pengantaran, mencari jalur, melakukan *tracking*, serta menyesuaikan rute ketika kondisi perjalanan berubah.

> **AI FOR THE BACKBONE OF ECONOMY — SMART LOGISTICS**  
> AI Innovation Competition — COMPFEST 18  
> Fakultas Ilmu Komputer, Universitas Indonesia

---

## 1. Gambaran Umum

Swift dirancang untuk membantu proses distribusi *last mile* dengan mengotomatisasi beberapa pekerjaan yang sebelumnya dilakukan secara manual oleh kurir, terutama:

- Menentukan urutan pengantaran beberapa paket.
- Menghitung jalur antara titik pengiriman.
- Menghitung estimasi jarak dan waktu perjalanan.
- Memantau posisi kurir secara real-time.
- Mendeteksi ketika kurir keluar dari rute.
- Menghasilkan rute baru ketika diperlukan.
- Mencatat status dan riwayat pengiriman.
- Mengelola *Proof of Delivery* (POD).

Sistem menggunakan kombinasi **graph pathfinding, route optimization, real-time tracking, dan AI sebagai komponen pendukung**.

AI tidak digunakan untuk menggantikan algoritma pencarian jalur. Perhitungan rute utama tetap dilakukan menggunakan data jaringan jalan dan algoritma optimasi.

---

## 2. Arsitektur Singkat

```text
                    ┌─────────────────────┐
                    │    Mobile Client    │
                    │   (Aplikasi Kurir)  │
                    └──────────┬──────────┘
                               │
                    REST API / WebSocket
                               │
                               ▼
                    ┌─────────────────────┐
                    │       FastAPI       │
                    │       Backend       │
                    └──────────┬──────────┘
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
      ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
      │ Pathfinding │   │ Optimization│   │  Tracking   │
      │    A* / CH  │   │ TSP / Heur. │   │ WebSocket   │
      └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
             │                 │                 │
             └─────────────────┼─────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │       Redis         │
                    │ Cache / Session /   │
                    │ Traffic Information │
                    └─────────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   PostgreSQL +      │
                    │      PostGIS        │
                    └─────────────────────┘

External Services:
OSM / PBF  ──► Road Network
TomTom     ──► Traffic Information
Cloudinary ──► Proof of Delivery
Gemini     ──► Optional AI Decision Support
```

---

## 3. Fitur Utama

| Fitur | Endpoint / Komponen | Keterangan |
|---|---|---|
| Authentication | `POST /api/v1/auth/login` | Login kurir dan JWT authentication |
| Pathfinding | `POST /api/v1/pathfinding/find-route` | Mencari jalur dari titik A ke B |
| Route Optimization | `POST /api/v1/pathfinding/find-optimized-delivery-route` | Menentukan urutan beberapa titik pengiriman |
| Shipment Management | `/api/v1/shipments/*` | Mengelola status dan riwayat pengiriman |
| Batch Management | `/api/v1/batches/*` | Mengelola kelompok pengiriman kurir |
| Driver Tracking | `WS /api/v1/ws/driver/position` | Menerima posisi kurir secara real-time |
| Navigation | `WS /api/v1/ws/navigation` | Mengirim progress dan perubahan rute |
| Dynamic Rerouting | Navigation WebSocket | Menghitung ulang rute ketika kurir keluar jalur |
| Proof of Delivery | `POST /api/v1/shipments/{id}/history/photo` | Upload foto bukti pengiriman |
| Photo Upload | `POST /api/v1/uploads/photo` | Upload foto ke Cloudinary |
| Health Check | `GET /health` | Memeriksa kondisi backend dan routing engine |
| Metrics | `GET /metrics` | Prometheus metrics jika diaktifkan |

Swagger tersedia setelah server berjalan:

```text
http://localhost:8001/docs
```

---

## 4. Teknologi

- **Python 3.12**
- **FastAPI**
- **SQLAlchemy Async**
- **PostgreSQL 16 + PostGIS**
- **Redis 7**
- **OpenStreetMap**
- **OSMnx**
- **A* Pathfinding**
- **Contraction Hierarchies (CH)**
- **TSP Heuristic**
- **Nearest Neighbor**
- **2-opt / Simulated Annealing**
- **WebSocket**
- **Cloudinary**
- **TomTom Traffic API**
- **Gemini API (opsional)**
- **Docker / Docker Compose**

---

# 5. Struktur Repository

```text
.
├── app/
│   ├── main.py
│   ├── api/
│   │   └── v1/
│   │       └── endpoints/
│   │           ├── auth/
│   │           ├── shipments/
│   │           ├── pathfinding/
│   │           ├── tracking/
│   │           ├── navigation/
│   │           └── uploads/
│   │
│   ├── services/
│   │   ├── pathfinding/
│   │   ├── traffic/
│   │   ├── ai_agent.py
│   │   └── cloudinary_service.py
│   │
│   ├── models/
│   │   ├── kurir.py
│   │   ├── paket.py
│   │   ├── batch.py
│   │   ├── shipment.py
│   │   ├── tracking_history.py
│   │   └── hub.py
│   │
│   └── core/
│       ├── database.py
│       └── logging.py
│
├── data/
│   ├── pbf/
│   │   └── java-260805.osm.pbf
│   └── tiles/
│       ├── manifest.json
│       └── *.pbf
│
├── scripts/
│   ├── seed_kurir.py
│   ├── seed_hub.py
│   ├── seed_paket.py
│   ├── seed_paket_per_kurir.py
│   ├── seed_batch.py
│   ├── seed_shipment.py
│   └── prewarm_route.py
│
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── pyproject.toml
└── .env.example
```

---

# 6. Prasyarat

Untuk setup yang direkomendasikan:

- Docker Desktop
- Docker Compose v2
- Git
- Koneksi internet

Docker Desktop:

https://docs.docker.com/get-docker/

Tidak perlu install PostgreSQL, PostGIS, atau Redis secara manual karena semuanya dijalankan melalui Docker Compose.

Port yang digunakan:

```text
8000  → Production API
8001  → Development API
5432  → PostgreSQL
6379  → Redis
```

---

# 7. Setup Cepat untuk Panitia

## 7.1 Clone Repository

```powershell
git clone https://github.com/Fiqqar/Swift_Backend.git
```

---

## 7.2 Environment

Buat file `.env`:

```powershell
Copy-Item .env.example .env
```

Konfigurasi minimal:

```ini
APP_ENV=development
LOG_LEVEL=INFO

DB_USER=postgres
DB_PASSWORD=postgres
DB_NAME=test2

REDIS_HOST=redis

JWT_SECRET=change-me-in-production

# Opsional
# GEMINI_API_KEY=
# TOMTOM_API_KEY=
# CLOUDINARY_CLOUD_NAME=
# CLOUDINARY_API_KEY=
# CLOUDINARY_API_SECRET=
```

API key eksternal tidak diperlukan untuk menjalankan fungsi utama pathfinding dan route optimization.

---

# 8. Menjalankan Backend

Build dan jalankan:

```powershell
docker compose up --build -d app-dev db redis
```

Periksa container:

```powershell
docker compose ps
```

Lihat log:

```powershell
docker compose logs -f app-dev
```

Backend development:

```text
http://localhost:8001
```

Swagger:

```text
http://localhost:8001/docs
```

Health check:

```powershell
curl.exe http://localhost:8001/health
```

---

# 9. Road Network Data

Swift membutuhkan data jaringan jalan untuk melakukan pathfinding.

Dataset jaringan jalan menggunakan OpenStreetMap dalam format PBF.

File utama:

```text
data/pbf/java-260805.osm.pbf
```

Ukuran dataset sekitar:

```text
895 MB
```

Pada startup pertama, backend dapat mengunduh dataset berdasarkan `PBF_URL`.

Jika file sudah tersedia secara lokal, proses download akan dilewati.

Karena ukuran dataset cukup besar, startup pertama dapat membutuhkan waktu lebih lama.

---

# 10. Seed Database

Jalankan seed:

```powershell
docker compose exec app-dev python scripts/seed_kurir.py
docker compose exec app-dev python scripts/seed_hub.py
docker compose exec app-dev python scripts/seed_paket.py
docker compose exec app-dev python scripts/seed_paket_per_kurir.py
```

Jika diperlukan:

```powershell
docker compose exec app-dev python scripts/seed_batch.py
docker compose exec app-dev python scripts/seed_shipment.py
```

---

# 11. Akun Dummy

| Nama | Username | Password | Kendaraan | Paket |
|---|---|---|---|---:|
| Joko Susilo | `joko` | `rahasia123` | motorcycle | 6 |
| Budi Hartono | `budi` | `rahasia123` | car | 6 |
| Sari Wulandari | `sari` | `rahasia123` | truck | 10 |

Login:

```http
POST /api/v1/auth/login
```

Contoh:

```powershell
$resp = Invoke-RestMethod `
  -Uri "http://localhost:8001/api/v1/auth/login" `
  -Method Post `
  -Body (@{
    username = "sari"
    password = "rahasia123"
  } | ConvertTo-Json) `
  -ContentType "application/json"

$token = $resp.data.token
```

Gunakan token sebagai:

```text
Authorization: Bearer <JWT_TOKEN>
```

---

# 12. Pengujian Pathfinding

Endpoint:

```http
POST /api/v1/pathfinding/find-route
```

Contoh request:

```json
{
  "origin": {
    "latitude": -6.8048,
    "longitude": 110.8385
  },
  "destination": {
    "latitude": -6.8100,
    "longitude": 110.8500
  },
  "mode": "car"
}
```

PowerShell:

```powershell
Invoke-RestMethod `
  -Uri "http://localhost:8001/api/v1/pathfinding/find-route" `
  -Method Post `
  -Headers @{
    Authorization = "Bearer $token"
    "Content-Type" = "application/json"
  } `
  -Body (@{
    origin = @{
      latitude = -6.8048
      longitude = 110.8385
    }
    destination = @{
      latitude = -6.8100
      longitude = 110.8500
    }
    mode = "car"
  } | ConvertTo-Json)
```

Hasil routing menyediakan informasi seperti:

```text
Distance
Estimated Travel Time
Polyline
Route Information
```

---

# 13. Route Optimization

Untuk skenario kurir membawa beberapa paket:

```http
POST /api/v1/pathfinding/find-optimized-delivery-route
```

Alurnya:

```text
Daftar Paket
     │
     ▼
Koordinat Tujuan
     │
     ▼
Matriks Jarak / Waktu
     │
     ▼
Route Optimization
     │
     ▼
Urutan Stop
     │
     ▼
Pathfinding Setiap Leg
     │
     ▼
Rute Pengiriman
```

Pendekatan yang digunakan meliputi:

```text
Nearest Neighbor
2-opt
Simulated Annealing
```

Tujuan utamanya adalah menghasilkan urutan pengiriman yang lebih efisien dibandingkan urutan input.

---

# 14. Pathfinding Engine

Swift menggunakan road graph dari OpenStreetMap.

Secara umum:

```text
OpenStreetMap Road Network
            │
            ▼
         Road Graph
            │
            ▼
       A* / CH Engine
            │
            ▼
       Route / Polyline
```

Cost perjalanan dapat mempertimbangkan:

```text
Distance
Estimated Travel Time
Traffic Penalty
Road Conditions
```

Untuk beberapa bagian routing, implementasi engine yang dioptimalkan dengan Rust digunakan untuk meningkatkan performa pada graph yang besar.

---

# 15. Real-Time Driver Tracking

Endpoint:

```text
WS /api/v1/ws/driver/position
```

Client mengirim posisi:

```json
{
  "type": "position",
  "lat": -6.8048,
  "lon": 110.8385
}
```

Backend kemudian:

1. Menyimpan posisi terakhir.
2. Melakukan snap-to-road.
3. Menghitung hubungan posisi terhadap tujuan.
4. Menjalankan geofence bila diperlukan.

Posisi disimpan sementara di Redis:

```text
driver:pos:{kurir_id}
```

Geofence default:

```text
30 meter
```

---

# 16. Navigation & Dynamic Rerouting

Endpoint:

```text
WS /api/v1/ws/navigation
```

Alur:

```text
Start Navigation
      │
      ▼
Route Leg Aktif
      │
      ▼
GPS Update
      │
      ▼
Snap-to-Road
      │
      ▼
Hitung Jarak ke Route
      │
      ├── Normal
      │     └── Update Progress
      │
      └── Off-route
            │
            ▼
       Hitung Rute Baru
            │
            ▼
       Update ETA
            │
            ▼
       Kirim Route Baru
```

Threshold off-route:

```text
40 meter
```

Jika posisi kurir berada di luar threshold, backend dapat menghitung ulang rute dari posisi terbaru menuju tujuan berikutnya.

Event yang dapat dikirim:

```text
route_progress
off_route_warning
auto_rerouted
```

---

# 17. Peran AI

AI pada Swift merupakan **komponen pendukung keputusan**, bukan pengganti routing engine.

Perhitungan utama dilakukan oleh:

```text
A*
Contraction Hierarchies
Nearest Neighbor
2-opt
Simulated Annealing
```

Komponen AI dapat digunakan untuk membantu keputusan pada kondisi dinamis seperti:

```text
Off-route
Traffic condition
Rerouting decision
```

Jika Gemini tersedia, backend dapat menggunakannya melalui function calling.

Jika Gemini tidak tersedia atau API gagal, backend menggunakan fallback deterministik.

Dengan demikian:

```text
                 Swift Routing
                       │
          ┌────────────┴────────────┐
          │                         │
          ▼                         ▼
  Deterministic Engine       AI Decision Support
          │                         │
      A* / CH                  Gemini (optional)
      TSP                       Rerouting
      Heuristics                Traffic Decision
          │                         │
          └────────────┬────────────┘
                       ▼
                  Final Route
```

**Core routing tetap berjalan tanpa Gemini API key.**

---

# 18. Traffic Adaptation

Traffic dapat memengaruhi biaya sebuah edge jalan.

Alur sederhana:

```text
Traffic Information
        │
        ▼
Traffic Penalty
        │
        ▼
Updated Edge Cost
        │
        ▼
A*
        │
        ▼
Traffic-aware Route
```

Traffic dapat berasal dari:

- TomTom Traffic API.
- Data internal.
- Data simulasi untuk kebutuhan prototipe.

Jika layanan eksternal tidak tersedia, sistem tetap menggunakan data atau fallback yang tersedia di backend.

---

# 19. Shipment Management

Alur status shipment:

```text
assigned
    │
    ▼
picked_up
    │
    ▼
delivered
```

Update status:

```http
PATCH /api/v1/shipments/{id}/status
```

Contoh:

```powershell
Invoke-RestMethod `
  -Uri "http://localhost:8001/api/v1/shipments/23/status" `
  -Method Patch `
  -Headers @{
    Authorization = "Bearer $token"
    "Content-Type" = "application/json"
  } `
  -Body (@{
    status = "picked_up"
  } | ConvertTo-Json)
```

---

# 20. Proof of Delivery

Endpoint:

```http
POST /api/v1/shipments/{id}/history/photo
```

Contoh:

```powershell
curl.exe `
  -X POST `
  "http://localhost:8001/api/v1/shipments/23/history/photo" `
  -H "Authorization: Bearer $token" `
  -F "files=@foto.jpg" `
  -F "recipient_name=Penerima"
```

Foto dapat diunggah ke Cloudinary.

Upload melakukan validasi file sebelum diproses.

Jika Cloudinary tidak dikonfigurasi, fitur POD dapat mengembalikan `503`, tetapi fungsi utama routing tetap dapat digunakan.

---

# 21. Alur End-to-End

```text
Kurir Login
    │
    ▼
Mendapatkan Batch Paket
    │
    ▼
Membaca Daftar Tujuan
    │
    ▼
Route Optimization
    │
    ▼
Menentukan Urutan Pengiriman
    │
    ▼
Pathfinding Setiap Leg
    │
    ▼
Kurir Memulai Navigasi
    │
    ▼
GPS melalui WebSocket
    │
    ▼
Backend Menghitung Progress
    │
    ├───────────────┐
    │               │
    ▼               ▼
On Route        Off Route
    │               │
    ▼               ▼
Progress       Recalculate Route
    │               │
    │               ▼
    │          Rerouting Decision
    │               │
    │               ▼
    │          Route Baru
    │               │
    └───────┬───────┘
            ▼
       Paket Diantar
            │
            ▼
       Update Status
            │
            ▼
          Upload POD
```

---

# 22. Pengujian Tracking

Tracking WebSocket:

```text
WS /api/v1/ws/driver/position
```

Contoh payload:

```json
{
  "type": "position",
  "lat": -6.8048,
  "lon": 110.8385
}
```

Navigation WebSocket:

```text
WS /api/v1/ws/navigation
```

Contoh proses:

```text
start_navigation
      │
      ▼
route_progress
      │
      ▼
location_update
      │
      ▼
off_route_warning
      │
      ▼
auto_rerouted
```

---

# 23. Tracking History

Untuk melihat riwayat pengiriman:

```powershell
Invoke-RestMethod `
  -Uri "http://localhost:8001/api/v1/shipments/23/tracking" `
  -Headers @{
    Authorization = "Bearer $token"
  }
```

Riwayat dapat mencatat:

```text
Shipment Status
Tracking Event
Navigation Event
Proof of Delivery
```

---

# 24. Redis

Redis digunakan sebagai penyimpanan sementara untuk proses real-time.

Contoh key:

```text
driver:pos:{kurir_id}
driver:nav:{kurir_id}
```

Penggunaan Redis meliputi:

- Posisi terakhir kurir.
- Session navigasi.
- Cache route.
- Traffic penalty.
- Informasi real-time lainnya.

Redis bukan sumber data utama untuk data operasional.

---

# 25. Health Check

Endpoint:

```http
GET /health
```

Pengujian:

```powershell
curl.exe http://localhost:8001/health
```

Health check digunakan untuk memastikan backend dan komponen routing utama siap digunakan.

---

# 26. API Documentation

Swagger UI:

```text
http://localhost:8001/docs
```

Swagger dapat digunakan untuk:

- Melihat seluruh endpoint.
- Melihat request/response schema.
- Login menggunakan JWT.
- Menguji pathfinding.
- Menguji shipment management.
- Menguji endpoint lainnya.

Endpoint tambahan:

```text
GET /health
GET /metrics
GET /api/v1/ws/driver/position/status
GET /api/v1/ws/navigation/status
```

---

# 27. Environment Variables

| Variable | Fungsi |
|---|---|
| `APP_ENV` | Environment backend |
| `LOG_LEVEL` | Level logging |
| `DATABASE_URL` | PostgreSQL connection |
| `REDIS_URL` | Redis connection |
| `PBF_URL` | Sumber dataset road network |
| `TILES_DIR` | Lokasi road network tiles |
| `ENABLE_LIVE_TRACKING` | Mengaktifkan tracking WebSocket |
| `ENABLE_LIVE_NAVIGATION` | Mengaktifkan navigation WebSocket |
| `AI_REROUTE_ENABLED` | Mengaktifkan AI decision layer |
| `GEMINI_API_KEY` | Gemini API key, opsional |
| `TOMTOM_API_KEY` | TomTom API key, opsional |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary configuration |
| `CLOUDINARY_API_KEY` | Cloudinary configuration |
| `CLOUDINARY_API_SECRET` | Cloudinary configuration |
| `CORS_ORIGINS` | Allowed frontend origins |

---

# 28. Troubleshooting

## PBF Download Gagal

Periksa koneksi internet dan `PBF_URL`.

File dapat diletakkan secara manual:

```text
data/pbf/java-260805.osm.pbf
```

Kemudian restart:

```powershell
docker compose restart app-dev
```

---

## Database Connection Refused

Periksa:

```powershell
docker compose ps
```

Pastikan service `db` sudah healthy.

Jika baru pertama kali dijalankan, tunggu proses initialization PostgreSQL selesai.

---

## Redis Tidak Tersedia

Periksa:

```powershell
docker compose ps
```

Pastikan Redis berjalan.

```powershell
docker compose logs redis
```

---

## Login 401

Jalankan ulang:

```powershell
docker compose exec app-dev python scripts/seed_kurir.py
```

Gunakan:

```text
sari / rahasia123
joko / rahasia123
budi / rahasia123
```

---

## AreaNotCovered

Gunakan koordinat dalam area road graph yang tersedia.

Contoh Kudus:

```text
Origin:
-6.8048, 110.8385

Destination:
-6.8100, 110.8500
```

---

## AI Tidak Aktif

AI bersifat opsional.

Tanpa:

```ini
GEMINI_API_KEY=
```

core routing tetap dapat berjalan menggunakan fallback deterministik.

---

## Cloudinary Tidak Tersedia

Jika Cloudinary tidak dikonfigurasi, upload POD dapat mengembalikan:

```text
503 Service Unavailable
```

Fitur pathfinding dan route optimization tetap dapat digunakan.

---

## Port 8001 Sudah Digunakan

Periksa:

```powershell
netstat -ano | findstr :8001
```

Atau hentikan stack:

```powershell
docker compose down
```

Kemudian jalankan:

```powershell
docker compose up --build -d app-dev db redis
```

---

# 29. Logging

Backend menggunakan structured logging.

Lihat log:

```powershell
docker compose logs -f app-dev
```

Aktivitas yang dapat terlihat antara lain:

```text
login_attempt
login_success
route_calculated
route_progress
off_route_detected
auto_rerouted
status_updated
upload_attempt
upload_success
```

Logging berguna untuk debugging sekaligus menunjukkan proses backend ketika demonstrasi.

---

# 30. Metrics

Jika metrics diaktifkan:

```text
GET /metrics
```

Gunakan:

```ini
METRICS_ENABLED=1
```

Metrics dapat digunakan untuk memantau performa backend.

---

# 31. Cloudflare Tunnel (Opsional)

Untuk demonstrasi yang membutuhkan akses dari luar jaringan lokal:

```powershell
docker compose up --build -d app-dev db redis quick-tunnel-dev
```

Lihat URL:

```powershell
docker logs quick-tunnel-dev --tail 20 | Select-String trycloudflare
```

URL akan berbentuk:

```text
https://xxxxx.trycloudflare.com
```

Quick Tunnel menghasilkan URL baru jika container dibuat ulang.

Untuk update backend tanpa membuat ulang tunnel:

```powershell
docker compose up --build -d app-dev --no-deps
```

---

# 32. Reset Environment

Untuk menghentikan service:

```powershell
docker compose down
```

Untuk reset total termasuk database:

```powershell
docker compose down -v
```

> `docker compose down -v` akan menghapus volume database.

Setelah reset:

```powershell
docker compose up --build -d app-dev db redis

docker compose exec app-dev python scripts/seed_kurir.py
docker compose exec app-dev python scripts/seed_hub.py
docker compose exec app-dev python scripts/seed_paket.py
docker compose exec app-dev python scripts/seed_paket_per_kurir.py
```

---

# 33. Quick Start

Untuk menjalankan backend dari kondisi awal:

```powershell
git clone https://github.com/Fiqqar/Swift_Backend.git

Copy-Item .env.example .env

docker compose up --build -d app-dev db redis

docker compose exec app-dev python scripts/seed_kurir.py
docker compose exec app-dev python scripts/seed_hub.py
docker compose exec app-dev python scripts/seed_paket.py
docker compose exec app-dev python scripts/seed_paket_per_kurir.py
```

Kemudian buka:

```text
http://localhost:8001/docs
```

Login:

```text
Username : sari
Password : rahasia123
```

Setelah login, panitia dapat menguji:

```text
Authentication
      ↓
Shipment Management
      ↓
Route Optimization
      ↓
A* Pathfinding
      ↓
Real-time Navigation
      ↓
Off-route Detection
      ↓
Dynamic Rerouting
```

---

# 34. Catatan untuk Evaluator

Swift memiliki beberapa komponen dengan tanggung jawab berbeda:

**Route Optimization**  
Menentukan urutan beberapa tujuan pengiriman menggunakan pendekatan heuristic.

**Pathfinding Engine**  
Menentukan jalur aktual pada road graph menggunakan A*/CH.

**Tracking & Navigation**  
Mengirim dan memproses posisi kurir secara real-time menggunakan WebSocket.

**Dynamic Rerouting**  
Menghitung ulang jalur ketika posisi kurir keluar dari route atau kondisi perjalanan berubah.

**Redis**  
Menyimpan state sementara seperti posisi kurir, session navigasi, cache route, dan informasi traffic.

**PostgreSQL/PostGIS**  
Menyimpan data operasional seperti kurir, paket, batch, shipment, dan tracking history.

**AI Decision Support**  
Membantu pengambilan keputusan pada kondisi tertentu dan bersifat opsional terhadap core routing engine.

Dengan arsitektur ini, sistem tetap dapat melakukan fungsi routing utama walaupun layanan AI atau service eksternal tertentu tidak tersedia.

---

# Swift

**AI FOR THE BACKBONE OF ECONOMY — SMART LOGISTICS**

AI Innovation Competition — COMPFEST 18  
Fakultas Ilmu Komputer, Universitas Indonesia  
2026
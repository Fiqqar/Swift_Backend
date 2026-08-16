# 🚚 B2B Multi-Stop Last-Mile Routing & Delivery Management System

> **Comprehensive System Architecture, Code Audit Checklist, & Implementation Blueprint**

Dokumen ini berisi panduan teknis end-to-end untuk sistem pengantaran paket B2B (_Last-Mile Delivery_). Dokumen ini menggabungkan **Arsitektur Sistem B2B**, **Checklist Audit Kode**, dan **Blueprint Implementasi** yang disesuaikan (_tailored_) dengan struktur endpoint Swagger API proyek saat ini.

---

## 📑 Daftar Isi

1. [System Overview & Workflow](#1-system-overview--workflow)
2. [Code & Architecture Audit Checklist](#2-code--architecture-audit-checklist)
3. [Database Schema Design](#3-database-schema-design)
4. [API Endpoint Mapping & Payloads](#4-api-endpoint-mapping--payloads)
5. [Technical Optimizations & Pathfinding Engine](#5-technical-optimizations--pathfinding-engine)

---

## 📐 1. System Overview & Workflow

Sistem ini menghubungkan 3 entitas utama secara real-time:

1. **Klien B2B (WMS/ERP/Dashboard):** Mengirimkan daftar paket harian melalui HTTP API Ingestion.
2. **Backend Server (Pathfinding & Batch Engine):** Menyimpan data, melakukan _geocoding_, menjalankan algoritma optimasi rute (TSP & 2-Opt), dan menyediakan audit trail.
3. **Aplikasi Mobile/Web Kurir:** Menerima notifikasi penugasan, menampilkan rute navigasi turn-by-turn per _leg_, serta mengunggah bukti pengiriman (_Proof of Delivery / POD_).

```text
┌─────────────────┐      1. POST /api/v1/batches          ┌──────────────────────┐
│                 ├──────────────────────────────────────►│                      │
│ B2B Client System │                                     │ Backend API Server   │
│ (WMS / Dashboard)│◄─────────────────────────────────────┤ (FastAPI / Express)  │
└─────────────────┘      2. Response: batch_id            └──────────┬───────────┘
                                                                     │
                                                                     │ 3. Save Batch & Shipments to DB
                                                                     │ 4. Push Notice (FCM/WebSocket)
                                                                     ▼
┌─────────────────┐                                       ┌──────────────────────┐
│ Driver App UI   │◄──────────────────────────────────────┤ Driver Mobile App    │
│ (Field Navigation)      5. Trigger "Start Navigation"   │ (State Machine UI)   │
└────────┬────────┘                                       └──────────┬───────────┘
         │                                                           │
         │ 6. POST /api/v1/pathfinding/find-optimized-delivery-route  │
         │    (Sends batch deliveries -> gets ordered sequence)      │
         ▼                                                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            TSP PATHFINDING ENGINE                           │
│  - Reads: hub_origin + list of deliveries (lat, lng, service_type)          │
│  - Calculates: Priority TSP Matrix (EXPRESS weight factor = 0.6)            │
│  - Returns: Stop Sequence (1, 2, 3...) + Geometry Polyline & Traffic        │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                             DELIVERY EXECUTION                              │
│  1. Driver navigates along active leg (Stop 1: Bold Blue)                   │
│  2. Geofence Trigger (Radius ≤ 30m) -> Pop-up POD Validation Form           │
│  3. Submit POD:                                                             │
│     - PATCH /api/v1/shipments/{id}/status   (assigned -> picked_up ->      │
│       delivered)                                                           │
│     - POST  /api/v1/shipments/{id}/history/photo  (upload foto ke Cloudinary │
│       server-side -> otomatis buat riwayat pod_submitted)                    │
│     - PATCH /api/v1/shipments/{id}/cod      (collected -> remitted)        │
│  4. UI auto-advances Active Index to Stop 2                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 📌 Catatan Implementasi

1. **`service_type` (EXPRESS/REGULAR):** tersedia di kolom `Paket.service_type` (default `REGULAR`, backfill dari `jenis_pengiriman`). Payload pathfinding tetap menerima `service_type` per delivery.
2. **Geofence Trigger (≤ 30m):** tersedia lewat `POST /api/v1/pathfinding/geofence-check` (haversine). UI demo delivery mengharuskan kurir dalam radius sebelum "Konfirmasi Terkirim".
3. **POD Photo/GPS:** upload **server-side** lewat `POST /shipments/{id}/history/photo` (multipart `files` 1–5 foto, maks 10 MB/file, isi dicek via magic bytes + opsional `recipient_name`, `latitude`, `longitude`, `keterangan`) → unggah ke Cloudinary (folder `pod/shipments/{id}/...`) lalu otomatis membuat riwayat `pod_submitted`. `POST /shipments/{id}/history` (JSON) tetap tersedia untuk alur manual dengan `photo_urls` (array URL Cloudinary — foto multiple). Kredensial dibaca dari env `CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET` (lihat `.env.example`).
4. **Status shipment** memakai `assigned → picked_up → delivered` (plus `failed`/`returned`), bukan `PENDING → IN_TRANSIT → DELIVERED`.
5. **COD:** status COD otomatis menjadi `collected` saat shipment di-set `delivered`; endpoint `PATCH /shipments/{id}/cod` hanya menandai `remitted` (membutuhkan status `collected` sebelumnya).

---

## ⚠️ Status Implementasi & Gap (checklist2.md)

| # | Checklist | Status | Lokasi |
|---|-----------|--------|--------|
| 1.1 | DB persistence pada `POST /api/v1/batches` | ✅ Ada | `app/api/v1/endpoints/shipments.py:112` |
| 1.2 | Relasi 1 Batch → Banyak Shipments | ✅ Ada | `app/models/shipment.py:13` (`batch_id` FK) |
| 1.3 | Kolom `service_type` EXPRESS/REGULAR | ✅ Ada | `app/models/paket.py:17` + `init_db()` backfill |
| 2.1 | Payload `hub_origin` + array `deliveries` | ✅ Ada | `app/schemas/pathfinding.py:78` |
| 2.2 | TSP hybrid (Haversine + road routing) | ✅ Ada | `app/services/pathfinding/delivery_optimizer.py` |
| 2.3 | Bobot prioritas Express (`0.6`) | ✅ Ada | `delivery_optimizer.py:14` |
| 2.4 | Response `legs` + geometry + incidents | ✅ Ada | `app/schemas/pathfinding.py:88` |
| 3.1 | `PATCH /shipments/{id}/status` | ✅ Ada | `app/api/v1/endpoints/shipments.py:398` |
| 3.2 | POD foto/GPS di `POST /shipments/{id}/history` | ✅ Ada | `app/schemas/shipment.py:34` + `tracking_history.py` |
| 3.4 | Upload Cloudinary server-side + buat history | ✅ Ada | `app/api/v1/endpoints/shipments.py` (`/history/photo`) + `app/services/cloudinary_service.py` |
| 3.3 | `PATCH /shipments/{id}/cod` | ✅ Ada | `app/api/v1/endpoints/shipments.py:449` |
| 4.1 | Geofence check radius ≤ 30m | ✅ Ada | `app/api/v1/endpoints/pathfinding.py` (`/geofence-check`) |

### Langkah lanjutan yang disarankan

- **Integrasi Frontend Mobile/Web:** hubungkan tombol "Mulai Navigasi" ke endpoint pathfinding dan alur POD (`PATCH status` → `POST history/photo` → `PATCH cod`) dengan token auth kurir.
- **Roadmap:** upload foto ke Cloudinary kini server-side (endpoint `/history/photo`); isi `CLOUDINARY_*` di `.env` agar unggahan nyata berfungsi.

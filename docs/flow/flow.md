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
│  3. Submit Photo & Signature:                                               │
│     - PATCH /api/v1/shipments/{id}/status (Set to DELIVERED)              │
│     - POST  /api/v1/shipments/{id}/history (Save Photo, Signature & GPS)   │
│     - PATCH /api/v1/shipments/{id}/cod     (Update COD Cash Collected)    │
│  4. UI auto-advances Active Index to Stop 2                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

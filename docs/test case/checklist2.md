# 🔍 Code & Architecture Audit Checklist

> Panduan pengecekan kesesuaian kode saat ini dengan arsitektur B2B Last-Mile Delivery.

Gunakan _checklist_ di bawah ini untuk memeriksa file-file kode yang sudah kamu buat (terutama di direktori `app/api/` dan `app/services/`).

---

## 1. Modul Shipment & Batch Ingestion (`POST /api/v1/batches`)

- [ ] **Database Persistence:** Apakah endpoint `POST /api/v1/batches` sudah benar-benar menyimpan data pengiriman (_shipments_) ke Database, bukan hanya meneruskan data (_stateless_)?
- [ ] **Relasi Batch & Shipments:** Apakah struktur tabel mendukung relasi 1 _Batch_ ke _Banyak Shipments_ (1-to-Many)?
- [ ] **Atribut Prioritas:** Apakah di dalam model _Shipment/Package_ sudah tersedia kolom `service_type` dengan opsi `EXPRESS` dan `REGULAR`?

## 2. Modul Pathfinding & TSP Optimizer (`POST /api/v1/pathfinding/find-optimized-delivery-route`)

- [ ] **Payload Struktur:** Apakah endpoint pathfinding menerima _request body_ yang mencakup `hub_origin` dan array `deliveries`?
- [ ] **Algoritma Pengurutan (TSP):** Apakah engine menggunakan pendekatan hibrida (Haversine matrix untuk kalkulasi urutan stop tercepat, diikuti routing segmen jalan aktual)?
- [ ] **Bobot Prioritas Express:** Apakah ada mekanisme diskon biaya/bobot (misal `express_discount = 0.6`) yang memaksa paket `EXPRESS` diproses di urutan awal?
- [ ] **Response Legs & Polyline:** Apakah respons API mengembalikan array `legs` lengkap dengan `geometry` (polyline), `distance_km`, `duration_mins`, dan deteksi `incidents` (macet/penutupan)?

## 3. Modul State Management & Proof of Delivery (POD)

- [ ] **Update Status:** Apakah endpoint `PATCH /api/v1/shipments/{shipment_id}/status` tersedia untuk mengubah status paket (`PENDING` $\rightarrow$ `IN_TRANSIT` $\rightarrow$ `DELIVERED`)?
- [ ] **Riwayat & Foto POD:** Apakah endpoint `POST /api/v1/shipments/{shipment_id}/history` mendukung penyimpanan file foto bukti serah terima, nama penerima, dan koordinat GPS geofencing?
- [ ] **Manajemen COD:** Apakah endpoint `PATCH /api/v1/shipments/{shipment_id}/cod` tersedia untuk mencatat pembayaran tunai (jika ada)?

---

## 🛠️ Panduan Tindakan Berdasarkan Hasil Cek:

1. **Jika sudah sesuai:** Lanjutkan ke tahap integrasi Frontend Mobile/Web untuk menghubungkan tombol "Mulai Navigasi" ke endpoint pathfinding.
2. **Jika ada yang kurang (Missing):** Tambahkan fungsi helper/service terkait (misalnya fungsi pembobotan TSP untuk Express atau tabel riwayat POD di database).
3. **Jika ada yang salah arah (Conflict):** Ubah urutan eksekusi agar _Ingestion_ data masuk ke DB dulu, lalu di-trigger oleh TSP engine saat kurir mulai bernavigasi.

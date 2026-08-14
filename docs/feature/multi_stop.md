# Task: Optimized Multi-Stop Last-Mile Routing (Hub to Recipients with Service Priority)

## Context

Aplikasi membutuhkan fitur pengantaran paket multi-stop dari satu Drop Point/Hub ke banyak alamat penerima. Urutan pengantaran TIDAK kaku (bukan A -> B -> C), melainkan dioptimasi secara otomatis untuk mencari rute tercepat/terefisien dengan mempertimbangkan variabel jenis layanan paket (Express vs Regular).

## Requirements

### 1. Route Optimization & TSP Engine (`POST /find_optimized_delivery_route`)

- Terima payload JSON berisi `hub_origin` dan array `deliveries`.
- Hitung Distance Matrix ($N \times N$) antar semua koordinat titik (Hub + Alamat Penerima).
- Terapkan algoritma **TSP / Vehicle Routing Problem (VRP)** untuk menentukan urutan pengantaran (_stop sequence_) paling efisien:
  - **Priority Cost Penalty:** Jika `service_type == 'EXPRESS'`, kurangi cost jarak pada matriks atau prioritaskan titik tersebut di urutan teratas sebelum paket `REGULAR`.
- Kembalikan response berupa rute terurut (`optimized_legs`) beserta urutan `stop_order` (misal: Stop 1, Stop 2, Stop 3).

### 2. Multi-Leg Navigation Payload Response

Setiap objek leg dalam response harus memuat:

- `leg_index` & `stop_sequence_number` (1, 2, 3...).
- `package_id`, `recipient_name`, `service_type`.
- `geometry`: Polyline segmen jalan dari titik $N-1$ ke titik $N$.
- `distance_km`, `duration_mins`, dan `incidents` (jika ada macet/penutupan jalan di segmen tersebut).

### 3. Mobile UI / Map Navigation State Machine

- **Overview Mode:** Render seluruh rute dari Hub ke semua penerima dengan marker bernomor ($1, 2, 3$). Tandai paket `EXPRESS` dengan warna marker beda (misal: Merah/Kuning).
- **Active Navigation Mode:** Sediakan switcher/step-by-step mode. Highlight segmen menuju paket aktif dengan warna biru tebal, dan segmen berikutnya dengan warna abu-abu.
- Sediakan tombol "Konfirmasi Terkirim / Package Delivered" yang akan secara otomatis menggeser active index ke `stop_sequence` berikutnya dan mengkalkulasi ulang sisa rute jika terjadi deviasi lokasi kurir.

### 3. Mobile UI / Map Navigation State Machine

- **Overview Mode:** Render seluruh rute dari Hub ke semua penerima dengan marker bernomor ($1, 2, 3$). Tandai paket `EXPRESS` dengan warna marker beda (misal: Merah/Kuning).
- **Active Navigation Mode:** Sediakan switcher/step-by-step mode. Highlight segmen menuju paket aktif dengan warna biru tebal, dan segmen berikutnya dengan warna abu-abu.
- Sediakan tombol "Konfirmasi Terkirim / Package Delivered" yang akan secara otomatis menggeser active index ke `stop_sequence` berikutnya dan mengkalkulasi ulang sisa rute jika terjadi deviasi lokasi kurir.

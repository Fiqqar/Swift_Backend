# Task: Optimized Multi-Stop Last-Mile Routing (Hub to Recipients with Service Priority)

## Context

Aplikasi membutuhkan fitur pengantaran paket multi-stop dari satu Drop Point/Hub ke banyak alamat penerima. Urutan pengantaran TIDAK kaku (bukan A -> B -> C), melainkan dioptimasi secara otomatis untuk mencari rute tercepat/terefisien dengan mempertimbangkan variabel jenis layanan paket (Express vs Regular).

## Requirements

### 1. Route Optimization & Multi-Stop Solver (`POST /find_optimized_delivery_route`)

- Terima payload JSON berisi `hub_origin`/`courier_position` dan array `deliveries`.
- Titik awal rute = posisi kurir webhook (Redis `driver:pos:{kurir_id}`) → `courier_position` → `hub_origin`.
- Urutkan stop memakai **hybrid greedy** (`optimize_stop_order_hybrid`): dari posisi
  saat ini ambil top-K kandidat terdekat (haversine, env `DELIVERY_ORDER_TOP_K`,
  default 3), hitung **jarak jalan nyata** untuk kandidat itu (cache Redis `route:*`),
  pilih yang paling efisien, lalu ulangi dari stop terpilih sampai stop terakhir —
  perilaku kurir "dari lokasi sekarang selalu cari yang terdekat/efisien".
  **Prioritas EXPRESS:** cost menuju paket EXPRESS dikali diskon (`0.6`) sehingga
  cenderung diantar lebih dulu.
- Kembalikan response berupa rute terurut (`optimized_legs`) beserta urutan
  `stop_order` (misal: Stop 1, Stop 2, Stop 3).
- **Koordinat langsung:** tiap `deliveries[].latitude`/`longitude` opsional —
  bila diberikan dipakai langsung (tanpa geocode); bila kosong, `alamat`
  di-geocode (Nominatim). UI demo `/delivery.html` menerima format baris
  `alamat | EXPRESS | lat,lon` atau `lat,lon`.

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

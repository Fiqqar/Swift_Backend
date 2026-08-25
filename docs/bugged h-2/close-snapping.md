# Temuan #29 — Resolusi Snapping Terlalu Kasar untuk Titik Berdekatan (Hyper-Local Delivery)

**Kategori:** Bug desain sistem (bukan kesalahan pemakaian/input)
**Ditemukan lewat:** laporan visual (screenshot peta) — rute putus tidak sampai ke marker, dan urutan paket berubah drastis saat presisi koordinat diubah.
**File terkait:** `app/services/pathfinding/graph_loader.py`, `app/services/pathfinding/delivery_optimizer.py`, `app/api/v1/endpoints/pathfinding.py`, `app/services/pathfinding/hierarchical.py`, `app/services/pathfinding/connectivity.py`

---

## Penjelasan Sederhana

Sistem pathfinding ini mencari jalan **bukan langsung ke titik koordinat persis** yang diberikan. Ia dulu "menempelkan" (snap) tiap titik ke node jalan terdekat di data peta — mirip peta kertas yang dibagi kotak-kotak seluas kira-kira **111 meter** per kotak.

Kalau titik-titik pengiriman berjarak cuma **10-50 meter** satu sama lain (satu gang/kompleks yang sama), sistem "melihat" mereka semua numpuk di satu kotak yang sama, walau sebenarnya beda rumah.

**Akibat 1 — rute putus/tidak sampai ke marker:** kalau suatu titik ada di gang kecil yang datanya di peta (OSM/PBF) tidak lengkap, sistem "menyerah" mencari jalan asli ke situ dan memaksa menempelkan titik itu ke jalan besar terdekat yang datanya lengkap — walau jaraknya cukup jauh dari marker aslinya. Garis rute berhenti di titik snap ini, bukan di lokasi pin sebenarnya.

**Akibat 2 — urutan paket berubah kalau presisi koordinat diubah:** sistem menentukan urutan pakai 2 cara — jarak garis lurus (pakai koordinat asli apa adanya) dan jarak jalan asli (kena masalah "kotak 111 meter" di atas). Kalau koordinat dibulatkan, pembulatannya bisa lebih besar dari jarak asli antar titik pengiriman — dua rumah berbeda jadi kelihatan "di titik yang sama" bagi sistem, urutannya jadi tidak bisa dibedakan secara akurat.

**Kenapa ini masalah desain, bukan masalah cara pakai:** presisi input koordinat itu di luar kendali sistem — bisa dari geocoding otomatis, input manual, atau device GPS yang beda-beda akurasinya. Sistem yang cuma "berfungsi baik kalau inputnya presisi" itu rapuh, karena akan rusak lagi kapan saja ada input yang dibulatkan atau tidak konsisten. Sistem seharusnya yang menyesuaikan diri, bukan berharap semua pemanggil selalu disiplin memberi data presisi penuh.

---

## Detail Teknis

### Root cause utama

```python
# graph_loader.py
_LOC_BUCKET = 1.0 / 1000.0   # ≈ 111 meter per grid cell
```

`find_nearest_node()` memakai grid dengan resolusi ini untuk mencari node jalan terdekat dari sebuah koordinat. Resolusi ini fixed di seluruh sistem, tidak menyesuaikan kepadatan titik pengiriman.

### Kontributor lain

1. **`hierarchical.py`/`connectivity.py`** — `_local_with_portal()`, `_snap()`, `get_main_component_nodes()`: kalau suatu titik tidak terhubung ke jaringan jalan utama (data OSM/PBF di gang kecil kurang lengkap), sistem memaksa snap ke node jalan utama terhubung terdekat — bisa fisiknya jauh dari marker asli. Ini yang menyebabkan gejala "garis rute berhenti sebelum sampai marker".

2. **`delivery_optimizer.py` — `optimize_stop_order_hybrid()`**:
```python
cand = sorted(remaining, key=lambda i: haversine_distance(current, deliveries[i]))[:top_k]
costs = await asyncio.gather(*(cost_fn(current, deliveries[i]) for i in cand))
```
Tahap 1 (haversine) akurat sesuai presisi koordinat asli. Tahap 2 (`road_cost_fn` → `_ordering_road_distance` di `pathfinding.py`) kena masalah resolusi grid di atas — dua titik berbeda bisa dapat jarak jalan ~0 kalau ke-snap ke node yang sama, membuat algoritma tie-breaking greedy TSP kehilangan sinyal untuk membedakan prioritas urutan.

3. **Gejala tambahan yang mendukung diagnosis ini:** leg dengan jarak `0,02 km` tapi durasi `3 mnt` di screenshot — pola khas ketika dua titik pengiriman snap ke node yang sama (jarak jalan ≈0), tapi `SERVICE_TIME_MINUTES` default (3.0 menit, dari `eta_config()` di `eta.py`) tetap ditambahkan sebagai waktu layanan minimum.

---

## Rekomendasi Fix

### 1. Fallback haversine saat road distance mengembalikan nilai ~0 (prioritas tertinggi)

Di `optimize_stop_order_hybrid()` / `_ordering_road_distance`, tambahkan pengaman:
```python
async def road_cost_fn(o, d):
    road_dist = await _ordering_road_distance(...)
    haversine_dist = haversine_distance(o, d)
    # Kalau road distance jauh lebih kecil dari jarak lurus asli
    # (indikasi node-collision karena snapping terlalu kasar),
    # jangan percaya angka road distance — pakai haversine sebagai
    # sinyal tie-breaking yang masih presisi sesuai input asli.
    if road_dist < 5.0 and haversine_dist > 15.0:
        return haversine_dist
    return road_dist
```
Ini paling murah diimplementasikan dan langsung mengatasi akar masalah #2 (urutan paket berubah-ubah) tanpa mengubah resolusi grid global.

**Perlu juga di-apply di:**
- `navigation.py` — `maybe_reorder_stops_on_off_route()` bagian `road_cost_fn` (sudah ada graceful fallback ke haversine saat routing engine error/gagal, tapi belum ada deteksi node-collision saat routing engine **berhasil** tapi mengembalikan nilai ~0).
- `delivery_optimizer.py` — tidak perlu diubah, menerima cost dari caller.

### 1b. Instrumentasi wajib digabung bareng Fix #1 (bukan langkah terpisah)

**Penting:** sistem saat ini **tidak menyimpan histori** leg/jarak secara permanen — `cache_service.py` (`ROUTE_TTL=300s`), `tracking.py` (`KURIR_NAV_TTL_SECONDS` default 3600s), dan model `Shipment` (tidak ada kolom jarak/durasi leg) semuanya bersifat sementara atau tidak mencatat data ini. Artinya **tidak mungkin mengecek data historis secara retroaktif** untuk memutuskan urgensi Fix #2/#3 — data itu harus mulai dikumpulkan **sejak hari fix #1 di-deploy**, bukan dikumpulkan dulu baru fix belakangan, dan bukan juga fix dulu baru "nanti mikirin data belakangan".

**Tambahkan metric counter Prometheus** (pola yang sudah dipakai konsisten di `core/metrics.py`) di titik yang sama dengan log node-collision:
```python
pf_order_node_collision_total = Counter(
    "pf_order_node_collision_total",
    "Node collisions detected during stop ordering (road_dist << haversine)",
    registry=REGISTRY,
)
```
Increment counter ini persis di titik yang sama dengan log:
```python
if road_dist < 5.0 and straight_dist > 15.0:
    logger.info(
        "[ORDER] Node collision terdeteksi (%.1fm vs %.1fm haversine); "
        "pakai haversine.", road_dist, straight_dist)
    pf_order_node_collision_total.inc()
    return straight_dist
```
Log teks saja tidak cukup untuk "mengonfirmasi frekuensi" secara terukur tanpa infrastruktur log-analytics (ELK/Loki/dsb) — metric Prometheus otomatis bisa dilihat di endpoint `/metrics` yang sudah ada, tanpa infrastruktur tambahan.

**Kriteria angka konkret untuk keputusan Fix #2/#3** (disepakati di awal, supaya "kumpulkan data dulu" tidak jadi keputusan yang menggantung tanpa batas waktu/angka jelas):
- Bandingkan `pf_order_node_collision_total` terhadap total panggilan `_ordering_road_distance` dalam periode observasi (mis. 1-2 minggu setelah deploy).
- **Jika rasio > 5%** dari total panggilan → confirmed frequent, worth investasi Fix #2 (grid adaptif) dan/atau Fix #3 (deteksi otomatis).
- **Jika rasio < 0.5%** dan jarang → cukup andalkan Fix #1 (fallback haversine) sebagai solusi permanen, tidak perlu effort tambahan.
- Di antara 0.5%-5% → evaluasi kasus per kasus, lihat juga apakah kejadian terkonsentrasi di area/klien tertentu (indikasi pola pemakaian spesifik) atau tersebar merata.

### 2. Resolusi grid adaptif untuk kumpulan titik berdekatan

`_LOC_BUCKET` saat ini fixed 111m di seluruh sistem. Idealnya, sebelum mulai proses `find-optimized-delivery-route`, sistem mendeteksi dulu: kalau mayoritas titik pengiriman dalam radius kecil (mis. < 200m satu sama lain), gunakan bucket lebih kecil (mis. 10-20m) khusus untuk proses snapping batch itu — tanpa perlu campur tangan manusia menyesuaikan presisi input.

### 3. Deteksi otomatis "titik numpuk" sebelum snapping

Tambahkan pre-check ringan (haversine antar semua pasangan titik dalam request) — kalau ada 2+ titik dengan jarak di bawah threshold tertentu (mis. 50m), log warning dan otomatis aktifkan mode resolusi tinggi (poin 2) untuk keseluruhan batch tersebut.

---

## Dampak & Prioritas

| Aspek | Penilaian |
|---|---|
| Severity | **Sedang-Tinggi** — langsung memengaruhi akurasi urutan pengiriman & visual rute yang dilihat kurir/dispatcher |
| Kompleksitas fix #1 (fallback haversine) | Rendah — perubahan lokal di satu fungsi |
| Kompleksitas fix #2 (grid adaptif) | Sedang — perlu threading resolusi sebagai parameter, bukan konstanta modul |
| Kompleksitas fix #3 (deteksi otomatis) | Rendah-Sedang — pre-check tambahan sebelum pipeline utama |
| Data pendukung | Berdasarkan 1 laporan visual (screenshot) + analisis kode. Disarankan kumpulkan beberapa kasus produksi lain dengan pola serupa (titik berdekatan <100m) untuk konfirmasi frekuensi masalah sebelum prioritaskan effort fix. |

**Status: FIX #1 + INSTRUMENTASI direkomendasikan untuk dieksekusi sekarang (digabung dalam satu deploy, bukan langkah terpisah), berdasarkan verifikasi kode yang mengonfirmasi seluruh klaim teknis di dokumen ini akurat. Fix #2 (grid adaptif) dan Fix #3 (deteksi otomatis) ditunda sampai data metric `pf_order_node_collision_total` terkumpul cukup (2-4 minggu observasi) dan melewati ambang batas yang disepakati di atas.**

**Verifikasi terhadap fix Batch 1-5 sebelumnya:** dikonfirmasi #29 **belum** teratasi oleh perbaikan manapun di Batch 1-5 — graceful degradation `road_cost_fn` → haversine (Batch 2) hanya aktif saat routing engine gagal/error, bukan saat berhasil tapi mengembalikan nilai ~0 karena node collision; forward-only nearest search (#28-P2), EWMA speed smoothing (#28-P1), dan dead-band push (#28-P3) semuanya menyasar masalah berbeda (posisi kurir real-time & noise ETA), bukan resolusi snapping saat penentuan urutan stop. #29 adalah bug independen yang baru ditemukan.
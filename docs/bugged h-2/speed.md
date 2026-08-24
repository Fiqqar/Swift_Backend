# Bug: Kecepatan (`speed_kmh`) Hardcoded dari Env di `navigation.py`

**File:** `app/services/navigation.py`
**Fungsi:** `_leg_metrics()` — closure di dalam `maybe_reorder_stops_on_off_route()`

---

## Kode Bermasalah

```python
def _leg_metrics(coords):
    """Jarak (km) + estimasi durasi (menit) dari decoded coords."""
    dist_m = sum(
        haversine_distance(coords[i], coords[i + 1])
        for i in range(len(coords) - 1))
    speed_kmh = _env_float("MODE_AVG_SPEED_KMH",
                           _env_float("DEFAULT_SPEED_KMH", 40.0))
    dur_min = (dist_m / (speed_kmh / 3.6) / 60.0) if speed_kmh > 0 else 0.0
    return round(dist_m / 1000.0, 2), round(dur_min, 1)
```

Fungsi ini dipanggil dua kali di dalam blok pengiriman notifikasi WS `stops_reordered`, untuk menghitung `distance_km` dan `duration_mins` yang dikirim ke client (mobile) setelah stop kurir di-reorder.

---

## Masalah

`speed_kmh` diambil murni dari environment variable global (`MODE_AVG_SPEED_KMH` → fallback `DEFAULT_SPEED_KMH` → fallback `40.0`). Fungsi ini **tidak melihat state kurir sama sekali** — padahal `session` (parameter `NavSession`) sudah tersedia penuh di scope pemanggilnya.

Yang seharusnya jadi sumber kecepatan, tapi tidak dipakai:

- **`session.mode`** — motorcycle / car / bicycle. Semua mode saat ini dihitung dengan kecepatan yang sama.
- **`session.last_position.get("speed")`** — kecepatan real-time kurir dari GPS, yang di dua fungsi lain dalam file yang sama justru jadi sumber utama:

```python
# remaining_progress()
speed_kmh = 40.0
if session.last_position and session.last_position.get("speed"):
    try:
        speed_kmh = float(session.last_position["speed"])
    except (TypeError, ValueError):
        speed_kmh = 40.0

# compute_reroute()
speed_kmh = 40.0
if session.last_position and session.last_position.get("speed"):
    try:
        speed_kmh = float(session.last_position["speed"])
    except (TypeError, ValueError):
        speed_kmh = 40.0
```

Jadi ada **3 fungsi, 3 sumber kecepatan berbeda** untuk kasus yang secara konsep sama (estimasi durasi tempuh):

| Fungsi | Sumber `speed_kmh` |
|---|---|
| `remaining_progress()` | `session.last_position["speed"]` → fallback `40.0` (hardcoded) |
| `compute_reroute()` | `session.last_position["speed"]` → fallback `40.0` (hardcoded) |
| `_leg_metrics()` | env var `MODE_AVG_SPEED_KMH` → env var `DEFAULT_SPEED_KMH` → `40.0` |

Catatan tambahan: nama env var `MODE_AVG_SPEED_KMH` menyiratkan "kecepatan rata-rata per mode transportasi", tapi implementasinya cuma satu angka global — tidak pernah di-index berdasarkan `session.mode`.

---

## Dampak

1. **ETA/durasi tidak akurat** — setelah reorder, `duration_mins` dan `estimated_time_seconds` yang dikirim ke mobile dihitung dari asumsi kecepatan generik, bukan kecepatan aktual kurir.
2. **Tidak konsisten antar event** — ETA yang ditampilkan setelah `stops_reordered` bisa berbeda signifikan dari ETA di event lain (`progress`, `auto_rerouted`) untuk kurir yang sama pada waktu yang hampir bersamaan, karena sumber kecepatan berbeda.
3. **Semua mode kendaraan diperlakukan sama** — kurir `bicycle` dan kurir `car` mendapat estimasi durasi identik untuk jarak yang sama, padahal kecepatan riil keduanya jauh berbeda.
4. **Env var bisa jadi false sense of granularity** — operasional mungkin mengira mengatur `MODE_AVG_SPEED_KMH` akan berlaku "per mode", padahal berlaku global untuk semua mode & semua kurir.

---

## Rekomendasi Perbaikan

### 1. Buat satu helper terpusat untuk resolve kecepatan
Konsolidasikan logika yang sudah ada di `remaining_progress` dan `compute_reroute` ke satu fungsi, lalu reuse di `_leg_metrics`:

```python
def _resolve_speed_kmh(session: "NavSession") -> float:
    """Prioritas: kecepatan real-time kurir > default per-mode > default global."""
    if session.last_position and session.last_position.get("speed"):
        try:
            v = float(session.last_position["speed"])
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    # fallback default per-mode
    mode_defaults = {
        "motorcycle": _env_float("SPEED_KMH_MOTORCYCLE", 35.0),
        "car": _env_float("SPEED_KMH_CAR", 40.0),
        "bicycle": _env_float("SPEED_KMH_BICYCLE", 15.0),
        "on_foot": _env_float("SPEED_KMH_ON_FOOT", 5.0),
    }
    return mode_defaults.get(session.mode, _env_float("DEFAULT_SPEED_KMH", 40.0))
```

### 2. Ubah `_leg_metrics` menerima speed yang sudah di-resolve
```python
def _leg_metrics(coords, speed_kmh):
    dist_m = sum(
        haversine_distance(coords[i], coords[i + 1])
        for i in range(len(coords) - 1))
    dur_min = (dist_m / (speed_kmh / 3.6) / 60.0) if speed_kmh > 0 else 0.0
    return round(dist_m / 1000.0, 2), round(dur_min, 1)
```
Panggil dengan `speed_kmh = _resolve_speed_kmh(session)` sebelum loop pembuatan `legs_with_geometry`.

### 3. Ganti pemanggilan `speed_kmh = 40.0 ... if session.last_position...` di `remaining_progress()` dan `compute_reroute()`
dengan pemanggilan `_resolve_speed_kmh(session)` yang sama, supaya satu-satunya sumber logika kecepatan ada di satu tempat — bukan tersebar & berpotensi divergen.

---

## Area yang Perlu Dicek Sebelum Deploy

- Apakah `session.last_position["speed"]` konsisten diisi oleh semua entry point (WS `position_update`, dsb.) — kalau sering `None`, fallback per-mode jadi krusial dan perlu angka default yang realistis (bukan asumsi 40 km/jam untuk semua).
- Apakah ada requirement produk soal kecepatan berbeda per kota/kondisi jalan (bukan cuma per mode kendaraan) — kalau ada, `_resolve_speed_kmh` bisa diperluas menerima parameter tambahan.
- Pastikan unit test untuk `maybe_reorder_stops_on_off_route` (khususnya assertion `duration_mins`/`estimated_time_seconds`) di-update mengikuti sumber speed yang baru, supaya test tidak diam-diam lolos dengan asumsi lama.
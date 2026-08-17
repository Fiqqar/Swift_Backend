# Test Case: RAG Berita Publik (Gemini Grounding → Penalti Rute)

Runbook manual untuk menguji **RAG berita publik**: berita gangguan lalu lintas
(penutupan jalan, banjir, perbaikan jalan, dll) dari web diambil via **Gemini
Grounding (Google Search)**, disimpan dalam **in-memory vector store per kota**,
lalu saat reroute berita yang relevan di koridor rute dievaluasi dan di-snap ke
edge graf sebagai penalti tambahan.

```
Alur end-to-end (HTTP + WS) — panah = arah message / alur internal:

[1] INGESTION (periodik, rag_ingestion_worker tiap RAG_NEWS_INGEST_INTERVAL_S):
    prompt kota → Gemini Grounding (Google Search) → JSON array berita
    (title, summary, lat, lng, radius_m, severity) → text-embedding-004
    → simpan {items, vectors} di Redis `rag:news:<kota>` (TTL RAG_NEWS_CACHE_TTL_S)
    → seed in-memory store per kota.

[2] ROUTING (navigation aktif, tiap siklus reroute):
    compute_reroute() → retrieve_and_evaluate_road_incidents(polyline coords):
    ├─ _detect_city_from_coords(coords) → reverse geocode Nominatim
    │    (cache Redis `reverse:<grid>`, TTL RAG_NEWS_CITY_CACHE_TTL_S)
    │    └─ nonaktif/gagal → fallback RAG_NEWS_CITY statis
    ├─ _ensure_city_news(redis, city) → bila store kosong/kadaluarsa,
    │    spawn ingestion on-demand (fire-and-forget) → berita siap siklus berikutnya
    ├─ _retrieve_relevant(coords) → embed query koridor → vector search top-K
    │    → filter item yang jaraknya ke polyline ≤ radius_m
    ├─ _evaluate_penalties(coords, items) → Gemini evaluasi per kejadian
    │    {lat, lng, radius_m, penalty_multiplier, reason} (fallback: multiplier
    │    dari severity; cache in-memory _eval_cache per item TTL RAG_NEWS_CACHE_TTL_S)
    └─ _snap_incidents_to_edges(evaluated, app) → {edge_id: multiplier}
         → digabung ke traffic_penalties (ambil max) di compute_reroute
```

Semua operasi **opsional dan non-fatal**: tanpa `RAG_NEWS_ENABLED` / tanpa
`GEMINI_API_KEY` / kegagalan apa pun → kembali ke routing biasa tanpa penalti
berita. Reroute **tidak pernah diblok** oleh RAG.

Base URL: `http://localhost:8000` • WS URL: `ws://localhost:8000`

---

## Konfigurasi & Log yang Relevan (pahami sebelum tes)

### Variabel env (`app/services/rag_traffic.py`)

| Variabel | Default | Keterangan |
|---|---|---|
| `RAG_NEWS_ENABLED` | `0` | 1 = aktifkan RAG berita. |
| `RAG_NEWS_CITY` | (kosong) | Kota statis untuk ingestion periodik (mis. `Kudus`). Worker di-skip bila kosong. |
| `RAG_NEWS_INGEST_INTERVAL_S` | `3600` | Interval ingestion & ambang on-demand (min 60). |
| `RAG_NEWS_TOP_K` | `5` | Jumlah berita teratas hasil pencarian vektor. |
| `RAG_NEWS_CACHE_TTL_S` | `1800` | TTL cache Redis berita + cache evaluasi in-memory. |
| `RAG_NEWS_MAX_PENALTY` | `3.0` | Batas atas multiplier penalti per edge. |
| `RAG_NEWS_TIMEOUT_S` | `2.0` | Batas waktu satu panggilan Grounding/embedding. |
| `RAG_NEWS_EMBED_MODEL` | `text-embedding-004` | Model embedding. |
| `RAG_NEWS_DYNAMIC_CITY` | `1` | 1 = deteksi kota dari koordinat rute (reverse geocode). |
| `RAG_NEWS_CITY_CACHE_TTL_S` | `3600` | TTL cache reverse geocode kota. |

Dependensi eksternal geocode (`app/services/geocode.py`): `NOMINATIM_URL`
(default `https://nominatim.openstreetmap.org`), `GEOCODE_RATE_DELAY` (default `1.0`),
`GEOCODE_TIMEOUT` (default `8.0`), `GEOCODE_USER_AGENT`.

### Log utama

| Log | Arti |
|---|---|
| `[RAG] Ingestion berita aktif untuk kota 'Kudus' (interval 3600s).` | Worker periodik jalan. |
| `[RAG] Pakai cache berita rag:news:kudus (N item).` | Cache Redis valid, tanpa Grounding. |
| `[RAG] Store berita diperbarui: N item.` | Ingestion sukses, store di-seed. |
| `[RAG] Berita kota 'X' siap: N item.` | Ingestion on-demand kota dinamis selesai. |
| `[RAG] Reverse geocode kota gagal: ...` | Nominatim gagal → fallback kota statis. |
| `[RAG] Tidak ada graf untuk snap; penalti berita diabaikan.` | Graf belum tersedia → penalti di-skip (aman). |
| `[RAG] Grounding timeout ...` / `[RAG] Evaluasi gagal; fallback severity ...` | Fallback deterministik. |
| `[NAV] Gagal ambil penalti berita (RAG): ...` | `compute_reroute` menangkap kegagalan (rute tetap jalan). |

> **PENTING**: env RAG dibaca saat *import* (`app/services/rag_traffic.py`).
> Setelah mengubah nilai, **restart app**.

## 0. Prasyarat & Setup

1. `.env`:

   ```ini
   ENABLE_LIVE_NAVIGATION=1
   AUTO_REROUTE=1
   REDIS_HOST=redis
   REDIS_PORT=6379

   # --- RAG Berita Publik ---
   RAG_NEWS_ENABLED=1
   RAG_NEWS_CITY=Kudus
   RAG_NEWS_INGEST_INTERVAL_S=3600
   RAG_NEWS_TOP_K=5
   RAG_NEWS_CACHE_TTL_S=1800
   RAG_NEWS_MAX_PENALTY=3.0
   RAG_NEWS_TIMEOUT_S=2.0
   RAG_NEWS_DYNAMIC_CITY=1
   RAG_NEWS_CITY_CACHE_TTL_S=3600

   # --- Gemini (Grounding + embedding) ---
   GEMINI_API_KEY=<API_KEY>
   GEMINI_MODEL=gemini-2.5-flash

   # --- Reverse geocode kota (dynamic city) ---
   # NOMINATIM_URL=https://nominatim.openstreetmap.org
   # GEOCODE_RATE_DELAY=1.0
   ```

2. Jalankan stack: `docker compose up --build`.

3. Verifikasi startup log agent:

   ```
   [RAG] Ingestion berita aktif untuk kota 'Kudus' (interval 3600s).
   ```

   Dan setelah siklus pertama (~saat interval/setelah restart):

   ```
   [RAG] Store berita diperbarui: N item.
   ```

4. Verifikasi cache Redis:

   ```bash
   docker compose exec redis redis-cli TTL rag:news:kudus   # > 0
   docker compose exec redis redis-cli GET rag:news:kudus   # JSON {items, vectors}
   ```

## 1. Seed & Buat Rute Aktif (agar reroute berjalan)

1. Seed data + login:

   ```bash
   uv run python scripts/seed_kurir.py
   uv run python scripts/seed_hub.py
   uv run python scripts/seed_paket.py
   ```

2. Buat rute multi-leg (`POST /api/v1/pathfinding/find-optimized-delivery-route`
   dengan Bearer token), lalu hubungkan `WS /api/v1/ws/navigation?token=...`,
   `start_navigation`, dan kirim `location_update` ≥ 3 dtk (persis seperti
   runbook `runbook_ai_reroute_agent.md`). Tanpa ini, `navigation_worker` tidak
   melakukan reroute sehingga jalur RAG tidak terpanggil.

## 2. Skenario A — Ingestion Periodik (Kota Statis)

1. `RAG_NEWS_CITY=Kudus`, `RAG_NEWS_ENABLED=1`, `RAG_NEWS_DYNAMIC_CITY=0`
   (matikan deteksi dinamis dulu untuk isolasi), restart app.

**Kriteria lulus:**

- Log `[RAG] Ingestion berita aktif untuk kota 'Kudus' ...`.
- Setelah satu siklus: `[RAG] Store berita diperbarui: N item.` (N > 0 bila ada
  berita; bisa 0 saat tidak ada berita gangguan — valid).
- `docker compose exec redis redis-cli GET rag:news:kudus` berisi JSON dengan
  `items` (setiap item: `title`, `summary`, `lat`, `lng`, `radius_m`, `severity`)
  dan `vectors` (array vektor, panjang = panjang items).
- Siklus berikutnya (dalam TTL) memakai cache: log `[RAG] Pakai cache berita
  rag:news:kudus (N item).` — **tanpa** panggilan Grounding.

## 3. Skenario B — Kota Dinamis (Reverse Geocode)

Aktifkan `RAG_NEWS_DYNAMIC_CITY=1`, restart. Saat kurir navigasi di area Kudus
(polyline rute dimulai di sekitar `-6.8048, 110.8385`), tunggu satu siklus reroute.

**Kriteria lulus:**

- Log `[RAG] Berita kota 'Kudus' siap: N item.` (ingestion on-demand kota yang
  terdeteksi dari koordinat).
- Cache reverse geocode terisi:

  ```bash
  docker compose exec redis redis-cli KEYS 'reverse:*'
  ```

  (satu key per grid koordinat; `_grid_key` = `lat_lon` 3-desimal, lihat
  `test_geocode.py::test_grid_key`). Value = kota (`"Kudus"`).

- Kota dinamis mengikuti lokasi: jalankan kurir di kota lain (mis. `-6.9175, 107.6191`
  Bandung) → log `[RAG] Berita kota 'Bandung' siap: N item.` dan store kota Bandung
  terisi (query Grounding menyesuaikan).

> Nominatim butuh `GEOCODE_RATE_DELAY` (default 1 dtk) agar tidak kena rate-limit.
> Cache per-grid mencegah panggilan berulang di koordinat yang sama.

## 4. Skenario C — Fallback Kota Statis

Matikan deteksi dinamis dengan salah satu cara:

1. `RAG_NEWS_DYNAMIC_CITY=0`, restart → `_detect_city_from_coords` selalu
   mengembalikan `RAG_NEWS_CITY`.
2. Atau biarkan `RAG_NEWS_DYNAMIC_CITY=1` tapi set `NOMINATIM_URL` ke host yang
   mati / matikan jaringan Nominatim → log `[RAG] Reverse geocode kota gagal: ...`
   → fallback `RAG_NEWS_CITY`.

**Kriteria lulus:** store yang dipakai adalah kota statis; tidak ada error yang
menghentikan reroute; `[NAV] Gagal ambil penalti berita (RAG)` tidak muncul
(rute tetap dihitung).

## 5. Skenario D — Penalti Tersnap & Digabung (E2E)

1. Pastikan store berisi berita untuk kota aktif (Skenario A/B) dan ada berita
   dengan `severity` ≥ MEDIUM / radius yang menyentuh koridor rute kurir.
2. Picu reroute (deviasi > `OFF_ROUTE_THRESHOLD_M` atau biarkan `navigation_worker`
   menemukan rute lebih cepat).

**Kriteria lulus:**

- Tidak ada log error RAG; `_snap_incidents_to_edges` menghasilkan penalti
  `{edge_id: multiplier}` (1.0 < multiplier ≤ `RAG_NEWS_MAX_PENALTY`; `BLOCKING`
  → `inf` = jalan dianggap tertutup total).
- `compute_reroute` menggabungkan penalti berita ke `traffic_penalties` (ambil max).
- Kurir menerima event `auto_rerouted`/`reroute_available` seperti biasa — rute
  baru sudah menghindari edge berpenalti.
- Reroute berulang dalam TTL tidak memanggil Gemini lagi (cache `_eval_cache`
  per item; bisa diverifikasi lewat unit test, lihat Bagian 8).

> Untuk **verifikasi deterministik** tanpa bergantung isi berita nyata, jalankan
> unit test snap & merge (Bagian 8) yang menanam item buatan langsung di store.

## 6. Skenario E — Nonaktif = Tidak Ada Efek

1. `RAG_NEWS_ENABLED=0` (atau kosongkan `GEMINI_API_KEY`), restart.
2. Jalankan reroute di area yang sama.

**Kriteria lulus:**

- Tidak ada log `[RAG]` sama sekali (kecuali cache read yang aman).
- Rute/penalti identik dengan kondisi tanpa RAG; tidak ada overhead panggilan AI.
- `rag_enabled()` = `False` → `compute_reroute` melewati blok berita sepenuhnya.

## 7. Skenario F — Kegagalan Non-Fatal

Set `GEMINI_API_KEY=INVALID_KEY` (sengaja salah), restart, jalankan reroute.

**Kriteria lulus:**

- Log warning RAG muncul (`[RAG] Grounding gagal: ...` / timeout), **tapi**
  `compute_reroute` tetap menghasilkan rute (log `[NAV] Gagal ambil penalti
  berita (RAG): ...` adalah jalur aman terakhir).
- Event WS reroute tetap terkirim; tidak ada crash worker.
- Store tidak di-seed (tidak ada item) → `_store.size() == 0` → `{}` penalti.

## 8. Uji Otomatis (Regression)

```bash
uv run python -m pytest tests/test_rag_traffic.py tests/test_geocode.py -q
```

> Gunakan `python -m pytest` (bukan `uv run pytest`) agar `app` dapat di-import.

Cakupan (33 test RAG + 8 test geocode): cosine similarity, multiplier severity,
parse item/evaluasi (code fence, noise), store per-kota & search, gate
`rag_enabled`, worker nonaktif / tanpa kota, ingestion Grounding & cache Redis,
deteksi kota dinamis (reverse + fallback statis + nonaktif), `_ensure_city_news`
spawn-once, retrieve filter jarak, evaluasi (fallback severity tanpa key, pakai
Gemini, cache per item), snap-to-edge + skip closure, disabled/no-key/empty-store
→ `{}`.

Setelahnya regresi reroute terkait:

```bash
uv run python -m pytest tests/test_navigation.py tests/test_route_options.py -q
```

## 9. Troubleshooting

| Gejala | Penyebab & Solusi |
|---|---|
| Log `[RAG] Ingestion berita aktif` tidak muncul | `RAG_NEWS_ENABLED=0`, `RAG_NEWS_CITY` kosong (worker return), atau env dibaca saat import — restart. |
| `[RAG] RAG_NEWS_CITY kosong; ingestion berita nonaktif.` | `RAG_NEWS_CITY` tidak diisi; isi atau aktifkan dynamic city. |
| `[RAG] Store berita diperbarui: 0 item.` | Grounding sukses tapi tidak ada berita gangguan untuk kota tsb — valid, coba kota lain / teks prompt. |
| Grounding gagal / timeout | `GEMINI_API_KEY` invalid/kosong, model lambat (`RAG_NEWS_TIMEOUT_S`), kuota. Cek `[AI] Rate limit key 1, memakai key cadangan.` bila `GEMINI_API_KEY_2` diisi. |
| Kota dinamis selalu fallback statis | Nominatim gagal/rate-limit (`GEOCODE_RATE_DELAY` kecil), atau koordinat di luar area. Cek log `[RAG] Reverse geocode kota gagal`. |
| Reverse geocode memanggil berulang | Cache `reverse:<grid>` hilang (Redis down) — dengan Redis normal, satu panggilan per grid per `RAG_NEWS_CITY_CACHE_TTL_S`. |
| Penalti tidak tersnap (`[RAG] Tidak ada graf untuk snap ...`) | Graf base/region belum dimuat atau lokasi kosong. `uv run python scripts/prewarm_route.py <lat1> <lon1> <lat2> <lon2>` lalu ulangi. |
| Berita tidak berdampak walau ada di kota | `_retrieve_relevant` memfilter jarak ke polyline ≤ `radius_m` item; top-K kecil (`RAG_NEWS_TOP_K`), atau item jauh dari koridor. Naikkan `RAG_NEWS_TOP_K` / `RAG_NEWS_MAX_PENALTY`. |
| Reroute lambat | Siklus pertama menyertakan ingestion on-demand (fire-and-forget) + embedding (bisa beberapa detik); siklus berikutnya memakai cache. |
| Penalti `inf` (jalan tertutup) muncul tak terduga | Berita severity `BLOCKING` → multiplier `inf`. Atur `RAG_NEWS_MAX_PENALTY` (tidak memengaruhi `inf`; hanya membatasi nilai 1.0..max). |

## 10. Kriteria Lulus Keseluruhan

- [ ] Ingestion periodik kota statis: store ter-seed + cache Redis `rag:news:<kota>` berisi `{items, vectors}`; siklus berikutnya memakai cache (tanpa Grounding).
- [ ] Kota dinamis: log `[RAG] Berita kota '<kota-terdeteksi>' siap: N item.` + cache `reverse:*` terisi; kota mengikuti koordinat kurir.
- [ ] Fallback: `RAG_NEWS_DYNAMIC_CITY=0` atau Nominatim gagal → memakai `RAG_NEWS_CITY`, tanpa error mematikan.
- [ ] E2E reroute: penalti berita tersnap ke edge dan digabung di `compute_reroute`; event WS tetap terkirim; rute baru menghindari edge berpenalti.
- [ ] Nonaktif (`RAG_NEWS_ENABLED=0` / tanpa key): tidak ada efek RAG sama sekali.
- [ ] Kegagalan (`GEMINI_API_KEY` invalid): hanya warning, routing tetap berjalan.
- [ ] Regresi: `uv run python -m pytest tests/test_rag_traffic.py tests/test_geocode.py tests/test_navigation.py -q` → semua lolos.
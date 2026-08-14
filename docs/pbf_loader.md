# Task: Refactor Graph Loader untuk Membaca File Local .osm.pbf via Pyrosm

## Konteks & Tujuan
Saat ini `app/services/pathfinding/graph_loader.py` mengambil data peta dari Overpass API via `osmnx`. Kita ingin menambahkan kemampuan untuk membaca file `.osm.pbf` secara lokal menggunakan library `pyrosm` agar tidak bergantung pada koneksi internet/Overpass, menghilangkan masalah disconnect, dan mempercepat loading graf.

## Persyaratan Teknis

1. **Struktur Directory & Konfigurasi Env**
   - Buat direktori `data/pbf/` di root project untuk menyimpan file `.osm.pbf`.
   - Tambahkan env var baru di `.env.example`:
     `OSM_PBF_FILE_PATH=data/pbf/region.osm.pbf`
     `USE_LOCAL_PBF=1` (1 untuk lokal PBF, 0 untuk Overpass API)

2. **Integrasi Pyrosm di `graph_loader.py`**
   - Buat fungsi `load_graph_from_pbf(pbf_path: str, bbox: tuple = None) -> nx.MultiDiGraph`.
   - Gunakan `pyrosm.OSM(pbf_path, bounding_box=bbox)` untuk membaca network `driving`.
   - Konversi hasil Pyrosm ke NetworkX graph yang kompatibel dengan struktur `PathGraph` (`preprocess.py`).
   - Pastikan atribut node (`y` -> lat, `x` -> lon) dan edge (`length`, `oneway`, `maxspeed`) terpetakan dengan benar persis seperti format `osmnx`.

3. **Fallback Chain di `load_graph_covering`**
   Atur hirarki pemuatan graf sebagai berikut:
   1. Cek Memory / Disk Cache (Pickle `pg_v2_...`).
   2. Jika Cache Miss & `USE_LOCAL_PBF=1` -> Read dari local `.osm.pbf` file.
   3. Jika PBF gagal / `USE_LOCAL_PBF=0` -> Fallback ke Overpass API (`osmnx`).
   4. Jika Overpass gagal -> Fallback ke Demo Grid 6x6.

## Instruksi Pembuatan Kode
1. Update `pyproject.toml` / `requirements.txt` dengan menambah `pyrosm`.
2. Refactor `app/services/pathfinding/graph_loader.py` untuk mengimplementasikan fungsi loader PBF di atas dengan error handling yang aman (*graceful degradation*).
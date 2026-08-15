# Task: Enable Dynamic Routing Worldwide / Java-Wide (Multi-Region Support)

## Context
Aplikasi saat ini terkunci di area Monas. Kita ingin rute dapat dicari DI MANA SAJA (terutama seluruh Pulau Jawa: Jakarta, Semarang, Kudus, dll) secara dinamis tanpa bergantung pada titik awal Monas.

## Technical Requirements for Agent

1. **Multi-Anchor / Dynamic Warm-up (`app/main.py`):**
   - Hapus keterikatan hardcoded Bbox Monas saja pada lifespan startup.
   - Tambahkan daftar anchor warm-up untuk area operasional utama (misal: Jakarta, Semarang, Kudus) atau buat background warm-up task agar server langsung siap melayani request di kota-kota tersebut.

2. **Dynamic Bbox Extraction by Request Coordinates (`graph_loader.py`):**
   - Jika koordinat Request (`origin` dan `destination`) berada di luar RAM `app.state.path_graph`:
     a. Hitung Bbox penutup yang melingkupi origin + destination (ditambah padding margin ~0.05 - 0.1 derajat).
     b. Cek apakah Disk Cache `.pkl` untuk Bbox/Grid tersebut sudah ada di folder `cache/`.
     c. Jika HIT: Langsung `pickle.load()` dari disk cache.
     d. Jika MISS: Gunakan `osmium` untuk memotong Bbox tersebut dari `data/pbf/java-latest.osm.pbf`, buat `PathGraph`, lalu SERTA-MERTA simpan ke disk cache `.pkl` agar request berikutnya instant.

3. **Routing Execution:**
   - Setelah `PathGraph` area mana pun berhasil di-load (baik dari RAM/Disk Cache), eksekusi routing MURNI menggunakan `_rust_engine.RustGraph.route` (Bidirectional Dijkstra, CH OFF).

## Verification
1. Coba request rute di Kudus (misal: Alun-alun Kudus ke Menara Kudus).
2. Coba request rute di Semarang (misal: Simpang Lima ke Kota Lama).
3. Pastikan polyline rute muncul dengan benar dan file `.pkl` baru otomatis bertambah di folder `cache/`.
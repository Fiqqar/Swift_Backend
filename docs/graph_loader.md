# Task: Implementasi Adaptive Multi-Level Pathfinding (Local vs Highway Network Filtering)

## Konteks & Tujuan
Saat ini pemuatan graf dari `.osm.pbf` mengambil seluruh detail jalan tanpa membedakan jarak. Akibatnya, query rute jarak menengah/jauh (seperti Kudus–Semarang atau Kudus/Bali–Jakarta) memuat puluhan ribu jalan kecil yang tidak relevan, menyebabkan latency tinggi dan memory spike. 

Kita ingin membuat alur pemuatan graf dan routing yang **adaptif berdasarkan jarak lurus Haversine ($d$)** antara origin dan destination.

---

## 1. Arsitektur Adaptive Strategy

Terapkan logika tingkatan (*level*) pemuatan graf berikut:

1. **Short-Distance / Local ($d \le 20\text{ km}$):**
   - **Filter Jalan:** Semua jenis jalan kendaraan (`driving` / `all`).
   - **Bbox:** Dinamis `center + radius` dengan padding presisi (~0.02°).
   - **Tujuan:** Menjaga akurasi navigasi di dalam kota hingga jalan gang/perumahan.

2. **Medium-Distance ($20\text{ km} < d \le 100\text{ km}$):**
   - **Filter Jalan:** Hilangkan jalan perumahan/akses lokal kecil (`service`, `residential`, `living_street`, `unclassified`).
   - **Bbox:** Bounding Box persegi panjang dari `(min_lat, min_lon)` ke `(max_lat, max_lon)` + padding margin (~0.05°).
   - **Tujuan:** Mempercepat parsing PBF dan mengurangi jumlah node hingga 60-70%.

3. **Long-Distance / Intercity ($d > 100\text{ km}$):**
   - **Filter Jalan:** HANYA ambil hirarki jalan utama (`motorway`, `motorway_link`, `trunk`, `trunk_link`, `primary`, `primary_link`).
   - **Strategy:** Gunakan **Pre-loaded Highway Network** dari PBF (atau filter Bbox ketat + padding ~0.1°) + hubungkan origin/destination ke node jalan utama terdekat (*snap to highway*).
   - **Tujuan:** Memangkas 95%+ node non-esensial sehingga pathfinding di Rust (Bidirectional Dijkstra / CH) selesai dalam hitungan milidetik.

---

## 2. Instruksi Refactoring Kode

### A. Update `app/services/pathfinding/graph_loader.py`
- Tambahkan helper untuk menentukan filter `highway` berdasarkan jarak haversine origin-destination:
  ```python
  def get_highway_filter(distance_km: float) -> str | list[str] | None:
      if distance_km <= 20:
          return None  # Ambil semua jalan kendaraan
      elif distance_km <= 100:
          # Exclude jalan kecil/gang
          return "driving" # dengan kustomisasi filter pyrosm
      else:
          # Hanya jalan nasional & tol
          return ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"]
Tolong buatkan pipeline RAG (Retrieval-Augmented Generation) yang mengombinasikan pencarian berita publik/scraping internet real-time menggunakan Gemini Grounding dengan Vector Store lokal (`app/services/rag_traffic.py`).

**Model Provider Directive:**
Gunakan Google GenAI SDK (`google-genai`) dengan model `gemini-2.5-flash` dan fitur `GoogleSearch` tool (Grounding) untuk pengayaan data lalu lintas.

**Spesifikasi Detail:**
1. **News Scraper & Vector Ingestion:**
   - Gunakan `gemini-2.5-flash` dengan pemicu grounding Google Search untuk mencari berita lalu lintas lokal terkini (misal: "penutupan jalan banjir perbaikan jalan [nama_kota]").
   - Simpan hasil ekstraksi berita tersebut ke dalam Vector Database (ChromaDB / InMemory Vector Store) menggunakan model embedding Gemini (`text-embedding-004`).
2. **Spatial Knowledge Retrieval & Cost Penalty:**
   - Buat fungsi `retrieve_and_evaluate_road_incidents(polyline_coords: list)` yang mengambil konteks berita relevan berdasarkan lokasi rute.
   - Minta Gemini mengembalikan Structured JSON berisi penalti segmen jalan: `[{"street_name": "Jl. Pemuda", "penalty_multiplier": 2.5, "reason": "Banjir 30cm"}]`.
   - Suntikkan `penalty_multiplier` tersebut ke dalam bobot graf A*/Dijkstra backend saat pemicu `compute_reroute` dijalankan.
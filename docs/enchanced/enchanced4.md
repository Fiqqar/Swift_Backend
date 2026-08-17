Tolong buatkan AI Agent background worker yang memindai endpoint laporan internal driver dan memicu proactive auto-reroute via WebSocket (`app/services/internal_report_agent.py`).

**Model Provider Directive:**
Gunakan Google GenAI SDK (`google-genai`) dengan model `gemini-2.5-flash`.

**Spesifikasi Detail:**
1. **Internal Endpoint Scanner:**
   - Buat background worker `scan_internal_driver_reports()` yang memindai laporan baru dari driver lain (misal: "Pohon tumbang di KM 12", "Jalan berlubang parah di dekat pasar").
2. **Severity & Impact Reasoning:**
   - AI Agent mengirim teks laporan ke Gemini untuk diklasifikasikan ke dalam severity level (`LOW`, `MEDIUM`, `HIGH`, `BLOCKING`) dan dicocokkan lokasinya dengan rute aktif para kurir di `NavRegistry`.
3. **Proactive WebSocket Push:**
   - Jika laporan berdampak langsung (severity `HIGH`/`BLOCKING`), Agent memanggil `compute_reroute()`, menyusun pesan narasi verbal ("Rute diubah otomatis: Laporan driver lain menyebutkan jalan ditutup akibat pohon tumbang"), lalu me-push event `auto_rerouted` ke WebSocket kurir terkait.
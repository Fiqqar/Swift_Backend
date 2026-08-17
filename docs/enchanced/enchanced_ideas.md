1. Real-time Navigation & Auto-Rerouting Engine (WebSocket)
Modul navigasi aktif berbasis WebSocket untuk memandu pengemudi secara presisi tanpa jeda (low latency).

Off-Route Detection: Menghitung jarak proyeksi posisi GPS pengemudi terhadap garis polyline rute (OFF_ROUTE_THRESHOLD_M = 40m). Jika melenceng, sistem memicu kalkulasi ulang rute otomatis dari lokasi terkini.

Dynamic Traffic Rerouting: Worker background (navigation_worker) mengevaluasi kondisi traffic setiap 30 detik. Jika ada jalur alternatif yang memangkas waktu minimal 120 detik, sistem me-apply rute baru secara otomatis (AUTO_REROUTE=1).

Histeresis & Cooldown: Mengunci jeda minimal 30 detik antar-reroute untuk mencegah rute berganti-ganti terlalu cepat (flickering).

Multi-Leg Support: Navigasi otomatis berpindah ke leg/titik tujuan berikutnya setelah pengemudi menyelesaikan bukti pengiriman (Proof of Delivery).

2. Dynamic Pathfinding & Multi-Route Optimization
Modul kalkulasi graf jalan untuk menghasilkan rute efisien berbasis bobot real-time.

Alternative Routes (Yen's K-Shortest Path): Menyediakan 1–3 opsi rute alternatif yang efisien namun melewati jalan berbeda (tumpang-tindih rute max 70%).

Multi-Stop TSP/VRP Solver: Mengurutkan rantai pengiriman paket (multi-stop delivery) agar menempuh jarak dan waktu total paling minimal.

State Caching (Redis): Menyimpan snapshot rute (driver:nav:{kurir_id}) di Redis dengan TTL 3600 detik agar status navigasi tidak hilang jika aplikasi restart.

3. RAG Berita Kondisi Jalan (External Public Data)
Penerapan Retrieval-Augmented Generation (RAG) untuk menyerap informasi insiden lalu lintas dari berita publik di internet.

Vector Data Ingestion: Scraper mengumpulkan artikel berita harian terkait penutupan jalan, banjir, perbaikan pipa, atau rekayasa lalu lintas ke dalam Vector Database.

Spatial Knowledge Retrieval: Saat rute dihitung, RAG menarik artikel yang relevan dengan koordinat jalan yang akan dilalui.

Automatic Cost Penalty: LLM membaca konteks berita tersebut lalu memberikan penalti bobot (cost penalty) pada segmen jalan terkait agar algoritma Dijkstra/A* mengalihkan rute.

4. AI Agent Memindai Laporan Internal (Internal Driver Data)
Penerapan Agentic Workflow independen yang memantau kondisi lapangan dari laporan sesama pengemudi.

Endpoint Scanning Worker: AI Agent secara otomatis memindai endpoint laporan internal (misal: laporan jalan berlubang, razia, atau genangan air).

Severity & Risk Reasoning: Agent menganalisis tingkat keparahan laporan bahasa alami.

Proactive WebSocket Push: Jika ada laporan berisiko tinggi di jalur aktif pengemudi, Agent memanggil fungsi compute_reroute dan mengirim instruksi pembaruan rute beserta notifikasi teks/suara ke UI pengemudi.
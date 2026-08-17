Tolong buatkan modul LLM Tool Calling untuk fitur Navigasi pada backend FastAPI saya (`app/services/ai_tools.py` & `app/services/ai_agent.py`).

**Model Provider Directive:**
Gunakan SDK resmi Google GenAI (`google-genai`) dengan model default `gemini-2.5-flash`.

**Spesifikasi Detail:**
1. **Tool Definition (`app/services/ai_tools.py`):**
   - Bungkus fungsi-fungsi backend eksisting menjadi Native Gemini Tools / Function Spec:
     - `find_route_tool`: Parameter `origin`, `destination`, `avoid_tolls`, `mode`.
     - `compute_reroute_tool`: Parameter `session_id`, `current_lat`, `current_lon`.
     - `get_remaining_progress_tool`: Parameter `session_id`, `lat`, `lon`.
2. **LLM Function Calling Handler (`app/services/ai_agent.py`):**
   - Buat fungsi async `handle_driver_voice_or_text_command(kurir_id: str, user_prompt: str)` yang menginisiasi `client = genai.Client()` dan memanggil model `gemini-2.5-flash` dengan menyertakan daftar tools.
   - Jika Gemini merespons dengan `function_calls`, eksekusi fungsi backend terkait secara otomatis, kembalikan hasilnya ke Gemini, dan sajikan respons akhir berupa teks natural + payload rute terstruktur ke WebSocket client.
   - Batasi `GEMINI_REROUTE_TIMEOUT_S = 2.0` detik dan berikan graceful fallback jika API mengalami error.
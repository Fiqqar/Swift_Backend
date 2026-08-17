# Postman Workspace (file-based)

Folder ini adalah workspace Postman berbasis file system (format YAML,
*Postman on the filesystem* / Local View). Koleksi, environment, dan globals
disimpan sebagai file agar bisa di-version-control dan di-review lewat git —
bukan diimpor sebagai satu file JSON.

Struktur:

```
postman/
├── collections/                 # koleksi (masing-masing punya .resources/definition.yaml)
│   ├── TEST2 API Engine/        # koleksi utama: HTTP + WebSocket
│   └── WebSocket/               # koleksi khusus koneksi WebSocket
├── environments/
│   └── Dev.environment.yaml     # environment lokal (baseUrl, bearerToken)
└── globals/
    └── workspace.globals.yaml   # variabel global
```

## Prasyarat

- **Postman desktop v12+** — fitur file-based/Local View tidak tersedia di
  web app (Postman for Web).
- Stack API berjalan (default `http://localhost:8000`):
  ```bash
  docker compose up --build
  ```
- Untuk request WebSocket: pastikan `.env` berisi `ENABLE_LIVE_TRACKING=1`
  (posisi kurir) dan/atau `ENABLE_LIVE_NAVIGATION=1` (navigasi), lalu restart
  app. Bila tidak aktif, koneksi ditutup kode `1008`.

## Menghubungkan folder ini ke Postman

1. Buka Postman desktop.
2. Buat/buka workspace.
3. Pada sidebar, klik **Files**, lalu **Open folder**.
4. Pilih folder ini (`postman/`) — atau root repo yang berisi folder
   `postman/` — lalu **Open**.
5. Postman membaca file YAML dan melakukan **sinkronisasi dua arah**:
   perubahan di app menulis ulang file, perubahan di repo tampil di app.
6. Edits hasil sync bisa di-commit ke git.

> Catatan: Postman hanya menghubungkan satu folder per workspace. Untuk
> memutuskan, gunakan *File viewer options > Disconnect*.

## Cara pakai

1. Pilih environment **Dev** dari dropdown environment.
   - `baseUrl` sudah default `http://localhost:8000` (bisa diubah sesuai
     server).
   - `bearerToken` masih kosong — diisi otomatis oleh script test pada
     request **Login kurir**.
2. Jalankan `POST /api/v1/auth/login` (koleksi TEST2 API Engine →
   `api/v1/auth/login`). Script `afterResponse` menyimpan `data.token` ke
   `bearerToken`.
3. Request lain yang butuh auth memakai `Authorization: Bearer {{bearerToken}}`
   secara otomatis (auth level koleksi).
4. Untuk WS:
   - **Driver Position** (`/api/v1/ws/driver/position`) — kirim pesan
     `{"type":"position","lat":..,"lon":..,"bearing":90,"speed":20}` untuk
     menyimpan posisi realtime ke Redis.
   - **Navigation** (`/api/v1/ws/navigation`) — kirim `start_navigation`
     lalu `location_update` untuk progress/off-route/auto-reroute.

## Variabel

| Variabel | Sumber | Keterangan |
|---|---|---|
| `baseUrl` | environment `Dev` / globals / koleksi | default `http://localhost:8000` |
| `bearerToken` | environment `Dev` | diisi otomatis oleh script login |

## Catatan: beda dengan "Export to Postman"

Swagger UI (tombol *Export to Postman* di `/docs`) mengunduh `postman_collection.json`
+ `dev.postman_environment.json` (format Collection v2.1 klasik) via
`app/services/postman_export.py`. Itu mekanisme **terpisah** — untuk diimpor
manual (Import → Upload files). Folder ini adalah workspace file-based yang
disinkronkan dengan Postman desktop; pilih salah satu sesuai kebutuhan.
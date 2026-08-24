# Plan: COD jadi Bool buat Flutter

## Masalah
- Backend sekarang pakai `string`: `pending` | `collected` | `remitted` | `not_applicable`
  - `app/api/v1/endpoints/shipments.py:36`
  - `app/models/shipment.py:20`
- Flutter cuma mau `true/false` buat nampilin badge COD.
- Temen lu gamau handle `not_applicable` dll.

## Kenapa gak bisa hapus string?
- `pending` -> belum bayar
- `collected` -> udah bayar pas `delivered` (`shipments.py:521`)
- `remitted` -> udah setor via `PATCH /shipments/{id}/cod` (`shipments.py:571`)
- `not_applicable` -> paket bukan COD (`shipments.py:189`)
- Kalo jadi bool tok, finance gak bisa tau duit udah di-collect apa udah di-remit. Test jebol `tests/test_delivery_flow.py:211`.

## Plan Terbaik: Hybrid (String tetap + tambah Bool)

Backend tetap simpan string, tapi API kasih bool tambahan buat Flutter.

### Response sekarang
```json
"cod": { "status": "pending", "amount": 50000 }
```

### Response baru (tambah 1 field)
```json
"cod": { "status": "pending", "is_cod": true, "amount": 50000 }
```
- `is_cod = status != "not_applicable"`
- Flutter tinggal pakai `is_cod`, gak perlu cek string lagi.
- `status` tetap ada biar gak breaking.

### File yang diubah
1. `app/api/v1/endpoints/shipments.py:76` - `_cod_dict()` tambah `"is_cod": s.cod_status != "not_applicable"`
2. `docs/API.md` + `docs/openapi.json:434` - update contoh response
3. Gak perlu migrasi DB

### Flutter pakainya gimana?
```dart
if (shipment['cod']['is_cod'] == true) {
  showBadge('COD Rp ${shipment['cod']['amount']}');
}
```
List `GET /shipments` dan `GET /shipments/{id}` udah include, gak perlu fetch lain.

### Opsi lain (ditolak)
- **Ganti full jadi bool**: harus hapus logic `collected/remitted`, migrasi DB, breaking. Jangan.
- **Flutter mapping sendiri**: `isCod = status != 'not_applicable'` - 0 perubahan backend, tapi temen lu gamau.

### Cara cek berhasil
- `GET /api/v1/shipments` -> cek ada `is_cod: true` untuk paket COD, `false` untuk non-COD
- `pytest tests/test_delivery_flow.py` tetap pass

---
## Status: DONE (2026-08-24)
- `app/api/v1/endpoints/shipments.py:76` tambah `is_cod`
- `docs/API.md` update contoh + catatan Flutter
- Verifikasi: `pending->is_cod true`, `not_applicable->false` OK, no DB migration, no breaking.

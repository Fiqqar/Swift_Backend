"""Endpoint upload gambar generic (auto-upload ke Cloudinary).

Alur: klien kirim file via multipart/form-data -> file divalidasi -> diunggah
ke Cloudinary -> respons berisi ``secure_url`` + ``public_id``. Klien yang
menyimpan URL tersebut ke database (endpoint ini tidak menyentuh DB).
"""

import time
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile

from app.api.v1.response import err, ok
from app.schemas.uploads import UploadImageResponse
from app.services.cloudinary_service import (
    CloudinaryNotConfiguredError,
    UPLOAD_FOLDER,
    cloudinary_configured,
    sniff_image_format,
    upload_image_meta,
)

router = APIRouter(tags=["Upload"])

_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_BYTES = 10 * 1024 * 1024


@router.post("/photo", status_code=201, summary="Upload gambar ke Cloudinary (multipart)",
             description=(
                 "Menerima satu file gambar (jpeg/png/webp, maks 10 MB) lalu "
                 "mengunggahnya (auto-upload) ke Cloudinary. Respons memuat "
                 "`secure_url` (URL publik) dan `public_id` — siap disimpan ke "
                 "database oleh klien.\n\n"
                 "- **Form wajib:** `file`.\n"
                 "- **Form opsional:** `folder` (override folder Cloudinary), "
                 "`public_id` (default otomatis `img_<timestamp>_<random>`).\n"
                 "- Isi file diverifikasi via magic bytes (Content-Type yang "
                 "dipalsukan ditolak).\n"
                 "- Error `400` file kosong/terlalu besar, `415` tipe/isi bukan "
                 "gambar JPG/PNG/WEBP, `503` Cloudinary belum dikonfigurasi, "
                 "`502` upload gagal."))
async def upload_photo(
    file: UploadFile = File(...),
    folder: str | None = Form(default=None),
    public_id: str | None = Form(default=None),
):
    """Unggah satu gambar ke Cloudinary lalu kembalikan URL publik + metadata."""
    if not cloudinary_configured():
        return err("Cloudinary belum dikonfigurasi (CLOUDINARY_* tidak terisi)", 503)

    ctype = (file.content_type or "").lower()
    if ctype and ctype not in _ALLOWED_TYPES:
        return err(f"Tipe file tidak didukung: {file.content_type}", 415)

    # Baca maksimal batas + 1 byte supaya file raksasa tidak dimuat penuh.
    data = file.file.read(_MAX_BYTES + 1)
    if not data:
        return err("File kosong", 400)
    if len(data) > _MAX_BYTES:
        return err("File terlalu besar (maks 10 MB)", 400)
    detected = sniff_image_format(data)
    if detected is None or detected not in _ALLOWED_TYPES:
        return err(
            "File bukan gambar JPG/PNG/WEBP yang valid "
            "(isi dicek via magic bytes, bukan sekadar Content-Type)", 415)

    target_folder = (folder or UPLOAD_FOLDER or "uploads").strip() or "uploads"
    target_id = public_id or f"img_{int(time.time())}_{uuid4().hex[:8]}"

    try:
        meta = upload_image_meta(data, target_folder, target_id)
    except CloudinaryNotConfiguredError as e:
        return err(str(e), 503)
    except Exception as e:  # noqa: BLE001 - error upload diteruskan sebagai respons
        return err(f"Upload Cloudinary gagal: {e}", 502)

    return ok("Upload berhasil", UploadImageResponse(
        status="success",
        secure_url=meta["secure_url"],
        public_id=meta["public_id"],
        format=meta.get("format"),
        width=meta.get("width"),
        height=meta.get("height"),
        bytes=meta.get("bytes"),
    ).model_dump())
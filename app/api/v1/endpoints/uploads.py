"""Endpoint upload gambar generic (auto-upload ke Cloudinary).

Alur: klien kirim file via multipart/form-data -> file divalidasi -> diunggah
ke Cloudinary -> respons berisi ``secure_url`` + ``public_id``. Klien yang
menyimpan URL tersebut ke database (endpoint ini tidak menyentuh DB).
"""

import time
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile

from app.api.v1.response import err, ok
from app.core.logging import get_logger
from app.schemas.uploads import UploadImageResponse
from app.services.cloudinary_service import (
    CloudinaryNotConfiguredError,
    UPLOAD_FOLDER,
    cloudinary_configured,
    sniff_image_format,
    upload_image_meta,
)

router = APIRouter(tags=["Upload"])

logger = get_logger("upload")

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
    logger.info("upload.photo_attempt", filename=file.filename, content_type=file.content_type, folder=folder)
    if not cloudinary_configured():
        logger.warning("upload.photo_failed", reason="cloudinary_not_configured")
        return err("Cloudinary belum dikonfigurasi (CLOUDINARY_* tidak terisi)", 503)

    ctype = (file.content_type or "").lower()
    if ctype and ctype not in _ALLOWED_TYPES:
        logger.warning("upload.photo_failed", reason="unsupported_type", ctype=ctype)
        return err(f"Tipe file tidak didukung: {file.content_type}", 415)

    # Baca maksimal batas + 1 byte supaya file raksasa tidak dimuat penuh.
    data = file.file.read(_MAX_BYTES + 1)
    if not data:
        logger.warning("upload.photo_failed", reason="empty_file")
        return err("File kosong", 400)
    if len(data) > _MAX_BYTES:
        logger.warning("upload.photo_failed", reason="too_large", size=len(data))
        return err("File terlalu besar (maks 10 MB)", 400)
    detected = sniff_image_format(data)
    if detected is None or detected not in _ALLOWED_TYPES:
        logger.warning("upload.photo_failed", reason="invalid_magic_bytes", detected=detected)
        return err(
            "File bukan gambar JPG/PNG/WEBP yang valid "
            "(isi dicek via magic bytes, bukan sekadar Content-Type)", 415)

    target_folder = (folder or UPLOAD_FOLDER or "uploads").strip() or "uploads"
    target_id = public_id or f"img_{int(time.time())}_{uuid4().hex[:8]}"
    logger.info("upload.cloudinary_uploading", folder=target_folder, public_id=target_id, size=len(data), detected=detected)

    try:
        meta = upload_image_meta(data, target_folder, target_id)
        logger.info("upload.photo_success", public_id=meta["public_id"], url=meta["secure_url"], format=meta.get("format"))
    except CloudinaryNotConfiguredError as e:
        logger.warning("upload.photo_failed", reason="not_configured", error=str(e))
        return err(str(e), 503)
    except Exception as e:  # noqa: BLE001 - error upload diteruskan sebagai respons
        logger.warning("upload.photo_failed", reason="cloudinary_error", error=str(e))
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
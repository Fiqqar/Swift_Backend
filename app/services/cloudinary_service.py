"""Integrasi upload gambar ke Cloudinary untuk bukti pengiriman (POD).

Kredensial dibaca dari environment (CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY,
CLOUDINARY_API_SECRET) dan diunggah lewat SDK resmi `cloudinary`. Endpoint
pathfinding/shipment tidak menyimpan file lokal; hanya menyimpan URL.
"""

import os
from io import BytesIO

import cloudinary

CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
API_KEY = os.environ.get("CLOUDINARY_API_KEY", "").strip()
API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
UPLOAD_FOLDER = os.environ.get("CLOUDINARY_UPLOAD_FOLDER", "pod").strip() or "pod"


class CloudinaryNotConfiguredError(RuntimeError):
    pass


def cloudinary_configured() -> bool:
    return bool(CLOUD_NAME and API_KEY and API_SECRET)


# Brand HEIC/HEIF/AVIF (container ISO BMFF, box "ftyp").
_ISO_BMFF_BRANDS = {
    b"heic", b"heix", b"hevc", b"hevx",
    b"heim", b"heis", b"hevm", b"hevs",
    b"mif1", b"msf1", b"avif",
}


def sniff_image_format(data: bytes) -> str | None:
    """Deteksi format gambar dari isi file (magic bytes).

    Content-Type pada header multipart bisa dipalsukan klien (mis. file PHP
    berlabel ``image/jpeg``), jadi keputusan utama didasarkan pada isi file,
    bukan header. Mengembalikan MIME type (mis. ``image/jpeg``) atau ``None``.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _ISO_BMFF_BRANDS:
        return "image/heic"
    return None


def _upload_result(file_bytes: bytes, folder: str, public_id: str) -> dict:
    """Unggah byte gambar ke Cloudinary dan kembalikan seluruh hasil upload.

    Melempar :class:`CloudinaryNotConfiguredError` bila kredensial kosong, atau
    ``RuntimeError`` bila Cloudinary tidak mengembalikan URL.
    """
    if not cloudinary_configured():
        raise CloudinaryNotConfiguredError(
            "Cloudinary belum dikonfigurasi "
            "(CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET)."
        )
    cloudinary.config(
        cloud_name=CLOUD_NAME,
        api_key=API_KEY,
        api_secret=API_SECRET,
    )
    result = cloudinary.uploader.upload(
        file=BytesIO(file_bytes),
        folder=folder,
        public_id=public_id,
        resource_type="image",
        overwrite=True,
    )
    url = result.get("secure_url") or result.get("url")
    if not url:
        raise RuntimeError("Upload Cloudinary tidak mengembalikan URL.")
    return result


def upload_image(file_bytes: bytes, folder: str, public_id: str) -> str:
    """Unggah byte gambar ke Cloudinary dan kembalikan URL aman (secure_url)."""
    result = _upload_result(file_bytes, folder, public_id)
    return result.get("secure_url") or result.get("url")


def upload_image_meta(file_bytes: bytes, folder: str, public_id: str) -> dict:
    """Unggah byte gambar ke Cloudinary lalu kembalikan metadata penting.

    Berisi ``secure_url``, ``public_id``, ``format``, ``width``, ``height``,
    dan ``bytes`` — cukup untuk disimpan ke database oleh klien.
    """
    result = _upload_result(file_bytes, folder, public_id)
    return {
        "secure_url": result.get("secure_url") or result.get("url"),
        "public_id": result.get("public_id"),
        "format": result.get("format"),
        "width": result.get("width"),
        "height": result.get("height"),
        "bytes": result.get("bytes"),
    }
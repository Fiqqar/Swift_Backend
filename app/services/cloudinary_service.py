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


def upload_image(file_bytes: bytes, folder: str, public_id: str) -> str:
    """Unggah byte gambar ke Cloudinary dan kembalikan URL aman (secure_url)."""
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
    return url

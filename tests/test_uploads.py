"""Unit test endpoint upload gambar generic (/api/v1/uploads/photo).

Tidak butuh database maupun kredensial Cloudinary asli — panggilan Cloudinary
di-mock. Menjalankan dari root repo:

    uv run pytest tests/test_uploads.py -q
"""

import io
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints.uploads import router


def _app():
    app = FastAPI()
    app.include_router(router)
    return app


def _png_bytes(n=64):
    return b"\x89PNG\r\n\x1a\n" + (b"\x00" * max(0, n - 8))


def _client():
    return TestClient(_app())


def test_upload_photo_success():
    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=True), \
         patch("app.api.v1.endpoints.uploads.upload_image_meta",
               return_value={
                   "secure_url": "https://res.cloudinary.com/demo/image/upload/v1/up/foto.png",
                   "public_id": "up/foto.png",
                   "format": "png",
                   "width": 64,
                   "height": 64,
                   "bytes": 64,
               }) as m:
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("foto.png", io.BytesIO(_png_bytes()), "image/png")},
            )
    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["secure_url"].startswith("https://res.cloudinary.com/")
    assert data["public_id"] == "up/foto.png"
    assert data["format"] == "png"
    assert data["width"] == 64
    m.assert_called_once()


def test_upload_photo_rejects_non_allowed_content_type():
    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=True):
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("dokumen.txt", io.BytesIO(b"hello"), "text/plain")},
            )
    assert resp.status_code == 415
    assert resp.json()["success"] is False


def test_upload_photo_rejects_spoofed_content_type():
    # Header diklaim image/jpeg tapi isi bukan gambar (magic bytes tidak cocok).
    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=True):
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("shell.php.jpg", io.BytesIO(b"<?php echo 1;"), "image/jpeg")},
            )
    assert resp.status_code == 415


def test_upload_photo_empty_file():
    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=True):
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("kosong.png", io.BytesIO(b""), "image/png")},
            )
    assert resp.status_code == 400


def test_upload_photo_too_large():
    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=True), \
         patch("app.api.v1.endpoints.uploads._MAX_BYTES", 8):
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("besar.png", io.BytesIO(_png_bytes(64)), "image/png")},
            )
    assert resp.status_code == 400


def test_upload_photo_not_configured():
    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=False):
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("foto.png", io.BytesIO(_png_bytes()), "image/png")},
            )
    assert resp.status_code == 503
    assert resp.json()["success"] is False


def test_upload_photo_cloudinary_error():
    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=True), \
         patch("app.api.v1.endpoints.uploads.upload_image_meta",
               side_effect=RuntimeError("boom")):
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("foto.png", io.BytesIO(_png_bytes()), "image/png")},
            )
    assert resp.status_code == 502
    assert "boom" in resp.json()["message"]


def test_upload_photo_custom_folder_and_public_id():
    sent = {}

    def fake_upload(file_bytes, folder, public_id):
        sent["folder"] = folder
        sent["public_id"] = public_id
        return {
            "secure_url": "https://res.cloudinary.com/demo/image/upload/v1/ktp/a1.jpg",
            "public_id": "ktp/a1.jpg",
            "format": "png",
            "width": 1,
            "height": 1,
            "bytes": 1,
        }

    with patch("app.api.v1.endpoints.uploads.cloudinary_configured", return_value=True), \
         patch("app.api.v1.endpoints.uploads.upload_image_meta", side_effect=fake_upload):
        with _client() as client:
            resp = client.post(
                "/photo",
                files={"file": ("foto.png", io.BytesIO(_png_bytes()), "image/png")},
                data={"folder": "ktp", "public_id": "a1"},
            )
    assert resp.status_code == 201
    assert sent == {"folder": "ktp", "public_id": "a1"}
    assert resp.json()["data"]["secure_url"].startswith("https://res.cloudinary.com/")
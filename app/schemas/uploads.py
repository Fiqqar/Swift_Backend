from pydantic import BaseModel, Field


class UploadImageResponse(BaseModel):
    status: str = "success"
    secure_url: str = Field(
        description="URL publik gambar di Cloudinary (siap disimpan ke database).")
    public_id: str = Field(
        description="ID publik aset di Cloudinary (untuk delete/update).")
    format: str | None = Field(
        default=None, description="Format gambar (mis. jpg, png, webp).")
    width: int | None = Field(default=None, description="Lebar gambar (px).")
    height: int | None = Field(default=None, description="Tinggi gambar (px).")
    bytes: int | None = Field(default=None, description="Ukuran file (bytes).")
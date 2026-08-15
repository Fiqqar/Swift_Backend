from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(
        default="",
        description="Wajib diisi (tidak boleh kosong). Username kurir "
                    "(`Kurir.username`).")
    password: str = Field(
        default="",
        description="Wajib diisi (tidak boleh kosong). Password kurir.")

    model_config = ConfigDict(json_schema_extra={
        "examples": [{"username": "kurir1", "password": "rahasia123"}]
    })

from fastapi.responses import JSONResponse


def ok(message: str, data):
    return {"success": True, "message": message, "data": data}


def err(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"success": False, "message": message}, status_code=status_code
    )

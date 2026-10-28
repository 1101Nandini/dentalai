from typing import Any, Optional
from fastapi.responses import JSONResponse


def success_response(data: Any, message: str = "Success", status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": True, "message": message, "data": data},
    )


def error_response(message: str, status_code: int = 400, details: Optional[Any] = None) -> JSONResponse:
    body = {"success": False, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def paginated_response(data: list, total: int, skip: int, limit: int) -> dict:
    return {
        "success": True,
        "total": total,
        "skip": skip,
        "limit": limit,
        "data": data,
    }

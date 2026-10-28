"""
Standardised API response envelope.

Every endpoint returns:
  {
    "success": true | false,
    "message": "...",
    "data": { ... } | null
  }
"""
from typing import Any, Optional
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder


def api_response(
    *,
    success: bool = True,
    message: str = "",
    data: Optional[Any] = None,
    error: Optional[str] = None,
    status_code: int = 200,
    response: Optional[JSONResponse] = None,
) -> JSONResponse:
    
    # Use jsonable_encoder to handle datetimes, ObjectIds, etc.
    body = jsonable_encoder({
        "success": success,
        "message": message,
        "data": data,
    })
    
    # Add error field if provided
    if error is not None:
        body["error"] = error

    if response is not None:
        import json
        response.status_code = status_code
        response.body = json.dumps(body).encode("utf-8")
        response.headers["content-length"] = str(len(response.body))
        return response

    return JSONResponse(status_code=status_code, content=body)
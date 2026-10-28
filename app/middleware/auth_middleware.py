from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from jose import JWTError, jwt, ExpiredSignatureError

from app.config import settings

# Paths that do NOT require authentication
PUBLIC_PATHS = {
    "/",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    # Auth — all pre-login routes are public
    "/api/v1/auth/send-otp",
    "/api/v1/auth/register",
    "/api/v1/auth/request-account",
    "/api/v1/auth/verify-email",
    "/api/v1/auth/resend-otp",
    "/api/v1/auth/login",
    "/api/v1/auth/forgot-password",
    "/api/v1/auth/refresh",
    "/api/v1/auth/google",
    "/api/v1/auth/accept-invitation",
    "/api/v1/auth/validate-invitation",
    "/api/v1/blob/proxy",
    "/api/v1/stripe/webhook",
    "/api/v1/stripe/webhook/",
    "/api/v1/subscriptions/plans",
    "/api/v1/dental-voice/test/generate"
}

def _unauthorized_response(request: Request, message: str, error_code: str = "UNAUTHORIZED", refresh_allowed: bool = True) -> JSONResponse:
    content = {"success": False, "message": message, "data": None, "error": {"code": error_code, "refresh_allowed": refresh_allowed,  "message": message}}
    headers = {}
    origin = request.headers.get("origin")
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    return JSONResponse(content=content, status_code=401, headers=headers)

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Allow CORS preflight OPTIONS requests (no authentication needed)
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        # Allow public routes without token
        if (
            path in PUBLIC_PATHS 
            or (path != "/" and path.rstrip("/") in PUBLIC_PATHS) 
            or path.startswith("/docs") 
            or path.startswith("/redoc")
            or path.startswith("/api/v1/stripe/verify-payment")
        ):
            return await call_next(request)

        # 1. Try Authorization header first (Bearer token), then fall back to cookie for backward compatibility
        token = None
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]

        # Fallback to cookie if no Bearer token
        if not token:
            token = request.cookies.get("access_token")

        if not token:
            return _unauthorized_response(request, "Missing or invalid authentication token", refresh_allowed=True)

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            request.state.user_id = payload.get("sub")
            request.state.role = payload.get("role")
        except ExpiredSignatureError:
            return _unauthorized_response(request, "Token has expired. Please refresh your token or log in again.", refresh_allowed=True)
        except JWTError:
            return _unauthorized_response(request, "Invalid or malformed authentication token", refresh_allowed=False)

        return await call_next(request)

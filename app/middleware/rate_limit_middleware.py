import time
import logging
import re
from typing import Dict, List, Set
from fastapi import Request, status, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Compile regex for malicious scanners once at startup
# This catches common exploit payloads, hidden configs, and key dumps
SUSPICIOUS_PATTERNS = re.compile(
    r"(\.env|\.git|\.pem|\.sql|\.yml|\.yaml|wp-admin|config/smtp|elasticemail|sparkpost)",
    re.IGNORECASE
)

# Keep track of completely banned IPs to save CPU cycles on sliding window calculation
PERMANENTLY_BANNED_IPS: Set[str] = set()


class RateLimitManager:
    """
    Shared in-memory storage for rate limits.
    Supports sliding window logic for both Middleware and Dependencies.
    """
    # { group_name: { client_ip: [timestamps] } }
    _state: Dict[str, Dict[str, List[float]]] = {}
    _last_cleanup_time: float = time.time()

    @classmethod
    def is_allowed(cls, client_ip: str, group: str, limit: int, window: int) -> bool:
        """
        Check if a request is allowed under the given limit and window.
        Updates the state if allowed.
        """
        current_time = time.time()
        
        # Periodic Memory Cleanup (Every 10 minutes)
        # Prevents millions of unique scanner IPs from filling up RAM permanently
        if current_time - cls._last_cleanup_time > 600:
            cls._cleanup_expired_records(current_time)

        if group not in cls._state:
            cls._state[group] = {}
        
        timestamps = cls._state[group].get(client_ip, [])
        
        # Keep only timestamps within the last `window` seconds
        timestamps = [t for t in timestamps if current_time - t < window]
        
        if len(timestamps) >= limit:
            cls._state[group][client_ip] = timestamps
            return False
            
        timestamps.append(current_time)
        cls._state[group][client_ip] = timestamps
        return True

    @classmethod
    def _cleanup_expired_records(cls, current_time: float):
        """Cleans out entries that haven't sent requests within their tracking windows."""
        for group, clients in list(cls._state.items()):
            for client_ip, timestamps in list(clients.items()):
                # If the last timestamp is older than 5 minutes, purge the IP entry entirely
                if not timestamps or (current_time - timestamps[-1] > 300):
                    del cls._state[group][client_ip]
        cls._last_cleanup_time = current_time


def extract_real_ip(request: Request) -> str:
    """
    Robust IP extraction logic.
    Prioritizes headers sent by Nginx/Cloudflare over local socket connection.
    """
    # 1. Try X-Forwarded-For (standard for multi-layered proxies)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Split by comma and strip whitespace to get the true client origin
        return forwarded.split(",")[0].strip()
    
    # 2. Try X-Real-IP (fallback direct proxy header)
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    # 3. Fallback to physical socket connection connection
    return request.client.host if request.client else "127.0.0.1"


class RateLimiter:
    """
    FastAPI Dependency Factory for route-based rate limiting.
    """
    def __init__(self, limit: int, window: int, group: str):
        self.limit = limit
        self.window = window
        self.group = group

    async def __call__(self, request: Request):
        client_ip = extract_real_ip(request)

        if client_ip in PERMANENTLY_BANNED_IPS:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access Denied")

        if not RateLimitManager.is_allowed(client_ip, self.group, self.limit, self.window):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later."
            )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Global rate limiting middleware that checks for scans and normal limits.
    """
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        client_ip = extract_real_ip(request)

        # Early Drop 1: Check if IP was already blacklisted in memory for scanning
        if client_ip in PERMANENTLY_BANNED_IPS:
            return Response(status_code=status.HTTP_403_FORBIDDEN)

        # Early Drop 2: Regex scanning match
        path = request.url.path
        if SUSPICIOUS_PATTERNS.search(path):
            PERMANENTLY_BANNED_IPS.add(client_ip)
            logger.error(f"[SECURITY ALERT] Banned client {client_ip} permanently for targeting payload path: {path}")
            # Returning a raw 403 response cuts processing time to almost 0
            return Response(status_code=status.HTTP_403_FORBIDDEN)

        # Global rate limit settings
        limit = 100
        window = 60
        group = "global"

        if not RateLimitManager.is_allowed(client_ip, group, limit, window):
            logger.warning(
                f"[RATE-LIMIT] Blocked client {client_ip} requesting {path} "
                f"- Exceeded global limit of {limit} req/{window}s"
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "success": False,
                    "message": "Too many requests. Please try again later.",
                    "data": None,
                    "error": {
                        "code": "TOO_MANY_REQUESTS",
                        "message": "Too many requests. Please try again later."
                    }
                }
            )

        return await call_next(request)
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from contextlib import asynccontextmanager

from app.core.database import connect_to_mongo, close_mongo_connection
from app.middleware.auth_middleware import AuthMiddleware
from app.routes import auth_routes, user_routes, document_routes, chat_routes, blob_routes, analytics_routes, speech_routes, subscription_routes, stripe_routes, dental_voice_routes, organization_routes, template_routes, video_routes, case_study_routes
from app.config import settings

import logging
import sys
import io
from logging.handlers import RotatingFileHandler

# Windows console encoding fix for emojis
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, io.UnsupportedOperation):
        pass

# ── Logging Setup ─────────────────────────────────────────────────────────────
# RotatingFileHandler: caps each file at 10 MB, keeps 5 backups → max 50 MB on disk.
# When app_server.log hits 10 MB it becomes app_server.log.1, a fresh file starts.
# Files beyond 5 are auto-deleted (oldest first).
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_file_handler = RotatingFileHandler(
    "app_server.log",
    maxBytes=10 * 1024 * 1024,   # 10 MB per file
    backupCount=5,                # keep .1 → .5  (50 MB max total)
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console_handler, _file_handler],
)

# ── Silence Noisy Libraries ────────────────────────────────────────────────
# These libraries produce too much terminal clutter for large documents.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("azure.search.documents.SearchClient").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from app.background.processing_worker import processing_worker, recover_stuck_documents
    from app.background.soap_retention_worker import soap_retention_worker

    # Startup
    await connect_to_mongo()

    # Seed default role permissions (safe — won't overwrite existing customizations)
    try:
        from app.services.permission_service import PermissionService
        seed_result = await PermissionService.seed_defaults()
        logger.info(f"[STARTUP] Permissions seeded: {seed_result}")

        # Seed default note templates
        from app.services.template_service import TemplateService
        template_seed_result = await TemplateService.seed_default_templates()
        logger.info(f"[STARTUP] Templates seeded: {template_seed_result}")

        # Recover any documents stuck in intermediate states from a previous crash
        await recover_stuck_documents()
        
        # Start the processing worker as a background asyncio task
        worker_task = asyncio.create_task(processing_worker())
        # Start the SOAP retention worker (daily cleanup of 30-day-old reports)
        retention_task = asyncio.create_task(soap_retention_worker())

    except Exception as e:
        logger.warning(f"[STARTUP] Skipping DB-dependent startup steps because DB is not connected: {e}")
        worker_task = None
        retention_task = None

    yield

    # Shutdown
    if worker_task:
        worker_task.cancel()
    if retention_task:
        retention_task.cancel()
    
    if worker_task:
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    if retention_task:
        try:
            await retention_task
        except asyncio.CancelledError:
            pass
    await close_mongo_connection()


app = FastAPI(
    title="Smile Guide AI – Dental AI Assistant",
    description="RAG-powered dental knowledge assistant with admin and user roles.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Explicit CORS Preflight Handler — responds to OPTIONS requests (fallback)
# ---------------------------------------------------------------------------
@app.options("/{full_path:path}", include_in_schema=False)
async def preflight_handler(full_path: str):
    """Handle CORS preflight OPTIONS requests."""
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Rate Limiting Logic (Now applied per-route/group via dependencies)
# ---------------------------------------------------------------------------
from app.middleware.rate_limit_middleware import RateLimiter, RateLimitMiddleware

# ---------------------------------------------------------------------------
# Authentication Middleware (added FIRST, executes THIRD)
# ---------------------------------------------------------------------------
app.add_middleware(AuthMiddleware)

# ---------------------------------------------------------------------------
# Global Rate Limiting Middleware (added SECOND, executes SECOND - before auth but after CORS)
# ---------------------------------------------------------------------------
app.add_middleware(RateLimitMiddleware)

# ---------------------------------------------------------------------------
# CORS Middleware (added LAST, executes FIRST - wraps everything)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://192.168.0.172:3000", "http://13.88.83.69:8001", "https://smileguideai.com", "https://www.smileguideai.com", "https://app.smileguideai.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

# ---------------------------------------------------------------------------
# Global exception handlers — enforce standard response envelope on errors
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "message" in exc.detail:
        message = exc.detail["message"]
        error = exc.detail
    else:
        message = exc.detail
        error = exc.detail

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": message,
            "data": None,
            "error": error,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Flatten pydantic validation errors into a readable message
    errors = [f"{' → '.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "message": "Validation error",
            "data": errors,
            "error": "Validation failed",
        },
    )

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(
    auth_routes.router,
    prefix="/api/v1/auth",
    tags=["Auth"],
    dependencies=[Depends(RateLimiter(limit=10, window=60, group="auth"))]
)
# 1. Users router (Limit: 20/min)
app.include_router(
    user_routes.router,
    prefix="/api/v1/users",
    tags=["Users"],
    dependencies=[Depends(RateLimiter(limit=20, window=60, group="users"))]
)

# 2. Documents router (Limit: 30/min)
app.include_router(
    document_routes.router,
    prefix="/api/v1/documents",
    tags=["Documents"],
    dependencies=[Depends(RateLimiter(limit=30, window=60, group="documents"))]
)

# 3. Chat router (Limit: 50/min)
app.include_router(
    chat_routes.router,
    prefix="/api/v1",
    tags=["Chat"],
    dependencies=[Depends(RateLimiter(limit=50, window=60, group="chat"))]
)
app.include_router(
    blob_routes.router,
    prefix="/api/v1/blob",
    tags=["Blob"],
    dependencies=[Depends(RateLimiter(limit=30, window=60, group="blob"))]
)

app.include_router(
    analytics_routes.router,
    prefix="/api/v1/analytics",
    tags=["Analytics"],
    dependencies=[Depends(RateLimiter(limit=20, window=60, group="analytics"))]
)
app.include_router(
    speech_routes.router,
    prefix="/api/v1/speech",
    tags=["Speech"],
    dependencies=[Depends(RateLimiter(limit=10, window=60, group="speech"))]
)
app.include_router(
    subscription_routes.router,
    prefix="/api/v1/subscriptions",
    tags=["Subscriptions"],
    dependencies=[Depends(RateLimiter(limit=10, window=60, group="subscriptions"))]
)
app.include_router(stripe_routes.router, prefix="/api/v1/stripe", tags=["Stripe"]) # UNLIMITED
app.include_router(
    dental_voice_routes.router,
    prefix="/api/v1/dental-voice",
    tags=["Dental Voice Assistant"],
    dependencies=[Depends(RateLimiter(limit=30, window=60, group="dental_voice"))]
)
# WebSocket routes mounted separately — no rate limiting (connections are long-lived)
app.include_router(
    dental_voice_routes.ws_router,
    prefix="/api/v1/dental-voice",
    tags=["Dental Voice Assistant"],
)
# ── Organization routes (Multi-tenant) ─────────────────────────────────────
app.include_router(
    organization_routes.super_router,
    prefix="/api/v1/organizations",
    tags=["Organizations (Super Admin)"],
    dependencies=[Depends(RateLimiter(limit=20, window=60, group="organizations"))]
)
app.include_router(
    organization_routes.org_router,
    prefix="/api/v1/org",
    tags=["Organization (Org Admin)"],
    dependencies=[Depends(RateLimiter(limit=20, window=60, group="org_admin"))]
)
app.include_router(
    template_routes.router,
    prefix="/api/v1/templates",
    tags=["Templates"],
    dependencies=[Depends(RateLimiter(limit=30, window=60, group="templates"))]
)
app.include_router(
    video_routes.router,
    prefix="/api/v1/videos",
    tags=["Teaching Videos"],
    dependencies=[Depends(RateLimiter(limit=30, window=60, group="videos"))]
)
app.include_router(
    case_study_routes.router,
    prefix="/api/v1/case-studies",
    tags=["Case Studies"],
    dependencies=[Depends(RateLimiter(limit=30, window=60, group="case_studies"))]
)


@app.get("/health", tags=["Health"])
async def health_check():
    return {
        "success": True,
        "message": "Service is healthy",
        "data": {"service": "Smile Guide AI – Dental AI"},
    }

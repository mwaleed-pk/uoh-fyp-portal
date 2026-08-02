"""
UOH FYP Portal — Backend Entry Point
University of Haripur | Designed & Developed by MW Trader

Single deployable entry point per PRD Section 16.
This file handles ONLY: application bootstrap, router registration, middleware setup.
All business logic lives in app/services/. All route handlers live in app/routes/.
"""

import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.utils.logging_setup import configure_logging
from app.utils.config import settings
from app.db.supabase_client import get_supabase_client

# Import all routers
from app.routes import (
    auth,
    taxonomy,
    users,
    projects,
    documents,
    reviews,
    meetings,
    notifications,
    announcements,
    health,
    public,
    supervisor,
    admin,
)

# Configure structured logging before anything else
configure_logging()
logger = structlog.get_logger(__name__)

# Rate limiter (shared state must be in Supabase/Redis for horizontal scaling per PRD Section 60)
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    logger.info("fyp_portal_starting", environment=settings.ENVIRONMENT,
                backend_url=settings.BACKEND_BASE_URL)
    # Verify Supabase connectivity on startup
    try:
        client = get_supabase_client()
        logger.info("supabase_connected")
    except Exception as e:
        logger.error("supabase_connection_failed", error=str(e))
    yield
    logger.info("fyp_portal_shutting_down")


# Application instance
app = FastAPI(
    title="UOH FYP Portal API",
    description="University of Haripur — Final Year Project Management Portal. "
                "Designed & Developed by MW Trader.",
    version="1.0.0",
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
    lifespan=lifespan,
)

# Rate limiter state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — only allow the configured frontend origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_BASE_URL],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}):517\d$" if settings.ENVIRONMENT == "development" else None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Accept",
        "Accept-Language",
        "Authorization",
        "Content-Language",
        "Content-Type",
        "X-Request-ID",
        "Bypass-Tunnel-Reminder",
    ],
    expose_headers=["X-Request-ID"],
)


# Global exception handler — never leak stack traces to clients
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import uuid
    correlation_id = str(uuid.uuid4())
    logger.error(
        "unhandled_exception",
        correlation_id=correlation_id,
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred. Please try again or contact support.",
            "correlation_id": correlation_id,
        },
    )


# Register all routers with /api prefix
app.include_router(health.router, prefix="/api", tags=["Health"])
app.include_router(public.router, prefix="/api/public", tags=["Public Portal"])
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(taxonomy.router, prefix="/api/taxonomy", tags=["Taxonomy"])
app.include_router(users.router, prefix="/api/users", tags=["Users"])
app.include_router(projects.router, prefix="/api/projects", tags=["Projects"])
app.include_router(documents.router, prefix="/api/documents", tags=["Documents"])
app.include_router(reviews.router, prefix="/api/reviews", tags=["Reviews"])
app.include_router(meetings.router, prefix="/api/meetings", tags=["Meetings"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["Notifications"])
app.include_router(announcements.router, prefix="/api/announcements", tags=["Announcements"])
app.include_router(supervisor.router, prefix="/api/supervisor", tags=["Supervisor Workspace"])
app.include_router(admin.router, prefix="/api/admin", tags=["Admin Management"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=settings.ENVIRONMENT == "development",
        log_config=None,  # Use structlog, not uvicorn's default logging
    )

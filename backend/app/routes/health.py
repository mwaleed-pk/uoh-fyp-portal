"""Health check endpoint for uptime monitoring."""
from fastapi import APIRouter
from datetime import datetime, timezone
from app.utils.config import settings

router = APIRouter()

@router.get("/health")
async def health_check():
    """Health check endpoint. Returns 200 when service is running."""
    return {
        "status": "healthy",
        "service": settings.PORTAL_NAME,
        "environment": settings.ENVIRONMENT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

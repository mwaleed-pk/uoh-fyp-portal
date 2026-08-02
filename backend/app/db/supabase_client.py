"""
Supabase client — wraps the supabase-py client with service-role credentials.
The service role key is NEVER exposed to the frontend.
All privileged DB operations (bypassing RLS) go through this client.
"""
from supabase import create_client, Client
from app.utils.config import settings
import structlog

logger = structlog.get_logger(__name__)

_client: Client | None = None


def get_supabase_client() -> Client:
    """
    Returns the singleton Supabase service-role client.
    Service role bypasses RLS — backend must enforce authorization before use.
    """
    global _client
    if _client is None:
        _client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_ROLE_KEY,
        )
        logger.info("supabase_client_initialized")
    return _client


def get_db() -> Client:
    """Alias for dependency injection in FastAPI route handlers."""
    return get_supabase_client()

"""Notification service — creates in-app notifications and dispatches emails."""
import structlog
from app.db.supabase_client import get_supabase_client
from typing import Optional

logger = structlog.get_logger(__name__)


async def create_notification(
    user_id: str,
    user_role: str,
    event_type: str,
    title: str,
    body: str,
    resource_id: Optional[str] = None,
) -> None:
    """Create an in-app notification record."""
    try:
        client = get_supabase_client()
        entry = {
            "user_id": user_id,
            "user_role": user_role,
            "event_type": event_type,
            "title": title,
            "body": body,
            "resource_id": resource_id,
        }
        
        def _insert():
            try:
                client.table("notifications").insert(entry).execute()
            except Exception as e:
                logger.error("notification_create_failed", error=str(e), user_id=user_id)
                
        import asyncio
        asyncio.create_task(asyncio.to_thread(_insert))
    except Exception as e:
        logger.error("notification_create_failed", error=str(e), user_id=user_id)

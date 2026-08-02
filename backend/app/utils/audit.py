"""
Audit logging — writes structured audit entries for every state-changing action.
Every entry includes actor_id, actor_role, action_type, resource, and timestamp.
Log entries NEVER include plaintext OTP codes, tokens, or document content.
"""
import structlog
import uuid
import asyncio
from typing import Optional
from app.db.supabase_client import get_supabase_client

logger = structlog.get_logger(__name__)


async def write_audit_log(
    action_type: str,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """
    Write a structured audit log entry to the audit_logs table.
    Called for every state-changing action in the system.
    Failures are logged but never surface to the user (audit is non-blocking).
    """
    try:
        client = get_supabase_client()
        entry = {
            "action_type": action_type,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "metadata": metadata or {},
            "ip_address": ip_address,
        }
        
        def _insert():
            try:
                client.table("audit_logs").insert(entry).execute()
                logger.info("audit_log_written", action_type=action_type,
                            actor_id=actor_id, resource_type=resource_type)
            except Exception as e:
                logger.error("audit_log_failed", error=str(e), action_type=action_type)
                
        asyncio.create_task(asyncio.to_thread(_insert))
    except Exception as e:
        logger.error("audit_log_failed", error=str(e), action_type=action_type)

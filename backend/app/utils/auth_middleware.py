"""
Authentication middleware — validates session tokens on every request.
Authorization is evaluated fresh on EVERY request (no cached grants).
"""
from fastapi import Request, HTTPException, Depends
from typing import Optional
from datetime import datetime, timedelta, timezone
from time import monotonic
from threading import RLock
import asyncio
from app.utils.config import settings
import structlog

from app.utils.security import decode_session_token, hash_token_for_storage
from app.db.supabase_client import get_supabase_client

logger = structlog.get_logger(__name__)

_SESSION_CACHE_TTL_SECONDS = 5.0
_SESSION_ACTIVITY_WRITE_SECONDS = 300.0
_session_cache: dict[str, tuple[float, dict]] = {}
_session_cache_lock = RLock()

def cache_new_session(token: str, session: dict) -> None:
    """Seed validation cache immediately after login to remove first-page auth I/O."""
    with _session_cache_lock:
        _session_cache[hash_token_for_storage(token)] = (monotonic() + _SESSION_CACHE_TTL_SECONDS, session)

def invalidate_session_cache(token_hash: Optional[str] = None, user_id: Optional[str] = None) -> None:
    with _session_cache_lock:
        if token_hash:
            _session_cache.pop(token_hash, None)
        if user_id:
            for key in [k for k,v in _session_cache.items() if str(v[1].get("user_id")) == str(user_id)]:
                _session_cache.pop(key, None)


def get_token_from_request(request: Request) -> Optional[str]:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


async def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency: validates session token and returns caller identity.
    Raises 401 if unauthenticated, 403 if session is invalid/expired.
    Authorization rule evaluation (per permission matrix) happens in each endpoint.
    """
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail={
            "error_code": "UNAUTHENTICATED",
            "message": "Authentication required. Please log in.",
        })

    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail={
            "error_code": "INVALID_SESSION",
            "message": "Your session has expired. Please log in again.",
        })

    token_hash = hash_token_for_storage(token)
    now_mono = monotonic()
    with _session_cache_lock:
        cached = _session_cache.get(token_hash)
        if cached and cached[0] > now_mono:
            session = cached[1]
            return {"user_id": session["user_id"], "role": session["user_role"],
                    "session_id": session["id"], "token": token}

    # Verify session exists in DB (supports per-device logout)
    try:
        client = get_supabase_client()
        result = await asyncio.to_thread(lambda: client.table("user_sessions")
            .select("id, user_id, user_role, expires_at, last_active")
            .eq("token_hash", token_hash).single().execute())

        if not result.data:
            raise HTTPException(status_code=401, detail={
                "error_code": "SESSION_REVOKED",
                "message": "Your session has been revoked. Please log in again.",
            })

        session = result.data
        if (str(session["id"]) != str(payload.get("sid")) or
                str(session["user_id"]) != str(payload.get("sub")) or
                session["user_role"] != payload.get("role")):
            raise HTTPException(status_code=401, detail={
                "error_code": "SESSION_IDENTITY_MISMATCH",
                "message": "Your session is invalid. Please log in again.",
            })

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
        if expires_at <= now:
            client.table("user_sessions").delete().eq("id", session["id"]).execute()
            raise HTTPException(status_code=401, detail={
                "error_code": "SESSION_EXPIRED",
                "message": "Your session has expired. Please log in again.",
            })

        if session["user_role"] not in {"admin", "supervisor", "student"}:
            raise HTTPException(status_code=401, detail={
                "error_code": "INVALID_ROLE", "message": "Your session is invalid. Please log in again."
            })
        # Roll activity at most once per five minutes, not on every API call.
        absolute_expiry = datetime.fromisoformat(payload["abs_exp"].replace("Z", "+00:00"))
        next_expiry = min(
            now + timedelta(hours=settings.SESSION_INACTIVITY_HOURS),
            absolute_expiry,
        )
        last_active = datetime.fromisoformat(session["last_active"].replace("Z", "+00:00"))
        if (now - last_active).total_seconds() >= _SESSION_ACTIVITY_WRITE_SECONDS:
            await asyncio.to_thread(lambda: client.table("user_sessions").update({
                "last_active": now.isoformat(), "expires_at": next_expiry.isoformat()
            }).eq("id", session["id"]).execute())
            session["last_active"] = now.isoformat()
        with _session_cache_lock:
            if len(_session_cache) > 10_000:
                for key in [k for k, v in _session_cache.items() if v[0] <= now_mono]:
                    _session_cache.pop(key, None)
            _session_cache[token_hash] = (now_mono + _SESSION_CACHE_TTL_SECONDS, session)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("session_validation_db_error", error=str(e))
        raise HTTPException(status_code=401, detail={
            "error_code": "SESSION_VALIDATION_FAILED",
            "message": "Could not validate session. Please log in again.",
        })

    return {
        "user_id": session["user_id"],
        "role": session["user_role"],
        "session_id": session["id"],
        "token": token,
    }


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require Admin role."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail={
            "error_code": "FORBIDDEN",
            "message": "Admin access required.",
        })
    return current_user


async def require_supervisor(current_user: dict = Depends(get_current_user)) -> dict:
    """Require Supervisor role."""
    if current_user["role"] != "supervisor":
        raise HTTPException(status_code=403, detail={
            "error_code": "FORBIDDEN",
            "message": "Supervisor access required.",
        })
    return current_user


async def require_student(current_user: dict = Depends(get_current_user)) -> dict:
    """Require Student role."""
    if current_user["role"] != "student":
        raise HTTPException(status_code=403, detail={
            "error_code": "FORBIDDEN",
            "message": "Student access required.",
        })
    return current_user


async def require_supervisor_or_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require Supervisor or Admin role."""
    if current_user["role"] not in ("supervisor", "admin"):
        raise HTTPException(status_code=403, detail={
            "error_code": "FORBIDDEN",
            "message": "Supervisor or Admin access required.",
        })
    return current_user

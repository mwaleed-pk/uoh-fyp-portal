"""Announcements and Deadlines routes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from app.utils.auth_middleware import get_current_user, require_admin
from app.db.supabase_client import get_supabase_client
from app.utils.audit import write_audit_log

router = APIRouter()


class AnnouncementBody(BaseModel):
    title: str
    content: str
    scope_type: str = "university"  # university | department | program | session
    scope_id: Optional[str] = None
    publish_at: Optional[str] = None
    expire_at: Optional[str] = None
    is_published: bool = False
    is_public: bool = False


class DeadlineBody(BaseModel):
    title: str
    description: Optional[str] = None
    target_group: str
    target_id: Optional[str] = None
    due_date: str
    reminder_days: Optional[list] = [7, 3, 1]


@router.get("/")
async def list_announcements(current_user: dict = Depends(get_current_user)):
    """Return active announcements relevant to this user."""
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    result = client.table("announcements").select("*, admins(name)")\
        .lte("publish_at", now)\
        .or_(f"expire_at.is.null,expire_at.gte.{now}")\
        .order("publish_at", desc=True).execute()
    return {"announcements": result.data}


@router.post("/")
async def create_announcement(body: AnnouncementBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    data = body.dict(exclude_none=True)
    data["author_id"] = current_user["user_id"]
    if "publish_at" not in data:
        data["publish_at"] = datetime.now(timezone.utc).isoformat()
    result = client.table("announcements").insert(data).execute()
    await write_audit_log("announcement_published", current_user["user_id"], "admin", "announcement", result.data[0]["id"])
    return {"announcement": result.data[0]}


@router.put("/{ann_id}")
async def update_announcement(ann_id: str, body: AnnouncementBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    result = client.table("announcements").update(body.dict(exclude_none=True)).eq("id", ann_id).execute()
    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Announcement not found."})
    await write_audit_log("announcement_published", current_user["user_id"], "admin", "announcement", ann_id)
    return {"announcement": result.data[0]}


@router.delete("/{ann_id}")
async def delete_announcement(ann_id: str, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    client.table("announcements").delete().eq("id", ann_id).execute()
    return {"message": "Announcement deleted."}


# ─── Deadlines ────────────────────────────────────────────────────────────────

@router.get("/deadlines")
async def list_deadlines(current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    result = client.table("deadlines").select("*").eq("status", "active").order("due_date").execute()
    return {"deadlines": result.data}


@router.post("/deadlines")
async def create_deadline(body: DeadlineBody, current_user: dict = Depends(require_admin)):
    # Validate due date is in the future
    try:
        due = datetime.fromisoformat(body.due_date.replace("Z", "+00:00"))
        if due <= datetime.now(timezone.utc):
            raise HTTPException(422, detail={"error_code": "PAST_DATE", "message": "Deadline due date must be in the future."})
    except ValueError:
        raise HTTPException(422, detail={"error_code": "INVALID_DATE", "message": "Invalid due_date format."})

    client = get_supabase_client()
    data = body.dict(exclude_none=True)
    data["author_id"] = current_user["user_id"]
    result = client.table("deadlines").insert(data).execute()
    await write_audit_log("deadline_created", current_user["user_id"], "admin", "deadline", result.data[0]["id"])
    return {"deadline": result.data[0]}


@router.put("/deadlines/{deadline_id}")
async def update_deadline(deadline_id: str, body: DeadlineBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    result = client.table("deadlines").update(body.dict(exclude_none=True)).eq("id", deadline_id).execute()
    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Deadline not found."})
    await write_audit_log("deadline_updated", current_user["user_id"], "admin", "deadline", deadline_id)
    return {"deadline": result.data[0]}


@router.delete("/deadlines/{deadline_id}")
async def delete_deadline(deadline_id: str, current_user: dict = Depends(require_admin)):
    """Archive a deadline so historical references remain intact."""
    client = get_supabase_client()
    result = client.table("deadlines").update({"status": "inactive"}).eq("id", deadline_id).execute()
    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Deadline not found."})
    await write_audit_log("deadline_updated", current_user["user_id"], "admin", "deadline", deadline_id,
                          metadata={"action": "deactivated"})
    return {"message": "Deadline archived."}

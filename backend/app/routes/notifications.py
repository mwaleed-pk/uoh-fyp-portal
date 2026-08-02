"""Notifications routes."""
from fastapi import APIRouter, Depends
from app.utils.auth_middleware import get_current_user
from app.db.supabase_client import get_supabase_client

router = APIRouter()

@router.get("/")
async def list_notifications(current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    result = client.table("notifications").select("*")\
        .eq("user_id", current_user["user_id"]).eq("user_role", current_user["role"])\
        .order("created_at", desc=True).limit(50).execute()
    unread = sum(1 for n in (result.data or []) if not n.get("read_at"))
    return {"notifications": result.data, "unread_count": unread}

@router.post("/{notif_id}/read")
async def mark_read(notif_id: str, current_user: dict = Depends(get_current_user)):
    from datetime import datetime, timezone
    client = get_supabase_client()
    client.table("notifications").update({"read_at": datetime.now(timezone.utc).isoformat()})\
        .eq("id", notif_id).eq("user_id", current_user["user_id"])\
        .eq("user_role", current_user["role"]).execute()
    return {"message": "Marked as read."}

@router.post("/read-all")
async def mark_all_read(current_user: dict = Depends(get_current_user)):
    from datetime import datetime, timezone
    client = get_supabase_client()
    client.table("notifications").update({"read_at": datetime.now(timezone.utc).isoformat()})\
        .eq("user_id", current_user["user_id"]).eq("user_role", current_user["role"])\
        .is_("read_at", "null").execute()
    return {"message": "All notifications marked as read."}

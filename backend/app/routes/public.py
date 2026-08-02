"""Unauthenticated, read-only public portal content endpoints."""
import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException
from app.db.supabase_client import get_supabase_client

router = APIRouter()

_reg_cache = {"data": None, "expires_at": datetime.min.replace(tzinfo=timezone.utc)}


def _fetch_all(query_factory, page_size: int = 500):
    """Read complete master data despite the configured PostgREST row cap."""
    rows, start = [], 0
    while True:
        batch = query_factory().range(start, start + page_size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        start += page_size


@router.get("/settings")
async def public_settings():
    client = get_supabase_client()
    result = client.table("portal_settings").select(
        "portal_email, contact_phone, support_email, about_content, fyp_process_content, contact_content, current_academic_session"
    ).order("updated_at", desc=True).limit(1).execute()
    return {"settings": result.data[0] if result.data else None}


@router.get("/announcements")
async def public_announcements():
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    result = client.table("announcements").select("id, title, content, publish_at, expire_at, created_at") \
        .eq("is_public", True).eq("is_published", True).lte("publish_at", now).order("publish_at", desc=True).execute()
    data = [item for item in (result.data or []) if not item.get("expire_at") or item["expire_at"] > now]
    return {"announcements": data}


@router.get("/guidelines")
async def public_guidelines():
    client = get_supabase_client()
    result = client.table("guidelines").select(
        "id, title, content, category, display_order, storage_path, filename, allow_download, updated_at"
    ).eq("is_public", True).eq("is_published", True).order("display_order").execute()
    return {"guidelines": result.data or []}


@router.get("/statistics")
async def public_statistics():
    client = get_supabase_client()
    s1 = client.table("students").select("id", count="exact").eq("status", "active").execute()
    s2 = client.table("supervisors").select("id", count="exact").eq("status", "active").execute()
    p1 = client.table("projects").select("id", count="exact").execute()
    p2 = client.table("projects").select("id", count="exact").in_("lifecycle_stage", ["approved", "closed"]).execute()
    ps = client.table("portal_settings").select("current_academic_session").order("updated_at", desc=True).limit(1).execute()
    
    return {"statistics": {
        "students": s1.count or 0, "supervisors": s2.count or 0,
        "projects": p1.count or 0, "completed": p2.count or 0,
        "academic_session": ps.data[0].get("current_academic_session") if ps.data else None,
    }}


@router.get("/registration-options")
async def registration_options():
    """Active, administrator-managed values allowed on public student registration."""
    global _reg_cache
    now = datetime.now(timezone.utc)
    if _reg_cache["data"] and now < _reg_cache["expires_at"]:
        return _reg_cache["data"]

    client = get_supabase_client()
    departments = client.table("departments").select("id, name, code").eq("status", "active").order("name").execute()
    programs = client.table("programs").select("id, department_id, name, code").eq("status", "active").order("name").execute()
    semesters = client.table("semesters").select("id, name").eq("status", "active").order("name").execute()
    sessions = client.table("sessions").select("id, program_id, semester_id, academic_year").eq("status", "active").order("academic_year", desc=True).limit(5000).execute()
    sections = _fetch_all(lambda: client.table("sections").select("id, session_id, name").eq("status", "active").order("name").order("id"))
    supervisors = client.table("supervisors").select("id, name, email, department_id, status, approval_status, max_students, account_linked_at, password_hash, supervisor_program_eligibility(program_id)").eq("status", "active").eq("approval_status", "approved").order("name").execute()
    projects = _fetch_all(lambda: client.table("projects").select("id, supervisor_id, lifecycle_stage").not_.is_("supervisor_id", "null").order("id"))
    loads = {}
    for project in projects:
        if project.get("lifecycle_stage") != "closed":
            supervisor_id = project.get("supervisor_id")
            loads[supervisor_id] = loads.get(supervisor_id, 0) + 1
    supervisor_rows = []
    registration_rows = []
    for row in supervisors.data or []:
        safe = {key: value for key, value in row.items() if key != "password_hash"}
        assigned = loads.get(row["id"], 0)
        safe.update({"assigned_count": assigned, "max_students": 30, "is_full": assigned >= 30})
        supervisor_rows.append(safe)
        if not row.get("account_linked_at") and not row.get("password_hash"):
            master_email = safe.get("email") or ""
            public_email = None if master_email.lower().endswith(("@supervisor.local", "@placeholder.local", "@example.invalid")) else master_email
            registration_rows.append({"id": safe.get("id"), "name": safe.get("name"),
                                      "email": public_email, "department_id": safe.get("department_id")})
            
    res = {"departments": departments.data or [], "programs": programs.data or [],
            "semesters": semesters.data or [], "sessions": sessions.data or [],
            "sections": sections, "supervisors": supervisor_rows,
            "supervisor_registration_records": registration_rows}
    _reg_cache = {"data": res, "expires_at": now + timedelta(minutes=5)}
    return res

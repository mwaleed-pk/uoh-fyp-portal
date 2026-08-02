"""
Taxonomy Routes — CRUD for departments, programs, semesters, sessions, sections.
All mutations: Admin only. Reads: all authenticated users.
Deletion guards: entities with dependent records cannot be hard-deleted (PRD Section 34).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional
from app.utils.auth_middleware import get_current_user, require_admin
from app.db.supabase_client import get_supabase_client
from app.utils.audit import write_audit_log

router = APIRouter()

ACTIVE_STATUSES = {"active", "inactive"}


class CleanTaxonomyBody(BaseModel):
    """Normalize administrator-entered labels before they reach Supabase."""

    @field_validator("name", "code", "academic_year", mode="before", check_fields=False)
    @classmethod
    def clean_required_text(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Value cannot be empty.")
        return " ".join(value.split())

    @field_validator("status", mode="before", check_fields=False)
    @classmethod
    def valid_status(cls, value):
        value = value or "active"
        if value not in ACTIVE_STATUSES:
            raise ValueError("Status must be active or inactive.")
        return value


def _not_found(entity: str):
    raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": f"{entity} not found."})


def _dependent(entity: str):
    raise HTTPException(409, detail={
        "error_code": "HAS_DEPENDENTS",
        "message": f"Cannot delete: {entity} is already in use. Deactivate it to preserve historical records.",
    })


def _ensure_active_parent(client, table: str, record_id: str, label: str):
    row = client.table(table).select("id").eq("id", record_id).eq("status", "active").limit(1).execute()
    if not row.data:
        raise HTTPException(422, detail={"error_code": "INVALID_SELECTION", "message": f"Selected {label} is inactive or invalid."})


# ─── Departments ──────────────────────────────────────────────────────────────

class DepartmentBody(CleanTaxonomyBody):
    name: str
    code: str
    status: Optional[str] = "active"

@router.get("/departments")
async def list_departments(current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    result = client.table("departments").select("*").order("name").execute()
    return {"departments": result.data}

@router.post("/departments")
async def create_department(body: DepartmentBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    result = client.table("departments").insert({"name": body.name, "code": body.code.upper(), "status": body.status}).execute()
    await write_audit_log("taxonomy_created", current_user["user_id"], "admin", "department", result.data[0]["id"])
    return {"department": result.data[0]}

@router.put("/departments/{dept_id}")
async def update_department(dept_id: str, body: DepartmentBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    result = client.table("departments").update({"name": body.name, "code": body.code.upper(), "status": body.status}).eq("id", dept_id).execute()
    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Department not found."})
    await write_audit_log("taxonomy_updated", current_user["user_id"], "admin", "department", dept_id)
    return {"department": result.data[0]}

@router.delete("/departments/{dept_id}")
async def delete_department(dept_id: str, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    # Check for dependent programs
    dep_check = client.table("programs").select("id").eq("department_id", dept_id).limit(1).execute()
    if dep_check.data:
        raise HTTPException(409, detail={"error_code": "HAS_DEPENDENTS", "message": "Cannot delete: department has associated programs. Deactivate it instead."})
    result = client.table("departments").delete().eq("id", dept_id).execute()
    if not result.data: _not_found("Department")
    await write_audit_log("taxonomy_deleted", current_user["user_id"], "admin", "department", dept_id)
    return {"message": "Department deleted."}


# ─── Programs ──────────────────────────────────────────────────────────────────

class ProgramBody(CleanTaxonomyBody):
    department_id: str
    name: str
    code: str
    status: Optional[str] = "active"

@router.get("/programs")
async def list_programs(department_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    q = client.table("programs").select("*, departments(name, code)").order("name")
    if department_id:
        q = q.eq("department_id", department_id)
    result = q.execute()
    return {"programs": result.data}

@router.post("/programs")
async def create_program(body: ProgramBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    _ensure_active_parent(client, "departments", body.department_id, "department")
    result = client.table("programs").insert({"department_id": body.department_id, "name": body.name, "code": body.code.upper(), "status": body.status}).execute()
    await write_audit_log("taxonomy_created", current_user["user_id"], "admin", "program", result.data[0]["id"])
    return {"program": result.data[0]}

@router.put("/programs/{prog_id}")
async def update_program(prog_id: str, body: ProgramBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    _ensure_active_parent(client, "departments", body.department_id, "department")
    result = client.table("programs").update({"department_id": body.department_id, "name": body.name, "code": body.code.upper(), "status": body.status}).eq("id", prog_id).execute()
    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Program not found."})
    await write_audit_log("taxonomy_updated", current_user["user_id"], "admin", "program", prog_id)
    return {"program": result.data[0]}

@router.delete("/programs/{prog_id}")
async def delete_program(prog_id: str, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    dep_check = client.table("sessions").select("id").eq("program_id", prog_id).limit(1).execute()
    if dep_check.data:
        raise HTTPException(409, detail={"error_code": "HAS_DEPENDENTS", "message": "Cannot delete: program has sessions. Deactivate it instead."})
    students = client.table("students").select("id").eq("program_id", prog_id).limit(1).execute()
    if students.data: _dependent("Program")
    result = client.table("programs").delete().eq("id", prog_id).execute()
    if not result.data: _not_found("Program")
    await write_audit_log("taxonomy_deleted", current_user["user_id"], "admin", "program", prog_id)
    return {"message": "Program deleted."}


# ─── Semesters ────────────────────────────────────────────────────────────────

class SemesterBody(CleanTaxonomyBody):
    name: str
    status: Optional[str] = "active"

@router.get("/semesters")
async def list_semesters(current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    result = client.table("semesters").select("*").order("name").execute()
    return {"semesters": result.data}

@router.post("/semesters")
async def create_semester(body: SemesterBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    result = client.table("semesters").insert(body.dict()).execute()
    await write_audit_log("taxonomy_created", current_user["user_id"], "admin", "semester", result.data[0]["id"])
    return {"semester": result.data[0]}

@router.put("/semesters/{semester_id}")
async def update_semester(semester_id: str, body: SemesterBody, current_user: dict = Depends(require_admin)):
    result=get_supabase_client().table("semesters").update(body.dict()).eq("id",semester_id).execute()
    if not result.data: raise HTTPException(404,detail={"error_code":"NOT_FOUND","message":"Semester not found."})
    await write_audit_log("taxonomy_updated",current_user["user_id"],"admin","semester",semester_id)
    return {"semester":result.data[0]}

@router.delete("/semesters/{semester_id}")
async def delete_semester(semester_id: str, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    for table in ("sessions", "students"):
        if client.table(table).select("id").eq("semester_id", semester_id).limit(1).execute().data:
            _dependent("Semester")
    result = client.table("semesters").delete().eq("id", semester_id).execute()
    if not result.data: _not_found("Semester")
    await write_audit_log("taxonomy_deleted", current_user["user_id"], "admin", "semester", semester_id)
    return {"message": "Semester deleted."}


# ─── Sessions ──────────────────────────────────────────────────────────────────

class SessionBody(CleanTaxonomyBody):
    program_id: str
    semester_id: str
    academic_year: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = "active"

@router.get("/sessions")
async def list_sessions(program_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    q = client.table("sessions").select("*, programs(name, code), semesters(name)").order("academic_year", desc=True)
    if program_id:
        q = q.eq("program_id", program_id)
    result = q.execute()
    return {"sessions": result.data}

@router.post("/sessions")
async def create_session(body: SessionBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    _ensure_active_parent(client, "programs", body.program_id, "program")
    _ensure_active_parent(client, "semesters", body.semester_id, "semester")
    result = client.table("sessions").insert(body.dict(exclude_none=True)).execute()
    await write_audit_log("taxonomy_created", current_user["user_id"], "admin", "session", result.data[0]["id"])
    return {"session": result.data[0]}

@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    for table in ("sections", "students"):
        if client.table(table).select("id").eq("session_id", session_id).limit(1).execute().data:
            _dependent("Academic session")
    result = client.table("sessions").delete().eq("id", session_id).execute()
    if not result.data: _not_found("Academic session")
    await write_audit_log("taxonomy_deleted", current_user["user_id"], "admin", "session", session_id)
    return {"message": "Academic session deleted."}

@router.put("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    _ensure_active_parent(client, "programs", body.program_id, "program")
    _ensure_active_parent(client, "semesters", body.semester_id, "semester")
    result = client.table("sessions").update(body.dict(exclude_none=True)).eq("id", session_id).execute()
    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Session not found."})
    await write_audit_log("taxonomy_updated", current_user["user_id"], "admin", "session", session_id)
    return {"session": result.data[0]}


# ─── Sections ─────────────────────────────────────────────────────────────────

class SectionBody(CleanTaxonomyBody):
    session_id: str
    name: str
    status: Optional[str] = "active"

@router.get("/sections")
async def list_sections(session_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    q = client.table("sections").select("*, sessions(academic_year)").order("name")
    if session_id:
        q = q.eq("session_id", session_id)
    result = q.execute()
    return {"sections": result.data}

@router.post("/sections")
async def create_section(body: SectionBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    _ensure_active_parent(client, "sessions", body.session_id, "academic session")
    result = client.table("sections").insert(body.dict()).execute()
    await write_audit_log("taxonomy_created", current_user["user_id"], "admin", "section", result.data[0]["id"])
    return {"section": result.data[0]}

@router.put("/sections/{section_id}")
async def update_section(section_id: str, body: SectionBody, current_user: dict = Depends(require_admin)):
    client=get_supabase_client()
    _ensure_active_parent(client, "sessions", body.session_id, "academic session")
    result=client.table("sections").update(body.dict()).eq("id",section_id).execute()
    if not result.data: raise HTTPException(404,detail={"error_code":"NOT_FOUND","message":"Section not found."})
    await write_audit_log("taxonomy_updated",current_user["user_id"],"admin","section",section_id)
    return {"section":result.data[0]}

@router.delete("/sections/{section_id}")
async def delete_section(section_id: str, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    dep_check = client.table("students").select("id").eq("section_id", section_id).limit(1).execute()
    if dep_check.data:
        raise HTTPException(409, detail={"error_code": "HAS_DEPENDENTS", "message": "Cannot delete: section has enrolled students."})
    result = client.table("sections").delete().eq("id", section_id).execute()
    if not result.data: _not_found("Section")
    await write_audit_log("taxonomy_deleted", current_user["user_id"], "admin", "section", section_id)
    return {"message": "Section deleted."}

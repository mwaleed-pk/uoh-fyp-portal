"""
Users Routes — Supervisor & Student account management.
Admin-only mutations. Bulk CSV import with full-pre-validation.
"""
import csv
import uuid
import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from app.utils.auth_middleware import get_current_user, require_admin
from app.db.supabase_client import get_supabase_client
from app.utils.audit import write_audit_log
from app.email.smtp_client import send_temporary_password_email
from app.utils.security import generate_temporary_password, hash_password

router = APIRouter()

SUPERVISOR_SAFE_COLUMNS = (
    "id,email,name,department_id,max_students,areas_of_interest,phone,profile_photo_url,"
    "status,created_at,updated_at"
)
STUDENT_SAFE_COLUMNS = (
    "id,email,name,student_number,registration_number,department_id,program_id,session_id,"
    "semester_id,section_id,phone,profile_photo_url,status,notif_email_supervisor_assigned,"
    "notif_email_document_submitted,notif_email_review_decision,notif_email_comment_posted,"
    "notif_email_meeting,notif_email_deadline,notif_email_announcement,created_at,updated_at"
)
SENSITIVE_USER_FIELDS = {"password_hash", "password_changed_at", "must_change_password"}


def safe_user_record(record: dict) -> dict:
    """Defense in depth for mutation responses returned by PostgREST."""
    return {key: value for key, value in record.items() if key not in SENSITIVE_USER_FIELDS}


# ─── Supervisors ──────────────────────────────────────────────────────────────

class SupervisorCreateBody(BaseModel):
    email: EmailStr
    name: str
    department_id: str
    max_students: Optional[int] = 30
    areas_of_interest: Optional[str] = None
    phone: Optional[str] = None
    program_ids: Optional[List[str]] = []

class SupervisorUpdateBody(BaseModel):
    name: Optional[str] = None
    department_id: Optional[str] = None
    max_students: Optional[int] = None
    areas_of_interest: Optional[str] = None
    phone: Optional[str] = None
    program_ids: Optional[List[str]] = None
    status: Optional[str] = None


class StudentProfileUpdateBody(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    department_id: Optional[str] = None
    program_id: Optional[str] = None
    semester_id: Optional[str] = None
    session_id: Optional[str] = None
    section_id: Optional[str] = None
    supervisor_id: Optional[str] = None
    project_title: Optional[str] = None
    status: Optional[str] = None
    notif_email_supervisor_assigned: Optional[bool] = None
    notif_email_document_submitted: Optional[bool] = None
    notif_email_review_decision: Optional[bool] = None
    notif_email_comment_posted: Optional[bool] = None
    notif_email_meeting: Optional[bool] = None
    notif_email_deadline: Optional[bool] = None
    notif_email_announcement: Optional[bool] = None


@router.get("/supervisors")
async def list_supervisors(
    department_id: Optional[str] = None,
    program_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """List supervisors without exposing account/contact data to non-admin users."""
    client = get_supabase_client()
    if current_user["role"] == "admin":
        columns = f"{SUPERVISOR_SAFE_COLUMNS}, departments(name, code), supervisor_program_eligibility(program_id, programs(name, code))"
    else:
        columns = "id,name,department_id,max_students,areas_of_interest,status,departments(name, code),supervisor_program_eligibility(program_id, programs(name, code))"
    q = client.table("supervisors").select(columns).order("name")
    if department_id:
        q = q.eq("department_id", department_id)
    if current_user["role"] != "admin":
        q = q.eq("status", "active")
    elif status:
        q = q.eq("status", status)
    result = q.execute()

    data = result.data or []
    
    # Auto-filter by student's department to prevent selecting supervisors from other departments
    if current_user["role"] == "student" and not department_id:
        try:
            student = client.table("students").select("department_id").eq("id", current_user["user_id"]).single().execute()
            if student.data: department_id = student.data["department_id"]
        except Exception: pass

    data = result.data or []
    if department_id:
        data = [s for s in data if s.get("department_id") == department_id]
    return {"supervisors": data}


@router.post("/supervisors")
async def create_supervisor(body: SupervisorCreateBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    department = client.table("departments").select("id").eq("id", body.department_id).eq("status", "active").limit(1).execute()
    if not department.data:
        raise HTTPException(422, detail={"error_code": "INVALID_DEPARTMENT", "message": "Selected department is inactive or invalid."})
    if body.program_ids:
        programs = client.table("programs").select("id").in_("id", list(set(body.program_ids))).eq("department_id", body.department_id).eq("status", "active").execute()
        if len(programs.data or []) != len(set(body.program_ids)):
            raise HTTPException(422, detail={"error_code": "INVALID_PROGRAM", "message": "Every selected program must be active and belong to the selected department."})
    # Check email uniqueness
    existing = client.table("supervisors").select("id").eq("email", body.email.lower()).execute()
    if existing.data:
        raise HTTPException(409, detail={"error_code": "EMAIL_EXISTS", "message": "A supervisor with this email already exists."})

    result = client.table("supervisors").insert({
        "email": body.email.lower(),
        "name": " ".join(body.name.split()),
        "department_id": body.department_id,
        "max_students": 30,
        "areas_of_interest": body.areas_of_interest,
        "phone": body.phone,
        "created_by": current_user["user_id"],
        # This is an official directory record, not an account credential.
        # The matching supervisor claims it via the verified registration flow.
        "password_hash": None,
        "must_change_password": True,
        "approval_status": "approved",
    }).execute()

    supervisor_id = result.data[0]["id"]

    # Set program eligibility
    if body.program_ids:
        eligibility_rows = [{"supervisor_id": supervisor_id, "program_id": pid} for pid in body.program_ids]
        client.table("supervisor_program_eligibility").insert(eligibility_rows).execute()

    await write_audit_log("account_created", current_user["user_id"], "admin", "supervisor", supervisor_id)
    return {"supervisor": safe_user_record(result.data[0])}


@router.put("/supervisors/{supervisor_id}")
async def update_supervisor(supervisor_id: str, body: SupervisorUpdateBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    current = client.table("supervisors").select("id,department_id").eq("id", supervisor_id).limit(1).execute()
    if not current.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Supervisor not found."})
    department_id = body.department_id or current.data[0]["department_id"]
    if body.department_id:
        department = client.table("departments").select("id").eq("id", department_id).eq("status", "active").limit(1).execute()
        if not department.data:
            raise HTTPException(422, detail={"error_code": "INVALID_DEPARTMENT", "message": "Selected department is inactive or invalid."})
    if body.program_ids is not None and body.program_ids:
        programs = client.table("programs").select("id").in_("id", list(set(body.program_ids))).eq("department_id", department_id).eq("status", "active").execute()
        if len(programs.data or []) != len(set(body.program_ids)):
            raise HTTPException(422, detail={"error_code": "INVALID_PROGRAM", "message": "Every selected program must be active and belong to the selected department."})
    update_data = {k: v for k, v in body.dict().items() if v is not None and k != "program_ids"}
    if update_data:
        result = client.table("supervisors").update(update_data).eq("id", supervisor_id).execute()
        if not result.data:
            raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Supervisor not found."})

    # Update program eligibility if provided
    if body.program_ids is not None:
        client.table("supervisor_program_eligibility").delete().eq("supervisor_id", supervisor_id).execute()
        if body.program_ids:
            rows = [{"supervisor_id": supervisor_id, "program_id": pid} for pid in body.program_ids]
            client.table("supervisor_program_eligibility").insert(rows).execute()

    await write_audit_log("account_updated", current_user["user_id"], "admin", "supervisor", supervisor_id)
    return {"message": "Supervisor updated."}


@router.post("/supervisors/{supervisor_id}/deactivate")
async def deactivate_supervisor(supervisor_id: str, reassign_to: Optional[str] = None, current_user: dict = Depends(require_admin)):
    """Deactivate supervisor. If they have active students, reassign_to must be provided."""
    client = get_supabase_client()

    # Check for active assigned students
    active_students = client.table("projects").select("id, student_id").eq("supervisor_id", supervisor_id)\
        .not_.eq("lifecycle_stage", "closed").execute()

    if active_students.data and not reassign_to:
        raise HTTPException(409, detail={
            "error_code": "HAS_ACTIVE_STUDENTS",
            "message": f"Supervisor has {len(active_students.data)} active student(s). Provide reassign_to supervisor ID.",
        })

    if reassign_to and active_students.data:
        # Reassign all projects to new supervisor
        for project in active_students.data:
            client.table("projects").update({"supervisor_id": reassign_to}).eq("id", project["id"]).execute()

    client.table("supervisors").update({"status": "inactive"}).eq("id", supervisor_id).execute()
    await write_audit_log("account_deactivated", current_user["user_id"], "admin", "supervisor", supervisor_id,
                          metadata={"reassigned_to": reassign_to})
    return {"message": "Supervisor deactivated."}


# ─── Students ──────────────────────────────────────────────────────────────────

class StudentCreateBody(BaseModel):
    email: EmailStr
    name: str
    student_number: Optional[str] = None
    department_id: str
    program_id: str
    session_id: str
    section_id: str
    phone: Optional[str] = None

class ProfileImageRequestBody(BaseModel):
    filename: str
    file_size: int
    mime_type: str

class ProfileImageConfirmBody(BaseModel):
    storage_path: str

class AcademicChangeRequestBody(BaseModel):
    department_id: Optional[str] = None
    program_id: Optional[str] = None
    semester_id: Optional[str] = None
    session_id: Optional[str] = None
    section_id: Optional[str] = None
    reason: str

class SupervisorReassignmentRequestBody(BaseModel):
    requested_supervisor_id: Optional[str] = None
    reason: str

class RequestDecisionBody(BaseModel):
    decision: str
    note: Optional[str] = None

@router.get("/students")
async def list_students(
    session_id: Optional[str] = None,
    section_id: Optional[str] = None,
    supervisor_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """List students. Supervisor sees only their assigned students."""
    client = get_supabase_client()
    q = client.table("students").select(
        f"{STUDENT_SAFE_COLUMNS}, departments(name), programs(name), sessions(academic_year), sections(name), "
        "projects(id, title, lifecycle_stage, supervisor_id)"
    ).order("name")

    if current_user["role"] == "supervisor":
        # Only show students assigned to this supervisor
        projects = client.table("projects").select("student_id").eq("supervisor_id", current_user["user_id"]).execute()
        student_ids = [p["student_id"] for p in (projects.data or [])]
        if not student_ids:
            return {"students": []}
        q = q.in_("id", student_ids)
    elif current_user["role"] == "student":
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})

    if session_id:
        q = q.eq("session_id", session_id)
    if section_id:
        q = q.eq("section_id", section_id)
    if status:
        q = q.eq("status", status)

    result = q.execute()
    return {"students": result.data}


@router.post("/students")
async def create_student(body: StudentCreateBody, current_user: dict = Depends(require_admin)):
    client = get_supabase_client()
    temporary_password = generate_temporary_password()
    # Check email uniqueness
    existing = client.table("students").select("id").eq("email", body.email.lower()).execute()
    if existing.data:
        raise HTTPException(409, detail={"error_code": "EMAIL_EXISTS", "message": "A student with this email already exists."})

    # Validate section belongs to session
    section_check = client.table("sections").select("id").eq("id", body.section_id).eq("session_id", body.session_id).execute()
    if not section_check.data:
        raise HTTPException(422, detail={"error_code": "INVALID_SECTION", "message": "Section does not belong to the specified session."})

    result = client.table("students").insert({
        "email": body.email.lower(),
        "name": body.name,
        "student_number": body.student_number,
        "department_id": body.department_id,
        "program_id": body.program_id,
        "session_id": body.session_id,
        "section_id": body.section_id,
        "phone": body.phone,
        "created_by": current_user["user_id"],
        "password_hash": hash_password(temporary_password),
        "must_change_password": True,
    }).execute()

    student_id = result.data[0]["id"]
    # Auto-create project record for lifecycle tracking
    project = client.table("projects").insert({"student_id": student_id}).execute()
    try:
        client.table("project_members").insert({"project_id": project.data[0]["id"], "student_id": student_id}).execute()
    except Exception:
        pass

    await write_audit_log("account_created", current_user["user_id"], "admin", "student", student_id)
    await send_temporary_password_email(body.email.lower(), body.name, temporary_password)
    return {"student": safe_user_record(result.data[0])}


@router.get("/students/{student_id}")
async def get_student(student_id: str, current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    if current_user["role"] == "student" and current_user["user_id"] != student_id:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "You can only view your own profile."})
    if current_user["role"] == "supervisor":
        assignment = client.table("projects").select("id").eq("student_id", student_id) \
            .eq("supervisor_id", current_user["user_id"]).execute()
        if not assignment.data:
            raise HTTPException(403, detail={
                "error_code": "FORBIDDEN", "message": "You can only view students assigned to you."
            })
    result = client.table("students").select(
        f"{STUDENT_SAFE_COLUMNS}, departments(name), programs(name, code), semesters(name), "
        "sessions(academic_year), sections(name), projects(id, title, supervisor_id, supervisors(name))"
    ).eq("id", student_id).single().execute()
    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Student not found."})
    if result.data.get("profile_photo_url"):
        signed=client.storage.from_("profile-images").create_signed_url(result.data["profile_photo_url"],3600)
        result.data["profile_photo_preview_url"]=signed.get("signedURL") or signed.get("signedUrl")
    return {"student": result.data}


@router.patch("/students/{student_id}/profile")
async def update_student_profile(student_id: str, body: StudentProfileUpdateBody, current_user: dict = Depends(get_current_user)):
    """Update permitted relational profile fields after ownership and active-record validation."""
    if current_user["role"] == "supervisor":
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Supervisors cannot update student accounts."})
    if current_user["role"] == "student" and current_user["user_id"] != student_id:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "You can only edit your own profile."})

    # Academic placement and supervisor assignment require reviewed requests;
    # students may only edit personal/contact and notification preferences here.
    allowed_student_fields = {"name", "phone", "notif_email_supervisor_assigned", "notif_email_document_submitted",
                               "notif_email_review_decision", "notif_email_comment_posted",
                               "notif_email_meeting", "notif_email_deadline", "notif_email_announcement"}
    body = body.model_dump(exclude_unset=True)
    if current_user["role"] == "student":
        forbidden = set(body) - allowed_student_fields
        if forbidden:
            raise HTTPException(403, detail={
                "error_code": "FORBIDDEN_FIELDS",
                "message": f"You cannot update: {', '.join(sorted(forbidden))}.",
            })
        if "name" in body:
            body["name"] = (body["name"] or "").strip()
            if len(body["name"]) < 2 or len(body["name"]) > 120:
                raise HTTPException(422, detail={"error_code": "INVALID_NAME", "message": "Name must be between 2 and 120 characters."})

    client = get_supabase_client()
    project_data = {k: body.pop(k) for k in ("supervisor_id", "project_title") if k in body}
    checks = [("department_id","departments",None),("program_id","programs","department_id"),("semester_id","semesters",None),("session_id","sessions","program_id"),("section_id","sections","session_id")]
    for field, table, parent in checks:
        if body.get(field):
            query=client.table(table).select("id").eq("id",body[field]).eq("status","active")
            if parent and body.get(parent): query=query.eq(parent,body[parent])
            if not query.execute().data: raise HTTPException(422,detail={"error_code":"INVALID_SELECTION","message":f"Selected {field.replace('_id','')} is inactive or invalid."})
    if project_data.get("supervisor_id"):
        q=client.table("supervisors").select("id").eq("id",project_data["supervisor_id"]).eq("status","active")
        if body.get("department_id"): q=q.eq("department_id",body["department_id"])
        if not q.execute().data: raise HTTPException(422,detail={"error_code":"INVALID_SUPERVISOR","message":"Selected supervisor is inactive or unavailable."})
    result = client.table("students").update(body).eq("id", student_id).execute()
    if project_data:
        update={}
        if project_data.get("project_title"): update["title"]=project_data["project_title"].strip()
        project = client.table("projects").select("id").eq("student_id", student_id).limit(1).execute()
        if project.data:
            if project_data.get("supervisor_id"):
                try:
                    client.rpc("assign_project_supervisor", {
                        "p_project_id": project.data[0]["id"],
                        "p_supervisor_id": project_data["supervisor_id"],
                        "p_changed_by": current_user["user_id"],
                        "p_reason": "Profile administration update",
                    }).execute()
                except Exception as exc:
                    if "CAPACITY_FULL" in str(exc):
                        raise HTTPException(409, detail={"error_code":"CAPACITY_FULL","message":"This supervisor is now full (30 / 30)."}) from exc
                    raise
            if update: client.table("projects").update(update).eq("id",project.data[0]["id"]).execute()
    await write_audit_log("account_updated", current_user["user_id"], current_user["role"], "student", student_id)
    return {"student": safe_user_record(result.data[0]) if result.data else {}}


@router.post("/students/{student_id}/profile-image/upload-url")
async def profile_image_upload_url(student_id: str, body: ProfileImageRequestBody, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" or current_user["user_id"] != student_id:
        raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"You can only update your own profile picture."})
    if body.mime_type not in {"image/jpeg","image/png","image/webp"} or body.file_size > 2*1024*1024:
        raise HTTPException(422,detail={"error_code":"INVALID_IMAGE","message":"Use a JPG, PNG or WebP image under 2MB."})
    ext={"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}[body.mime_type]
    path=f"students/{student_id}/{uuid.uuid4()}.{ext}"
    signed=get_supabase_client().storage.from_("profile-images").create_signed_upload_url(path)
    return {"upload_url":signed.get("signed_url") or signed.get("signedUrl") or signed.get("signedURL"),"storage_path":path}


@router.post("/students/{student_id}/profile-image/confirm")
async def confirm_profile_image(student_id: str, body: ProfileImageConfirmBody, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" or current_user["user_id"] != student_id or not body.storage_path.startswith(f"students/{student_id}/"):
        raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"Invalid profile image path."})
    client=get_supabase_client()
    parent, filename = body.storage_path.rsplit("/", 1)
    try:
        objects=client.storage.from_("profile-images").list(parent,{"search":filename,"limit":10}) or []
    except Exception:
        raise HTTPException(503,detail={"error_code":"STORAGE_UNAVAILABLE","message":"Could not verify the uploaded image. Please retry."})
    if not any(item.get("name")==filename for item in objects):
        raise HTTPException(422,detail={"error_code":"UPLOAD_NOT_FOUND","message":"Upload the image before confirming it."})
    old=client.table("students").select("profile_photo_url").eq("id",student_id).single().execute().data
    client.table("students").update({"profile_photo_url":body.storage_path}).eq("id",student_id).execute()
    if old and old.get("profile_photo_url") and old["profile_photo_url"]!=body.storage_path:
        try: client.storage.from_("profile-images").remove([old["profile_photo_url"]])
        except Exception: pass
    signed=client.storage.from_("profile-images").create_signed_url(body.storage_path,3600)
    return {"profile_photo_url":body.storage_path,"preview_url":signed.get("signedURL") or signed.get("signedUrl")}


@router.delete("/students/{student_id}/profile-image")
async def remove_profile_image(student_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" or current_user["user_id"] != student_id:
        raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"You can only update your own profile picture."})
    client=get_supabase_client(); row=client.table("students").select("profile_photo_url").eq("id",student_id).single().execute().data
    if row and row.get("profile_photo_url"):
        try: client.storage.from_("profile-images").remove([row["profile_photo_url"]])
        except Exception: pass
    client.table("students").update({"profile_photo_url":None}).eq("id",student_id).execute()
    return {"message":"Profile picture removed."}


@router.post("/students/{student_id}/academic-change-requests", status_code=201)
async def create_academic_change_request(student_id: str, body: AcademicChangeRequestBody,
                                         current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" or current_user["user_id"] != student_id:
        raise HTTPException(403, detail={"error_code":"FORBIDDEN","message":"You can only submit your own request."})
    data=body.model_dump(exclude_none=True); reason=data.pop("reason").strip()
    if len(reason)<10 or not data:
        raise HTTPException(422,detail={"error_code":"INVALID_REQUEST","message":"Select at least one requested change and provide a reason of at least 10 characters."})
    client=get_supabase_client()
    if client.table("student_academic_change_requests").select("id").eq("student_id",student_id).eq("status","pending").execute().data:
        raise HTTPException(409,detail={"error_code":"PENDING_REQUEST_EXISTS","message":"You already have a pending academic change request."})
    row=client.table("student_academic_change_requests").insert({"student_id":student_id,"requested_changes":data,"reason":reason}).execute().data[0]
    await write_audit_log("account_updated",student_id,"student","academic_change_request",row["id"],metadata={"action":"submitted"})
    return {"request":row}

@router.get("/students/{student_id}/academic-change-requests")
async def list_academic_change_requests(student_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] == "student" and current_user["user_id"] != student_id:
        raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"Access denied."})
    if current_user["role"] == "supervisor":
        assigned=get_supabase_client().table("projects").select("id").eq("student_id",student_id).eq("supervisor_id",current_user["user_id"]).execute().data
        if not assigned: raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"Access denied."})
    rows=get_supabase_client().table("student_academic_change_requests").select("id,student_id,requested_changes,reason,status,decision_note,created_at,decided_at").eq("student_id",student_id).order("created_at",desc=True).execute().data or []
    return {"requests":rows}

@router.post("/students/{student_id}/supervisor-reassignment-requests", status_code=201)
async def create_reassignment_request(student_id: str, body: SupervisorReassignmentRequestBody,
                                      current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" or current_user["user_id"] != student_id:
        raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"You can only submit your own request."})
    reason=body.reason.strip()
    if len(reason)<10: raise HTTPException(422,detail={"error_code":"INVALID_REASON","message":"Provide a reason of at least 10 characters."})
    client=get_supabase_client(); project=client.table("projects").select("id,supervisor_id").eq("student_id",student_id).single().execute().data
    if not project: raise HTTPException(404,detail={"error_code":"NOT_FOUND","message":"Project not found."})
    if body.requested_supervisor_id:
        target=client.table("supervisors").select("id").eq("id",body.requested_supervisor_id).eq("status","active").execute().data
        if not target: raise HTTPException(422,detail={"error_code":"INVALID_SUPERVISOR","message":"Requested supervisor is unavailable."})
    if client.table("supervisor_reassignment_requests").select("id").eq("student_id",student_id).eq("status","pending").execute().data:
        raise HTTPException(409,detail={"error_code":"PENDING_REQUEST_EXISTS","message":"You already have a pending reassignment request."})
    row=client.table("supervisor_reassignment_requests").insert({"student_id":student_id,"project_id":project["id"],"current_supervisor_id":project.get("supervisor_id"),"requested_supervisor_id":body.requested_supervisor_id,"reason":reason}).execute().data[0]
    await write_audit_log("account_updated",student_id,"student","supervisor_reassignment_request",row["id"],metadata={"action":"submitted"})
    return {"request":row}

@router.get("/students/{student_id}/supervisor-reassignment-requests")
async def list_reassignment_requests(student_id: str, current_user: dict = Depends(get_current_user)):
    if current_user["role"] == "student" and current_user["user_id"] != student_id: raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"Access denied."})
    if current_user["role"] == "supervisor":
        assigned=get_supabase_client().table("projects").select("id").eq("student_id",student_id).eq("supervisor_id",current_user["user_id"]).execute().data
        if not assigned: raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"Access denied."})
    rows=get_supabase_client().table("supervisor_reassignment_requests").select("id,student_id,project_id,current_supervisor_id,requested_supervisor_id,reason,status,decision_note,created_at,decided_at").eq("student_id",student_id).order("created_at",desc=True).execute().data or []
    return {"requests":rows}


@router.post("/students/bulk-import")
async def bulk_import_students(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_admin)
):
    """
    Bulk CSV import. Validates ALL rows before committing ANY.
    Returns pre-import summary on validation errors.
    CSV columns: name, email, student_number, department_code, program_code, academic_year, section_name
    """
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    client = get_supabase_client()
    errors = []
    valid_rows = []
    required_cols = {"name", "email", "student_number", "department_code", "program_code", "academic_year", "section_name"}

    for i, row in enumerate(reader, start=2):
        row_errors = []
        # Check required columns
        missing = required_cols - set(row.keys())
        if missing:
            errors.append({"row": i, "errors": [f"Missing columns: {', '.join(missing)}"]})
            continue

        # Validate email
        email = (row.get("email") or "").strip().lower()
        if not email or "@" not in email:
            row_errors.append("Invalid email format.")

        # Check for duplicate email
        if email:
            existing = client.table("students").select("id").eq("email", email).execute()
            if existing.data:
                row_errors.append(f"Email {email} already registered.")

        # Look up department
        dept_result = client.table("departments").select("id").eq("code", (row.get("department_code") or "").upper()).execute()
        if not dept_result.data:
            row_errors.append(f"Unknown department code: {row.get('department_code')}")
            dept_id = None
        else:
            dept_id = dept_result.data[0]["id"]

        # Look up program
        prog_result = client.table("programs").select("id").eq("code", (row.get("program_code") or "").upper()).execute()
        if not prog_result.data:
            row_errors.append(f"Unknown program code: {row.get('program_code')}")
            prog_id, session_id, section_id = None, None, None
        else:
            prog_id = prog_result.data[0]["id"]
            # Look up session by academic_year + program
            sess_result = client.table("sessions").select("id").eq("program_id", prog_id)\
                .eq("academic_year", (row.get("academic_year") or "").strip()).execute()
            if not sess_result.data:
                row_errors.append(f"No session found for academic year '{row.get('academic_year')}' in program '{row.get('program_code')}'.")
                session_id, section_id = None, None
            else:
                session_id = sess_result.data[0]["id"]
                # Look up section
                sect_result = client.table("sections").select("id").eq("session_id", session_id)\
                    .eq("name", (row.get("section_name") or "").strip()).execute()
                if not sect_result.data:
                    row_errors.append(f"Section '{row.get('section_name')}' not found in this session.")
                    section_id = None
                else:
                    section_id = sect_result.data[0]["id"]

        if row_errors:
            errors.append({"row": i, "email": email, "errors": row_errors})
        else:
            valid_rows.append({
                "email": email,
                "name": row["name"].strip(),
                "student_number": (row.get("student_number") or "").strip() or None,
                "department_id": dept_id,
                "program_id": prog_id,
                "session_id": session_id,
                "section_id": section_id,
                "created_by": current_user["user_id"],
            })

    if errors:
        return {"success": False, "errors": errors, "valid_count": len(valid_rows), "error_count": len(errors)}

    # All rows valid — commit
    created = []
    for row_data in valid_rows:
        temporary_password = generate_temporary_password()
        row_data["password_hash"] = hash_password(temporary_password)
        row_data["must_change_password"] = True
        result = client.table("students").insert(row_data).execute()
        if result.data:
            student_id = result.data[0]["id"]
            client.table("projects").insert({"student_id": student_id}).execute()
            created.append(safe_user_record(result.data[0]))
            await send_temporary_password_email(row_data["email"], row_data["name"], temporary_password)

    await write_audit_log("account_created", current_user["user_id"], "admin", "students_bulk",
                          metadata={"count": len(created)})
    return {"success": True, "created_count": len(created), "students": created}

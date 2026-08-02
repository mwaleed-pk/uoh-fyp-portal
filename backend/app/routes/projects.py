"""
Projects Routes — project details, supervisor assignment, lifecycle stage.
Lifecycle is always computed by backend, never manually set.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import secrets
from app.utils.auth_middleware import get_current_user, require_admin
from app.db.supabase_client import get_supabase_client
from app.utils.audit import write_audit_log
from app.services.project_service import compute_lifecycle_stage, get_project_for_user
from app.services.notification_service import create_notification

router = APIRouter()


class ProjectUpdateBody(BaseModel):
    title: Optional[str] = None
    idea: Optional[str] = None
    abstract: Optional[str] = None
    category: Optional[str] = None
    keywords: Optional[str] = None


class SupervisorAssignBody(BaseModel):
    supervisor_id: str

class TeamJoinBody(BaseModel):
    invite_code: str = Field(min_length=6, max_length=20)


@router.get("/")
async def list_projects(
    session_id: Optional[str] = None,
    lifecycle_stage: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """List projects per role visibility rules."""
    client = get_supabase_client()
    q = client.table("projects").select(
        "*, students(id, name, email, student_number, sections(name), sessions(academic_year)), "
        "supervisors(id, name, email)"
    )

    if current_user["role"] == "student":
        project_ids = []
        try:
            memberships = client.table("project_members").select("project_id").eq("student_id", current_user["user_id"]).execute()
            project_ids = [row["project_id"] for row in memberships.data or []]
        except Exception:
            pass
        if project_ids:
            q = q.in_("id", project_ids)
        else:
            q = q.eq("student_id", current_user["user_id"])
    elif current_user["role"] == "supervisor":
        q = q.eq("supervisor_id", current_user["user_id"])
    # admin: no filter

    if lifecycle_stage:
        q = q.eq("lifecycle_stage", lifecycle_stage)

    result = q.execute()
    return {"projects": result.data}


@router.get("/{project_id}")
async def get_project(project_id: str, current_user: dict = Depends(get_current_user)):
    project = await get_project_for_user(project_id, current_user)
    return {"project": project}


@router.put("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdateBody, current_user: dict = Depends(get_current_user)):
    """Student updates their own project details. Admin can update any."""
    client = get_supabase_client()

    project = await get_project_for_user(project_id, current_user)

    if current_user["role"] not in {"student", "admin"}:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Only the student or an administrator can edit project details."})

    # get_project_for_user has already verified owner or team membership.

    # Closed projects cannot be edited
    if project["lifecycle_stage"] == "closed":
        raise HTTPException(409, detail={"error_code": "PROJECT_CLOSED", "message": "This project has been closed and cannot be edited."})

    update_data = {k: v for k, v in body.dict().items() if v is not None}
    if not update_data:
        return {"project": project}

    result = client.table("projects").update(update_data).eq("id", project_id).execute()

    # Recompute lifecycle stage
    await compute_lifecycle_stage(project_id)
    await write_audit_log("project_updated", current_user["user_id"], current_user["role"], "project", project_id)
    return {"project": result.data[0]}


@router.get("/{project_id}/team")
async def project_team(project_id: str, current_user: dict = Depends(get_current_user)):
    project = await get_project_for_user(project_id, current_user)
    client = get_supabase_client()
    if not project.get("invite_code"):
        for _ in range(5):
            code = secrets.token_hex(4).upper()
            try:
                updated = client.table("projects").update({"invite_code": code}).eq("id", project_id).is_("invite_code", "null").execute()
                if updated.data: project["invite_code"] = code
                else: project = client.table("projects").select("invite_code").eq("id", project_id).single().execute().data
                break
            except Exception:
                continue
    try:
        members = client.table("project_members").select("student_id,joined_at,students(id,name,email,student_number)") \
            .eq("project_id", project_id).order("joined_at").execute().data or []
    except Exception as exc:
        raise HTTPException(503, detail={"error_code":"TEAM_MIGRATION_REQUIRED","message":"Project teams require the supplied Supabase migration."}) from exc
    return {"project_id": project_id, "invite_code": project.get("invite_code"), "members": members,
            "supervisor": project.get("supervisors")}


@router.post("/team/join")
async def join_team(body: TeamJoinBody, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student":
        raise HTTPException(403, detail={"error_code":"FORBIDDEN","message":"Only students can join a project team."})
    client = get_supabase_client()
    try:
        result = client.rpc("join_project_team", {"p_student_id": current_user["user_id"],
                                                   "p_invite_code": body.invite_code.strip().upper()}).execute()
    except Exception as exc:
        message = str(exc)
        if "INVITE_CODE_INVALID" in message:
            raise HTTPException(404, detail={"error_code":"INVITE_CODE_INVALID","message":"Invite code is invalid or the project is closed."}) from exc
        if "STUDENT_PROJECT_ALREADY_STARTED" in message:
            raise HTTPException(409, detail={"error_code":"PROJECT_ALREADY_STARTED","message":"Your current project already contains details or documents and cannot be replaced automatically."}) from exc
        raise HTTPException(503, detail={"error_code":"TEAM_MIGRATION_REQUIRED","message":"Project teams require the supplied Supabase migration."}) from exc
    return {"message":"Team joined successfully. Project, supervisor and submissions are now shared.",
            "project_id": result.data}


@router.post("/{project_id}/assign-supervisor")
async def assign_supervisor(project_id: str, body: SupervisorAssignBody, current_user: dict = Depends(get_current_user)):
    """
    Assign (or self-select) a supervisor.
    Student can select from eligible list. Admin can override.
    Enforces capacity limits.
    """
    client = get_supabase_client()
    project = await get_project_for_user(project_id, current_user)

    # Student can only assign to their own project
    if current_user["role"] == "student" and project["student_id"] != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})

    old_supervisor_id = project.get("supervisor_id")

    # Supervisor cannot be assigned by supervisor role
    if current_user["role"] == "supervisor":
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Supervisors cannot make assignments."})

    # Validate supervisor exists and is active
    sup_result = client.table("supervisors").select("id, name, email, max_students, department_id").eq("id", body.supervisor_id).eq("status", "active").execute()
    if not sup_result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Supervisor not found or inactive."})
    supervisor = sup_result.data[0]

    # Student self-selection is limited to their department and approved program eligibility.
    if current_user["role"] == "student":
        student = client.table("students").select("department_id, program_id").eq("id", current_user["user_id"]).single().execute()
        if not student.data or supervisor["department_id"] != student.data["department_id"]:
            raise HTTPException(403, detail={"error_code": "INELIGIBLE_SUPERVISOR", "message": "This supervisor is not eligible for your department."})


    # The database RPC locks the target supervisor row and performs the final
    # capacity check atomically, preventing two concurrent requests from both
    # claiming slot 30.
    try:
        client.rpc("assign_project_supervisor", {
            "p_project_id": project_id,
            "p_supervisor_id": body.supervisor_id,
            "p_changed_by": current_user["user_id"] if current_user["role"] == "admin" else None,
            "p_reason": "Student selection" if current_user["role"] == "student" else "Administrator assignment",
        }).execute()
    except Exception as exc:
        message = str(exc)
        if "CAPACITY_FULL" in message:
            raise HTTPException(409, detail={"error_code": "CAPACITY_FULL", "message": "This supervisor is now full (30 / 30). Please select another supervisor."}) from exc
        if "SUPERVISOR_UNAVAILABLE" in message:
            raise HTTPException(422, detail={"error_code": "SUPERVISOR_UNAVAILABLE", "message": "Selected supervisor is inactive or unavailable."}) from exc
        raise
    await compute_lifecycle_stage(project_id)

    # Get student info for notifications
    student = client.table("students").select("id, name, email").eq("id", project["student_id"]).single().execute()

    # Notify both parties
    await create_notification(
        user_id=project["student_id"], user_role="student",
        event_type="supervisor_assigned",
        title="Supervisor Assigned",
        body=f"{supervisor['name']} has been assigned as your supervisor.",
        resource_id=project_id,
    )
    await create_notification(
        user_id=body.supervisor_id, user_role="supervisor",
        event_type="supervisor_assigned",
        title="New Student Assigned",
        body=f"{student.data['name']} has been assigned to you for FYP supervision.",
        resource_id=project_id,
    )
    
    if old_supervisor_id and old_supervisor_id != body.supervisor_id:
        await create_notification(
            user_id=old_supervisor_id, user_role="supervisor",
            event_type="supervisor_removed",
            title="Student Reassigned",
            body=f"{student.data['name']} has changed their supervisor. They are no longer assigned to you.",
            resource_id=project_id,
        )

    await write_audit_log("supervisor_assigned", current_user["user_id"], current_user["role"], "project", project_id,
                          metadata={"supervisor_id": body.supervisor_id, "old_supervisor_id": old_supervisor_id})
    return {"message": "Supervisor assigned successfully."}


@router.post("/{project_id}/close")
async def close_project(project_id: str, current_user: dict = Depends(require_admin)):
    """Admin-only: close a project (freezes uploads/decisions, preserves history)."""
    client = get_supabase_client()
    from datetime import datetime, timezone
    client.table("projects").update({
        "lifecycle_stage": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "closed_by": current_user["user_id"],
    }).eq("id", project_id).execute()
    await write_audit_log("project_updated", current_user["user_id"], "admin", "project", project_id,
                          metadata={"action": "closed"})
    return {"message": "Project closed."}

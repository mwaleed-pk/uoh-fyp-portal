"""
Project Service — lifecycle computation, project authorization helper.
Lifecycle is ALWAYS computed from events, never manually set (PRD Section 39).
"""
from fastapi import HTTPException
from app.db.supabase_client import get_supabase_client
import structlog

logger = structlog.get_logger(__name__)


LIFECYCLE_STAGES = [
    "not_started",
    "details_submitted",
    "supervisor_assigned",
    "awaiting_review",
    "revision_requested",
    "approved",
    "closed",
]


async def compute_lifecycle_stage(project_id: str) -> str:
    """
    Compute and persist the correct lifecycle stage for a project.
    Derived purely from data state — never manually set by any user.

    Logic (PRD Section 39):
    - not_started: no title/abstract
    - details_submitted: has title+abstract but no supervisor
    - supervisor_assigned: has supervisor, no documents uploaded
    - awaiting_review: has unreviewed document (no approval on latest version)
    - revision_requested: latest approval decision is 'revision_requested' or 'rejected'
    - approved: latest approval decision is 'approved'
    - closed: explicitly closed (set by Admin, not computed)
    """
    client = get_supabase_client()
    project = client.table("projects").select("*").eq("id", project_id).single().execute()
    if not project.data:
        return "not_started"

    p = project.data

    # Closed overrides everything
    if p["lifecycle_stage"] == "closed":
        return "closed"

    # Step 1: Check details
    has_details = bool(p.get("title") and p.get("abstract"))
    if not has_details:
        new_stage = "not_started"
    elif not p.get("supervisor_id"):
        new_stage = "details_submitted"
    else:
        # Has supervisor — check documents
        docs = client.table("project_documents").select("id, version_number")\
            .eq("project_id", project_id).order("version_number", desc=True).limit(1).execute()

        if not docs.data:
            new_stage = "supervisor_assigned"
        else:
            latest_doc_id = docs.data[0]["id"]
            # Check if there's an approval on the latest document
            approval = client.table("approvals").select("decision")\
                .eq("document_id", latest_doc_id).order("created_at", desc=True).limit(1).execute()

            if not approval.data:
                new_stage = "awaiting_review"
            elif approval.data[0]["decision"] == "approved":
                new_stage = "approved"
            else:
                new_stage = "revision_requested"

    # Persist if changed
    if new_stage != p["lifecycle_stage"]:
        client.table("projects").update({"lifecycle_stage": new_stage}).eq("id", project_id).execute()
        logger.info("lifecycle_updated", project_id=project_id, from_stage=p["lifecycle_stage"], to_stage=new_stage)

    return new_stage


async def get_project_for_user(project_id: str, current_user: dict) -> dict:
    """
    Fetch a project, enforcing role-based visibility (PRD Section 9).
    Raises 403 if the caller is not authorized to see this project.
    """
    client = get_supabase_client()
    result = client.table("projects").select(
        "*, students(id, name, email, student_number), supervisors(id, name, email)"
    ).eq("id", project_id).single().execute()

    if not result.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Project not found."})

    project = result.data

    if current_user["role"] == "student" and project["student_id"] != current_user["user_id"]:
        try:
            member = client.table("project_members").select("student_id").eq("project_id", project_id) \
                .eq("student_id", current_user["user_id"]).limit(1).execute()
        except Exception:
            member = None
        if not member or not member.data:
            raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "You can only view your project or joined team project."})

    if current_user["role"] == "supervisor" and project["supervisor_id"] != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "You can only view projects assigned to you."})

    return project

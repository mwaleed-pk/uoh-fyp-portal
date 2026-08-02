"""Reviews, Comments, Meetings, Notifications, Announcements routes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from app.utils.auth_middleware import get_current_user, require_admin, require_supervisor_or_admin
from app.db.supabase_client import get_supabase_client
from app.utils.audit import write_audit_log
from app.services.project_service import compute_lifecycle_stage, get_project_for_user
from app.services.notification_service import create_notification

router = APIRouter()


# ─── Review Decision ─────────────────────────────────────────────────────────

class ReviewBody(BaseModel):
    document_id: str
    decision: str  # approved | rejected | revision_requested
    comment: Optional[str] = None


@router.post("/decisions")
async def record_decision(body: ReviewBody, current_user: dict = Depends(get_current_user)):
    """Supervisor records a review decision on a document version."""
    if current_user["role"] != "supervisor":
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Only supervisors can record review decisions."})

    valid_decisions = {"under_review", "approved", "rejected", "revision_requested"}
    if body.decision not in valid_decisions:
        raise HTTPException(422, detail={"error_code": "INVALID_DECISION",
                                          "message": f"Decision must be one of: {', '.join(valid_decisions)}"})

    # Non-approved decisions require a comment (PRD Section 33)
    if body.decision in {"rejected", "revision_requested"} and (not body.comment or len(body.comment.strip()) < 10):
        raise HTTPException(422, detail={"error_code": "COMMENT_REQUIRED",
                                          "message": "A comment of at least 10 characters is required for Rejected or Revision Requested decisions."})

    client = get_supabase_client()
    doc = client.table("project_documents").select("*, projects(id, student_id, supervisor_id, title)").eq("id", body.document_id).single().execute()
    if not doc.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Document not found."})

    project = doc.data.get("projects", {})
    if project.get("supervisor_id") != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "You can only review documents for your assigned students."})

    result = client.table("approvals").insert({
        "document_id": body.document_id,
        "supervisor_id": current_user["user_id"],
        "decision": body.decision,
        "comment": body.comment,
    }).execute()
    client.table("project_documents").update({"status": body.decision, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", body.document_id).execute()

    # Recompute lifecycle
    await compute_lifecycle_stage(project["id"])

    # Notify student
    decision_label = body.decision.replace("_", " ").title()
    await create_notification(
        user_id=project["student_id"],
        user_role="student",
        event_type="review_decision",
        title=f"Review Decision: {decision_label}",
        body=f"Your supervisor has reviewed your submission. Decision: {decision_label}." +
             (f" Comment: {body.comment}" if body.comment else ""),
        resource_id=project["id"],
    )

    await write_audit_log("review_recorded", current_user["user_id"], "supervisor", "approval", result.data[0]["id"],
                          metadata={"decision": body.decision, "document_id": body.document_id})
    return {"approval": result.data[0]}


class CommentBody(BaseModel):
    project_id: str
    content: str
    document_id: Optional[str] = None
    parent_comment_id: Optional[str] = None


@router.get("/comments")
async def list_comments(project_id: str, document_id: Optional[str] = None,
                        current_user: dict = Depends(get_current_user)):
    """List only comments belonging to a project visible to the caller."""
    await get_project_for_user(project_id, current_user)
    client = get_supabase_client()
    query = client.table("comments").select("*").eq("project_id", project_id).order("created_at")
    if document_id:
        query = query.eq("document_id", document_id)
    result = query.execute()
    return {"comments": result.data or []}


@router.post("/comments")
async def post_comment(body: CommentBody, current_user: dict = Depends(get_current_user)):
    project = await get_project_for_user(body.project_id, current_user)
    if current_user["role"] not in ("student", "supervisor", "admin"):
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})
    content = body.content.strip()
    if not content or len(content) > 5000:
        raise HTTPException(422, detail={"error_code": "INVALID_COMMENT", "message": "Comment must be between 1 and 5000 characters."})
    client = get_supabase_client()
    if body.document_id:
        doc = client.table("project_documents").select("id").eq("id", body.document_id).eq("project_id", body.project_id).execute()
        if not doc.data:
            raise HTTPException(422, detail={"error_code": "INVALID_DOCUMENT", "message": "Document does not belong to this project."})
    result = client.table("comments").insert({
        "project_id": body.project_id, "document_id": body.document_id,
        "parent_comment_id": body.parent_comment_id, "author_id": current_user["user_id"],
        "author_role": current_user["role"], "content": content,
    }).execute()
    recipient = project["supervisor_id"] if current_user["role"] == "student" else project["student_id"]
    recipient_role = "supervisor" if current_user["role"] == "student" else "student"
    if recipient:
        await create_notification(recipient, recipient_role, "comment_posted", "New Project Comment",
                                  "A new comment was added to your FYP project.", body.project_id)
    await write_audit_log("comment_posted", current_user["user_id"], current_user["role"], "comment", result.data[0]["id"])
    return {"comment": result.data[0]}


@router.post("/comments/{comment_id}/retract")
async def retract_comment(comment_id: str, current_user: dict = Depends(get_current_user)):
    client = get_supabase_client()
    comment = client.table("comments").select("*").eq("id", comment_id).single().execute()
    if not comment.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Comment not found."})
    await get_project_for_user(comment.data["project_id"], current_user)
    if current_user["role"] != "admin" and comment.data["author_id"] != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "You can only retract your own comment."})
    from datetime import datetime, timezone
    result = client.table("comments").update({"retracted": True, "content": "", "retracted_at": datetime.now(timezone.utc).isoformat()}).eq("id", comment_id).execute()
    await write_audit_log("comment_retracted", current_user["user_id"], current_user["role"], "comment", comment_id)
    return {"comment": result.data[0] if result.data else {"id": comment_id, "retracted": True}}

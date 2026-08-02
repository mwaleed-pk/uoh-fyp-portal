"""Meetings routes — propose, accept, reschedule, cancel, outcome notes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from app.utils.auth_middleware import get_current_user
from app.db.supabase_client import get_supabase_client
from app.utils.audit import write_audit_log
from app.services.notification_service import create_notification
from app.services.project_service import get_project_for_user

router = APIRouter()


class MeetingProposeBody(BaseModel):
    project_id: str
    proposed_time: str  # ISO datetime
    agenda: Optional[str] = None
    location: Optional[str] = None


class MeetingRespondBody(BaseModel):
    status: str  # accepted | rescheduled | cancelled
    new_time: Optional[str] = None  # required if rescheduled
    cancel_reason: Optional[str] = None  # required if cancelled


class OutcomeBody(BaseModel):
    outcome_notes: str


def _student_has_project_access(client, project: dict, student_id: str) -> bool:
    if project.get("student_id") == student_id:
        return True
    project_id = project.get("id")
    if not project_id:
        return False
    try:
        membership = client.table("project_members").select("student_id").eq("project_id", project_id) \
            .eq("student_id", student_id).limit(1).execute()
        return bool(membership.data)
    except Exception:
        return False


@router.post("/")
async def propose_meeting(body: MeetingProposeBody, current_user: dict = Depends(get_current_user)):
    """Either student or supervisor can propose a meeting."""
    client = get_supabase_client()

    # Validate proposed time is in the future
    try:
        proposed_dt = datetime.fromisoformat(body.proposed_time.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, detail={"error_code": "INVALID_DATE", "message": "Invalid proposed_time format."})
    if proposed_dt.tzinfo is None:
        proposed_dt = proposed_dt.replace(tzinfo=timezone.utc)

    if proposed_dt <= datetime.now(timezone.utc):
        raise HTTPException(422, detail={"error_code": "PAST_TIME", "message": "Meeting cannot be proposed for a past time."})

    project = await get_project_for_user(body.project_id, current_user)
    if current_user["role"] == "student" and not project.get("supervisor_id"):
        raise HTTPException(409, detail={"error_code": "SUPERVISOR_REQUIRED", "message": "Assign a supervisor before proposing a meeting."})
    if project["lifecycle_stage"] == "closed":
        raise HTTPException(409, detail={"error_code": "PROJECT_CLOSED", "message": "Cannot propose meetings for closed projects."})

    result = client.table("meetings").insert({
        "project_id": body.project_id,
        "proposed_by": current_user["role"],
        "proposed_time": body.proposed_time,
        "agenda": body.agenda,
        "location": body.location,
        "status": "proposed",
    }).execute()

    meeting_id = result.data[0]["id"]

    # Notify the other party
    other_id = project["supervisor_id"] if current_user["role"] == "student" else project["student_id"]
    other_role = "supervisor" if current_user["role"] == "student" else "student"
    if other_id:
        await create_notification(
            user_id=other_id, user_role=other_role,
            event_type="meeting_proposed",
            title="Meeting Proposed",
            body=f"A meeting has been proposed for {proposed_dt.strftime('%b %d, %Y at %I:%M %p')}.",
            resource_id=meeting_id,
        )

    await write_audit_log("meeting_proposed", current_user["user_id"], current_user["role"], "meeting", meeting_id)
    return {"meeting": result.data[0]}


@router.patch("/{meeting_id}")
async def respond_to_meeting(meeting_id: str, body: MeetingRespondBody, current_user: dict = Depends(get_current_user)):
    """Accept, reschedule, or cancel a meeting."""
    client = get_supabase_client()
    meeting = client.table("meetings").select("*, projects(id, student_id, supervisor_id)").eq("id", meeting_id).single().execute()
    if not meeting.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Meeting not found."})

    m = meeting.data
    project = m.get("projects", {})

    # Authorization
    if current_user["role"] == "student":
        if not _student_has_project_access(client, project, current_user["user_id"]):
            raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})
    if current_user["role"] == "supervisor" and project.get("supervisor_id") != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})

    valid_statuses = {"accepted", "rescheduled", "cancelled"}
    if body.status not in valid_statuses:
        raise HTTPException(422, detail={"error_code": "INVALID_STATUS", "message": f"Status must be one of: {', '.join(valid_statuses)}"})

    if body.status == "cancelled" and not body.cancel_reason:
        raise HTTPException(422, detail={"error_code": "REASON_REQUIRED", "message": "A reason is required when cancelling a meeting."})

    if body.status == "rescheduled" and not body.new_time:
        raise HTTPException(422, detail={"error_code": "NEW_TIME_REQUIRED", "message": "A new time is required when rescheduling."})

    if body.status == "rescheduled":
        try:
            new_dt = datetime.fromisoformat(body.new_time.replace("Z", "+00:00"))
            if new_dt.tzinfo is None:
                new_dt = new_dt.replace(tzinfo=timezone.utc)
        except (AttributeError, ValueError):
            raise HTTPException(422, detail={"error_code": "INVALID_DATE", "message": "Invalid new_time format."})
        if new_dt <= datetime.now(timezone.utc):
            raise HTTPException(422, detail={"error_code": "PAST_TIME", "message": "A rescheduled meeting must be in the future."})

    update_data = {"status": body.status}
    if body.new_time:
        update_data["proposed_time"] = body.new_time
    if body.cancel_reason:
        update_data["cancel_reason"] = body.cancel_reason

    client.table("meetings").update(update_data).eq("id", meeting_id).execute()

    # Notify other party
    other_id = project.get("supervisor_id") if current_user["role"] == "student" else project.get("student_id")
    other_role = "supervisor" if current_user["role"] == "student" else "student"
    event_map = {"accepted": "meeting_accepted", "rescheduled": "meeting_rescheduled", "cancelled": "meeting_cancelled"}
    if other_id:
        await create_notification(
            user_id=other_id, user_role=other_role,
            event_type=event_map[body.status],
            title=f"Meeting {body.status.title()}",
            body=f"The meeting has been {body.status}." + (f" Reason: {body.cancel_reason}" if body.cancel_reason else ""),
            resource_id=meeting_id,
        )

    await write_audit_log(f"meeting_{body.status}", current_user["user_id"], current_user["role"], "meeting", meeting_id)
    return {"message": f"Meeting {body.status}."}


@router.patch("/{meeting_id}/outcome")
async def add_outcome(meeting_id: str, body: OutcomeBody, current_user: dict = Depends(get_current_user)):
    """Add outcome notes after a meeting has taken place."""
    client = get_supabase_client()
    meeting = client.table("meetings").select("*, projects(id, student_id, supervisor_id)").eq("id", meeting_id).single().execute()
    if not meeting.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Meeting not found."})

    record = meeting.data
    project = record.get("projects") or {}
    role = current_user["role"]
    if role == "student":
        if not _student_has_project_access(client, project, current_user["user_id"]):
            raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})
    if role == "supervisor" and project.get("supervisor_id") != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})
    if role not in {"student", "supervisor", "admin"}:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})

    if record.get("status") not in {"accepted", "rescheduled"}:
        raise HTTPException(409, detail={"error_code": "INVALID_MEETING_STATE", "message": "Only an accepted meeting can be completed."})
    try:
        scheduled_at = datetime.fromisoformat(record["proposed_time"].replace("Z", "+00:00"))
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    except (KeyError, AttributeError, ValueError):
        raise HTTPException(409, detail={"error_code": "INVALID_MEETING_TIME", "message": "Meeting time is invalid."})
    if scheduled_at > datetime.now(timezone.utc):
        raise HTTPException(409, detail={"error_code": "MEETING_NOT_OCCURRED", "message": "Outcome notes can only be added after the scheduled meeting time."})

    notes = body.outcome_notes.strip()
    if not notes:
        raise HTTPException(422, detail={"error_code": "OUTCOME_REQUIRED", "message": "Outcome notes are required."})
    client.table("meetings").update({
        "outcome_notes": notes,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", meeting_id).execute()
    await write_audit_log("meeting_completed", current_user["user_id"], current_user["role"], "meeting", meeting_id)
    return {"message": "Outcome notes saved."}


@router.get("/project/{project_id}")
async def list_meetings(project_id: str, current_user: dict = Depends(get_current_user)):
    await get_project_for_user(project_id, current_user)
    client = get_supabase_client()
    result = client.table("meetings").select("*").eq("project_id", project_id).order("proposed_time", desc=True).execute()
    return {"meetings": result.data}

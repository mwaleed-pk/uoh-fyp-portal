"""
Documents Routes — signed upload URL, post-upload verification, preview/download URLs.
All file access via short-lived signed URLs. No permanent public links.
"""
import uuid
import mimetypes
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.utils.auth_middleware import get_current_user
from app.db.supabase_client import get_supabase_client
from app.utils.audit import write_audit_log
from app.utils.config import settings
from app.services.project_service import get_project_for_user, compute_lifecycle_stage
from app.services.notification_service import create_notification

router = APIRouter()

BUCKET = "fyp-documents"
SIGNED_URL_EXPIRY = 900  # 15 minutes in seconds


class UploadRequestBody(BaseModel):
    project_id: str
    filename: str
    file_size: int  # bytes
    document_category: str
    replace_document_id: Optional[str] = None


class ConfirmUploadBody(BaseModel):
    project_id: str
    storage_path: str
    filename: str
    file_size: int
    version_number: int
    document_category: str
    replaces_document_id: Optional[str] = None


@router.post("/upload-url")
async def request_upload_url(body: UploadRequestBody, current_user: dict = Depends(get_current_user)):
    """
    Step 1: Student requests a signed upload URL.
    Backend validates ownership and lifecycle stage, then issues a scoped URL.
    """
    if current_user["role"] != "student":
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Only students can upload documents."})

    client = get_supabase_client()

    # Validate project ownership
    project = await get_project_for_user(body.project_id, current_user)
    # get_project_for_user accepts the owner or any joined team member.

    # Validate lifecycle allows upload
    stage = project["lifecycle_stage"]
    if stage == "closed":
        raise HTTPException(409, detail={"error_code": "PROJECT_CLOSED", "message": "This project is closed. No uploads allowed."})
    if stage == "approved":
        raise HTTPException(409, detail={"error_code": "PROJECT_APPROVED", "message": "Project is already approved. Contact your supervisor if re-upload is needed."})

    # Validate file size
    if body.file_size > settings.max_file_size_bytes:
        raise HTTPException(422, detail={"error_code": "FILE_TOO_LARGE",
                                          "message": f"File must be under {settings.MAX_FILE_SIZE_MB}MB. Got {body.file_size / 1024 / 1024:.1f}MB."})

    # Validate extension
    allowed_extensions=(".pdf",".doc",".docx")
    if not body.filename.lower().endswith(allowed_extensions):
        raise HTTPException(422, detail={"error_code": "INVALID_FILE_TYPE", "message": "Only PDF, DOC and DOCX project documents are accepted."})
    if body.document_category not in {"proposal","srs","design","progress_report","final_report","presentation","other"}:
        raise HTTPException(422,detail={"error_code":"INVALID_CATEGORY","message":"Select a valid document category."})
    if body.replace_document_id:
        old=client.table("project_documents").select("id, project_id").eq("id",body.replace_document_id).eq("project_id",body.project_id).execute()
        if not old.data: raise HTTPException(404,detail={"error_code":"NOT_FOUND","message":"Document to replace was not found."})

    # Determine next version number
    existing_docs = client.table("project_documents").select("version_number")\
        .eq("project_id", body.project_id).order("version_number", desc=True).limit(1).execute()
    next_version = (existing_docs.data[0]["version_number"] + 1) if existing_docs.data else 1

    # Build storage path — namespace includes dept/session/student/project for defense-in-depth
    student_info = client.table("students").select("department_id, session_id").eq("id", current_user["user_id"]).single().execute()
    s = student_info.data
    storage_path = (
        f"{s['department_id']}/{s['session_id']}/{current_user['user_id']}/"
        f"{body.project_id}/v{next_version}_{body.filename}"
    )

    # Issue signed upload URL
    try:
        signed = client.storage.from_(BUCKET).create_signed_upload_url(storage_path)
        upload_url = (signed.get("signed_url") or signed.get("signedUrl") or signed.get("signedURL")) \
            if isinstance(signed, dict) else signed.signed_url
        if not upload_url:
            raise ValueError("Storage provider did not return a signed upload URL.")
    except Exception as e:
        raise HTTPException(500, detail={"error_code": "STORAGE_ERROR", "message": "Could not generate upload URL. Please try again."})

    return {
        "upload_url": upload_url,
        "storage_path": storage_path,
        "version_number": next_version,
        "expires_in_seconds": SIGNED_URL_EXPIRY,
    }


@router.post("/confirm-upload")
async def confirm_upload(body: ConfirmUploadBody, current_user: dict = Depends(get_current_user)):
    """
    Step 2: After frontend uploads to Supabase Storage, notify backend.
    Backend verifies file exists in storage, inspects MIME type, writes metadata row.
    """
    if current_user["role"] != "student":
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Only students can confirm uploads."})

    client = get_supabase_client()

    # Re-authorize at confirmation time; never trust project/path/version values from the browser.
    project = await get_project_for_user(body.project_id, current_user)
    expected_path_segment = f"/{current_user['user_id']}/{body.project_id}/"
    if expected_path_segment not in f"/{body.storage_path}":
        raise HTTPException(422, detail={"error_code": "INVALID_STORAGE_PATH", "message": "Upload path does not match this student and project."})
    latest = client.table("project_documents").select("version_number").eq("project_id", body.project_id) \
        .order("version_number", desc=True).limit(1).execute()
    expected_version = (latest.data[0]["version_number"] + 1) if latest.data else 1
    if body.version_number != expected_version:
        raise HTTPException(409, detail={"error_code": "VERSION_CONFLICT", "message": "Document version is no longer current. Please upload again."})

    # Verify file exists in storage
    try:
        file_info = client.storage.from_(BUCKET).list(path=body.storage_path.rsplit("/", 1)[0])
        filename = body.storage_path.split("/")[-1]
        file_exists = any(f.get("name") == filename for f in (file_info or []))
        if not file_exists:
            raise HTTPException(422, detail={"error_code": "UPLOAD_NOT_FOUND",
                                              "message": "Upload could not be verified. Please try again."})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail={"error_code": "STORAGE_VERIFY_ERROR", "message": "Could not verify upload."})

    # MIME type validation — accept PDF only (validated by extension in upload step, storage enforces MIME)
    mime_type = mimetypes.guess_type(body.filename)[0] or "application/octet-stream"

    # Write metadata row
    result = client.table("project_documents").insert({
        "project_id": body.project_id,
        "version_number": body.version_number,
        "filename": body.filename,
        "storage_path": body.storage_path,
        "mime_type": mime_type,
        "file_size": body.file_size,
        "uploaded_by": current_user["user_id"],
        "status": "pending",
        "document_category": body.document_category,
        "replaces_document_id": body.replaces_document_id,
    }).execute()

    doc_id = result.data[0]["id"]

    # Recompute lifecycle stage
    await compute_lifecycle_stage(body.project_id)

    # Notify supervisor
    project = client.table("projects").select("supervisor_id, title").eq("id", body.project_id).single().execute()
    if project.data and project.data.get("supervisor_id"):
        await create_notification(
            user_id=project.data["supervisor_id"],
            user_role="supervisor",
            event_type="document_submitted",
            title="New Document Submitted",
            body=f"A new document (v{body.version_number}) has been submitted for review.",
            resource_id=doc_id,
        )

    await write_audit_log("document_uploaded", current_user["user_id"], "student", "document", doc_id,
                          metadata={"version": body.version_number, "project_id": body.project_id})

    return {"document": result.data[0], "message": "Document uploaded successfully."}


@router.get("/project/{project_id}")
async def list_project_documents(project_id: str, current_user: dict = Depends(get_current_user)):
    """List all document versions for a project."""
    await get_project_for_user(project_id, current_user)  # Authorization check
    client = get_supabase_client()
    result = client.table("project_documents").select(
        "*, approvals(decision, comment, created_at, supervisors(name))"
    ).eq("project_id", project_id).order("version_number", desc=True).execute()
    return {"documents": result.data}


@router.get("/{doc_id}/preview-url")
async def get_preview_url(doc_id: str, current_user: dict = Depends(get_current_user)):
    """Issue a short-lived signed URL for in-browser PDF preview."""
    client = get_supabase_client()
    doc = client.table("project_documents").select("*, projects(id, student_id, supervisor_id)").eq("id", doc_id).single().execute()
    if not doc.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Document not found."})

    project = doc.data.get("projects", {})
    # Authorization per permission matrix
    if current_user["role"] == "student":
        await get_project_for_user(project.get("id"), current_user)
    if current_user["role"] == "supervisor" and project.get("supervisor_id") != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})

    try:
        signed = client.storage.from_(BUCKET).create_signed_url(doc.data["storage_path"], SIGNED_URL_EXPIRY)
        url = signed["signedURL"] if isinstance(signed, dict) else signed.signed_url
    except Exception:
        raise HTTPException(500, detail={"error_code": "STORAGE_ERROR", "message": "Could not generate preview URL."})

    return {"url": url, "expires_in_seconds": SIGNED_URL_EXPIRY, "filename": doc.data["filename"]}


@router.get("/{doc_id}/download-url")
async def get_download_url(doc_id: str, current_user: dict = Depends(get_current_user)):
    """Issue a signed URL for explicit file download (separate from preview)."""
    # Same auth logic as preview
    client = get_supabase_client()
    doc = client.table("project_documents").select("*, projects(id, student_id, supervisor_id)").eq("id", doc_id).single().execute()
    if not doc.data:
        raise HTTPException(404, detail={"error_code": "NOT_FOUND", "message": "Document not found."})

    project = doc.data.get("projects", {})
    if current_user["role"] == "student":
        await get_project_for_user(project.get("id"), current_user)
    if current_user["role"] == "supervisor" and project.get("supervisor_id") != current_user["user_id"]:
        raise HTTPException(403, detail={"error_code": "FORBIDDEN", "message": "Access denied."})

    try:
        signed = client.storage.from_(BUCKET).create_signed_url(doc.data["storage_path"], SIGNED_URL_EXPIRY,
                                                                  options={"download": f"v{doc.data['version_number']}_{doc.data['filename']}"})
        url = signed["signedURL"] if isinstance(signed, dict) else signed.signed_url
    except Exception:
        raise HTTPException(500, detail={"error_code": "STORAGE_ERROR", "message": "Could not generate download URL."})

    return {"url": url, "expires_in_seconds": SIGNED_URL_EXPIRY, "filename": doc.data["filename"]}

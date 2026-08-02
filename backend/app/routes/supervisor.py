"""Supervisor workspace: assignment-scoped students, review queues, exports and profile."""
import csv, io, uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.db.supabase_client import get_supabase_client
from app.utils.auth_middleware import get_current_user
from app.utils.audit import write_audit_log
from app.services.notification_service import create_notification

router=APIRouter()
SUPERVISOR_PROFILE_COLUMNS="id,email,name,phone,designation,areas_of_interest,department_id,max_students,profile_photo_url,status,approval_status,created_at,updated_at"
ASSIGNED_STUDENT_COLUMNS="id,name,email,phone,student_number,registration_number,profile_photo_url,department_id,program_id,semester_id,session_id,section_id,status,created_at,updated_at"
def supervisor_only(user=Depends(get_current_user)):
    if user["role"]!="supervisor": raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"Supervisor access required."})
    return user
def assigned_project(project_id,user):
    row=get_supabase_client().table("projects").select("id, student_id, supervisor_id").eq("id",project_id).eq("supervisor_id",user["user_id"]).single().execute().data
    if not row: raise HTTPException(404,detail={"error_code":"NOT_FOUND","message":"Assigned project not found."})
    return row

@router.get("/dashboard")
async def dashboard(user=Depends(supervisor_only)):
    client=get_supabase_client(); projects=client.table("projects").select("id, student_id, lifecycle_stage, updated_at").eq("supervisor_id",user["user_id"]).execute().data or []
    ids=[p["id"] for p in projects]; docs=[]
    if ids: docs=client.table("project_documents").select("id, project_id, filename, uploaded_at, status").in_("project_id",ids).order("uploaded_at",desc=True).execute().data or []
    pending=[d for d in docs if (d.get("status") or "pending") in {"pending","under_review"}]
    approved=sum(1 for d in docs if d.get("status")=="approved")
    rejected=sum(1 for d in docs if d.get("status")=="rejected")
    return {"statistics":{"assigned_students":len({p['student_id'] for p in projects}),"active_projects":sum(p["lifecycle_stage"]!="closed" for p in projects),"pending_reviews":len(pending),"approved_submissions":approved,"rejected_submissions":rejected},"recent_documents":docs[:6],"recent_activity":sorted(projects,key=lambda p:p.get("updated_at") or "",reverse=True)[:6]}

@router.get("/students")
async def assigned_students(search:Optional[str]=None,status:Optional[str]=None,sort:str="updated_desc",page:int=1,page_size:int=20,user=Depends(supervisor_only)):
    client=get_supabase_client(); rows=client.table("projects").select("id, title, idea, abstract, category, lifecycle_stage, updated_at, students(id,name,email,phone,student_number,registration_number,profile_photo_url,department_id,program_id,semester_id,session_id,section_id,created_at,updated_at,departments(name),programs(name),semesters(name),sessions(academic_year),sections(name))").eq("supervisor_id",user["user_id"]).execute().data or []
    # Fetch document aggregates once.  The previous implementation issued two
    # queries per project, which made this page progressively slower as a
    # supervisor's assignment count grew.
    project_ids=[row["id"] for row in rows]
    documents=[]
    if project_ids:
        documents=client.table("project_documents").select("id,project_id,status").in_("project_id",project_ids).execute().data or []
    aggregates={project_id:{"document_count":0,"pending_review_count":0} for project_id in project_ids}
    for document in documents:
        aggregate=aggregates.get(document.get("project_id"))
        if aggregate is None: continue
        aggregate["document_count"]+=1
        if (document.get("status") or "pending") in {"pending","under_review"}: aggregate["pending_review_count"]+=1
    for row in rows: row.update(aggregates.get(row["id"],{"document_count":0,"pending_review_count":0}))
    if search: rows=[r for r in rows if search.lower() in " ".join([r.get("title") or "",(r.get("students") or {}).get("name") or "",(r.get("students") or {}).get("student_number") or ""]).lower()]
    if status: rows=[r for r in rows if r.get("lifecycle_stage")==status]
    rows.sort(key=lambda r:r.get("updated_at") or "",reverse=sort!="updated_asc"); total=len(rows); start=(max(page,1)-1)*page_size
    return {"students":rows[start:start+page_size],"total":total,"page":page,"page_size":page_size}

@router.get("/students/{student_id}")
async def student_detail(student_id:str,user=Depends(supervisor_only)):
    client=get_supabase_client(); project=client.table("projects").select(f"id,title,idea,abstract,category,lifecycle_stage,student_id,supervisor_id,created_at,updated_at, students({ASSIGNED_STUDENT_COLUMNS},departments(name),programs(name),semesters(name),sessions(academic_year),sections(name)), supervisors(id,name)").eq("student_id",student_id).eq("supervisor_id",user["user_id"]).single().execute().data
    if not project: raise HTTPException(404,detail={"error_code":"NOT_FOUND","message":"Assigned student not found."})
    docs=client.table("project_documents").select("*, approvals(*,supervisors(name))").eq("project_id",project["id"]).order("uploaded_at",desc=True).execute().data or []
    history=client.table("project_status_history").select("*, supervisors(name)").eq("project_id",project["id"]).order("created_at",desc=True).execute().data or []
    return {"student":project["students"],"project":{k:v for k,v in project.items() if k!="students"},"documents":docs,"status_history":history}

@router.get("/reviews")
async def review_queue(status:str="pending",user=Depends(supervisor_only)):
    client=get_supabase_client(); projects=client.table("projects").select("id,title,student_id,students(name,student_number,programs(name),semesters(name),sections(name))").eq("supervisor_id",user["user_id"]).execute().data or []
    ids=[p["id"] for p in projects]; mapping={p["id"]:p for p in projects}; docs=[]
    if ids: docs=client.table("project_documents").select("*, approvals(*,supervisors(name))").in_("project_id",ids).order("uploaded_at",desc=True).execute().data or []
    result=[]
    for d in docs:
        # project_documents.status is updated atomically with every review and
        # is deterministic; nested approvals have no guaranteed row order.
        decision=d.get("status") or "pending"
        if status=="pending" and decision not in {"pending","under_review"}: continue
        if status!="pending" and decision!=status: continue
        result.append({**d,"project":mapping[d["project_id"]],"review_status":decision})
    return {"reviews":result}

@router.get("/meetings")
async def supervisor_meetings(user=Depends(supervisor_only)):
    """Return all meetings in one assignment-scoped query."""
    rows=get_supabase_client().table("meetings").select(
        "*,projects!inner(id,title,supervisor_id,students(id,name,student_number))"
    ).eq("projects.supervisor_id",user["user_id"]).order("proposed_time",desc=True).execute().data or []
    return {"meetings":rows}

class StatusBody(BaseModel): status:str; note:Optional[str]=None
@router.patch("/projects/{project_id}/status")
async def update_project_status(project_id:str,body:StatusBody,user=Depends(supervisor_only)):
    # This is the canonical project lifecycle enum from the database.
    allowed={"not_started","details_submitted","supervisor_assigned","awaiting_review","revision_requested","approved","closed"}
    if body.status not in allowed: raise HTTPException(422,detail={"error_code":"INVALID_STATUS","message":"Invalid project status."})
    if body.status == "revision_requested" and len((body.note or "").strip())<10: raise HTTPException(422,detail={"error_code":"NOTE_REQUIRED","message":"A clear note of at least 10 characters is required."})
    client=get_supabase_client(); project=assigned_project(project_id,user); old=client.table("projects").select("lifecycle_stage").eq("id",project_id).single().execute().data or {}
    previous=old.get("lifecycle_stage")
    if previous == "closed": raise HTTPException(409,detail={"error_code":"PROJECT_CLOSED","message":"A closed project cannot be changed."})
    client.table("projects").update({"lifecycle_stage":body.status}).eq("id",project_id).execute(); client.table("project_status_history").insert({"project_id":project_id,"previous_status":previous,"new_status":body.status,"note":body.note,"supervisor_id":user["user_id"]}).execute()
    await create_notification(project["student_id"],"student","project_status_changed","Project Status Updated",f"Your project status is now {body.status.replace('_',' ')}.",project_id)
    await write_audit_log("project_updated",user["user_id"],"supervisor","project",project_id,metadata={"previous_lifecycle_stage":previous,"lifecycle_stage":body.status})
    return {"message":"Project status updated."}

def export_rows(rows):
    for r in rows:
        s=r.get("students") or {}; yield [s.get("name"),s.get("student_number"),s.get("registration_number"),s.get("email"),s.get("phone"),(s.get("departments") or {}).get("name"),(s.get("programs") or {}).get("name"),(s.get("semesters") or {}).get("name"),(s.get("sections") or {}).get("name"),(s.get("sessions") or {}).get("academic_year"),r.get("title"),r.get("idea"),r.get("lifecycle_stage"),r.get("document_count"),r.get("pending_review_count"),r.get("updated_at"),s.get("updated_at")]
HEADERS=["Student Name","Roll Number","Registration Number","Email","Phone","Department","Program","Semester","Section","Academic Session","Project Title","Project Idea","Project Status","Document Count","Pending Review Count","Last Activity","Profile Last Updated"]
@router.get("/students-export.csv")
async def export_csv(search:Optional[str]=None,status:Optional[str]=None,user=Depends(supervisor_only)):
    data=await assigned_students(search,status,"updated_desc",1,10000,user); stream=io.StringIO(); w=csv.writer(stream); w.writerow(HEADERS); w.writerows(export_rows(data["students"])); content="\ufeff"+stream.getvalue(); name=f"assigned-students-{datetime.now().date()}.csv"
    return StreamingResponse(iter([content]),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="{name}"'})
@router.get("/students-export.xlsx")
async def export_xlsx(search:Optional[str]=None,status:Optional[str]=None,user=Depends(supervisor_only)):
    from openpyxl import Workbook
    data=await assigned_students(search,status,"updated_desc",1,10000,user); wb=Workbook(); ws=wb.active; ws.title="Assigned Students"; ws.append(HEADERS)
    for row in export_rows(data["students"]): ws.append(row)
    ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
    for col in ws.columns: ws.column_dimensions[col[0].column_letter].width=min(max(len(str(c.value or "")) for c in col)+2,35)
    out=io.BytesIO(); wb.save(out); out.seek(0); name=f"assigned-students-{datetime.now().date()}.xlsx"
    return StreamingResponse(out,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f'attachment; filename="{name}"'})

class ProfileBody(BaseModel): name:Optional[str]=None; phone:Optional[str]=None; designation:Optional[str]=None; areas_of_interest:Optional[str]=None
class ImageBody(BaseModel): filename:str; file_size:int; mime_type:str
class ConfirmImage(BaseModel): storage_path:str
@router.get("/profile")
async def profile(user=Depends(supervisor_only)):
    client=get_supabase_client(); row=client.table("supervisors").select(f"{SUPERVISOR_PROFILE_COLUMNS},departments(name),supervisor_program_eligibility(program_id,programs(name))").eq("id",user["user_id"]).single().execute().data
    if row and row.get("profile_photo_url"):
        signed=client.storage.from_("profile-images").create_signed_url(row["profile_photo_url"],3600); row["profile_photo_preview_url"]=signed.get("signedURL") or signed.get("signedUrl")
    return {"profile":row}
@router.patch("/profile")
async def update_profile(body:ProfileBody,user=Depends(supervisor_only)):
    data={k:v.strip() if isinstance(v,str) else v for k,v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "name" in data and len(data["name"])<2: raise HTTPException(422,detail={"error_code":"INVALID_NAME","message":"Name must contain at least 2 characters."})
    if "phone" in data and (len(data["phone"])<7 or len(data["phone"])>20): raise HTTPException(422,detail={"error_code":"INVALID_PHONE","message":"Enter a valid phone number."})
    if "designation" in data and len(data["designation"])>120: raise HTTPException(422,detail={"error_code":"INVALID_DESIGNATION","message":"Designation is too long."})
    client=get_supabase_client(); client.table("supervisors").update(data).eq("id",user["user_id"]).execute()
    await write_audit_log("account_updated",user["user_id"],"supervisor","supervisor",user["user_id"],metadata={"changed_fields":sorted(data)})
    row=client.table("supervisors").select(SUPERVISOR_PROFILE_COLUMNS).eq("id",user["user_id"]).single().execute().data
    return {"profile":row or {}}
@router.post("/profile-image/upload-url")
async def profile_image_url(body:ImageBody,user=Depends(supervisor_only)):
    if body.mime_type not in {"image/jpeg","image/png","image/webp"} or body.file_size>2*1024*1024: raise HTTPException(422,detail={"error_code":"INVALID_IMAGE","message":"Use JPG, PNG or WebP under 2MB."})
    path=f"supervisors/{user['user_id']}/{uuid.uuid4()}.webp"; signed=get_supabase_client().storage.from_("profile-images").create_signed_upload_url(path); return {"upload_url":signed.get("signed_url") or signed.get("signedUrl") or signed.get("signedURL"),"storage_path":path}
@router.post("/profile-image/confirm")
async def confirm_image(body:ConfirmImage,user=Depends(supervisor_only)):
    if not body.storage_path.startswith(f"supervisors/{user['user_id']}/"): raise HTTPException(403,detail={"error_code":"FORBIDDEN","message":"Invalid image path."})
    client=get_supabase_client(); parent,filename=body.storage_path.rsplit("/",1)
    try: objects=client.storage.from_("profile-images").list(parent,{"search":filename,"limit":10}) or []
    except Exception: raise HTTPException(503,detail={"error_code":"STORAGE_UNAVAILABLE","message":"Could not verify the uploaded image. Please retry."})
    if not any(item.get("name")==filename for item in objects): raise HTTPException(422,detail={"error_code":"UPLOAD_NOT_FOUND","message":"Upload the image before confirming it."})
    old=client.table("supervisors").select("profile_photo_url").eq("id",user["user_id"]).single().execute().data; client.table("supervisors").update({"profile_photo_url":body.storage_path}).eq("id",user["user_id"]).execute()
    if old and old.get("profile_photo_url"):
        try: client.storage.from_("profile-images").remove([old["profile_photo_url"]])
        except Exception: pass
    signed=client.storage.from_("profile-images").create_signed_url(body.storage_path,3600)
    await write_audit_log("account_updated",user["user_id"],"supervisor","supervisor",user["user_id"],metadata={"changed_fields":["profile_photo_url"]})
    return {"profile_photo_url":body.storage_path,"preview_url":signed.get("signedURL") or signed.get("signedUrl")}
@router.delete("/profile-image")
async def remove_image(user=Depends(supervisor_only)):
    client=get_supabase_client(); old=client.table("supervisors").select("profile_photo_url").eq("id",user["user_id"]).single().execute().data
    if old and old.get("profile_photo_url"):
        try: client.storage.from_("profile-images").remove([old["profile_photo_url"]])
        except Exception: pass
    client.table("supervisors").update({"profile_photo_url":None}).eq("id",user["user_id"]).execute(); return {"message":"Profile picture removed."}

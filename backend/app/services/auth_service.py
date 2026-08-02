"""
Authentication Service — OTP generation, verification, session management.
Implements the full flow from PRD Section 22-23 and 28.
"""
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from fastapi import HTTPException

from app.db.supabase_client import get_supabase_client
from app.utils.security import (
    generate_otp, hash_otp, verify_otp, verify_legacy_otp,
    create_session_token, hash_token_for_storage, verify_password,
    hash_password
)
from app.utils.config import settings
from app.utils.audit import write_audit_log
from app.email.smtp_client import send_otp_email

logger = structlog.get_logger(__name__)

_OTP_HASH_VERSION = "otp2"


def _encode_otp_hash(purpose: str, digest: str, salt: str) -> str:
    """Bind purpose without requiring a live database schema migration."""
    return f"{_OTP_HASH_VERSION}${purpose}${digest}:{salt}"


def _decode_otp_hash(value: str) -> tuple[str, str, str]:
    """Return purpose, digest and salt; legacy records are sign-in only."""
    if value.startswith(("otp1$", "otp2$")):
        _, purpose, value = value.split("$", 2)
    else:
        purpose = "sign-in"
    digest, salt = value.split(":", 1)
    return purpose, digest, salt


def _verify_encoded_otp(submitted: str, encoded: str) -> bool:
    _, digest, salt = _decode_otp_hash(encoded)
    if encoded.startswith("otp1$") or not encoded.startswith("otp2$"):
        return verify_legacy_otp(submitted, digest, salt)
    return verify_otp(submitted, digest, salt)


async def _active_otp(client, email: str, purpose: str) -> Optional[dict]:
    records = await asyncio.to_thread(lambda: client.table("otp_records").select("*").eq("email", email) \
        .eq("purpose", purpose).eq("consumed", False).order("created_at", desc=True).limit(1).execute())
    if not records.data:
        # Rolling-upgrade compatibility: old rows may have the database default
        # purpose even though the purpose is bound inside the encoded hash.
        records = await asyncio.to_thread(lambda: client.table("otp_records").select("*").eq("email", email) \
            .eq("consumed", False).order("created_at", desc=True).limit(20).execute())
    for record in records.data or []:
        try:
            stored_purpose, _, _ = _decode_otp_hash(record.get("otp_hash") or "")
        except (AttributeError, ValueError):
            continue
        if stored_purpose == purpose:
            return record
    return None


async def _consume_other_otps(client, email: str, purpose: str, keep_id: Optional[str]) -> None:
    records = await asyncio.to_thread(lambda: client.table("otp_records").select("id, otp_hash").eq("email", email) \
        .eq("purpose", purpose).eq("consumed", False).execute())
    updates = []
    for record in records.data or []:
        if keep_id and str(record.get("id")) == str(keep_id):
            continue
        try:
            stored_purpose, _, _ = _decode_otp_hash(record.get("otp_hash") or "")
        except (AttributeError, ValueError):
            continue
        if stored_purpose == purpose:
            updates.append(asyncio.to_thread(lambda rid=record["id"]: client.table("otp_records").update({"consumed": True}).eq("id", rid).execute()))
    if updates:
        await asyncio.gather(*updates)


def _email_delivery_unavailable() -> HTTPException:
    """Stable client-safe error; never includes provider details or addresses."""
    return HTTPException(
        status_code=503,
        detail={
            "error_code": "EMAIL_DELIVERY_UNAVAILABLE",
            "message": "We could not send the verification email. Please try again shortly.",
        },
    )


# =============================================================================
# Account Lookup — resolve email to user record across all role tables
# =============================================================================

async def find_user_by_email(email: str) -> Optional[dict]:
    """
    Check all three role tables for an active account with this email.
    Returns {"id": ..., "role": ..., "name": ..., "email": ...} or None.
    Does NOT reveal whether email exists (caller should return generic response).
    """
    client = get_supabase_client()
    email_lower = email.lower().strip()

    admin, supervisor, student = await asyncio.gather(
        asyncio.to_thread(lambda: client.table("admins").select("id, name, email, password_hash, must_change_password").eq("email", email_lower).eq("status", "active").execute()),
        asyncio.to_thread(lambda: client.table("supervisors").select("id, name, email, profile_photo_url, password_hash, must_change_password").eq("email", email_lower).eq("status", "active").eq("approval_status", "approved").execute()),
        asyncio.to_thread(lambda: client.table("students").select("id, name, email, profile_photo_url, password_hash, must_change_password").eq("email", email_lower).eq("status", "active").execute()),
    )
    for result, role in ((admin,"admin"),(supervisor,"supervisor"),(student,"student")):
        if result.data: return {**result.data[0], "role": role}

    return None


async def find_user_by_identifier(identifier: str) -> Optional[dict]:
    """Resolve credential email for staff/admins or roll number for students."""
    normalized = identifier.strip()
    if "@" in normalized:
        return await find_user_by_email(normalized)
    client = get_supabase_client()
    result = await asyncio.to_thread(lambda: client.table("students").select(
        "id, name, email, student_number, password_hash, must_change_password"
    ).ilike("student_number", normalized).eq("status", "active").execute())
    return {**result.data[0], "role": "student"} if result.data else None


async def find_user_by_role(identifier: str, role: Optional[str]) -> Optional[dict]:
    """Smart lookup: if it's an email, find across all tables so admins can login from any tab."""
    normalized = identifier.strip().lower()
    if "@" in normalized:
        return await find_user_by_email(normalized)
    
    if role not in {"student", "supervisor", "admin"}:
        return await find_user_by_identifier(identifier)
        
    if role == "student":
        return await find_user_by_identifier(identifier)
        
    client = get_supabase_client()
    table = {"student": "students", "supervisor": "supervisors", "admin": "admins"}[role]
    columns = "id,name,email,password_hash,must_change_password" + (",profile_photo_url" if role in {"student", "supervisor"} else "")
    query = client.table(table).select(columns).eq("email", normalized).eq("status", "active")
    if role == "supervisor":
        query = query.eq("approval_status", "approved")
    result = await asyncio.to_thread(lambda: query.limit(1).execute())
    return {**result.data[0], "role": role} if result.data else None


# =============================================================================
# OTP Request — Step 2-4 of PRD Section 23
# =============================================================================

async def request_otp(email: str, ip_address: Optional[str] = None, purpose: str = "sign-in") -> dict:
    """
    Handle OTP request. Always returns a generic success response to prevent
    email enumeration (PRD Section 26). OTP email is only sent if account exists.
    """
    client = get_supabase_client()
    email_lower = email.lower().strip()

    # Check rate limit: max OTP requests per hour per email (PRD Section 26)
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rate_check = client.table("otp_records").select("id") \
        .eq("email", email_lower) \
        .gte("created_at", one_hour_ago) \
        .execute()

    if len(rate_check.data or []) >= settings.OTP_REQUESTS_PER_HOUR:
        logger.warning("otp_rate_limit_exceeded")
        # Still return generic response — don't reveal rate limiting state per enumeration rules
        return {"message": "If this email is registered, a verification code has been sent."}

    # Look up user
    user = await find_user_by_email(email_lower)

    if user:
        # Generate and hash OTP
        otp_plaintext = generate_otp()
        otp_hash, otp_salt = hash_otp(otp_plaintext)

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)).isoformat()

        # Store hashed OTP (NEVER plaintext)
        inserted = client.table("otp_records").insert({
            "email": email_lower,
            "otp_hash": _encode_otp_hash(purpose, otp_hash, otp_salt),
            "expires_at": expires_at,
            "consumed": False,
            "attempt_count": 0,
            "ip_address": ip_address,
            "purpose": purpose,
        }).execute()
        record_id = (inserted.data or [{}])[0].get("id")

        # Send OTP email in the background to make the API instant
        asyncio.create_task(send_otp_email(
            to_email=email_lower,
            to_name=user["name"],
            otp_code=otp_plaintext,
            expiry_minutes=settings.OTP_EXPIRY_MINUTES,
            purpose=purpose,
        ))

        # Preserve an older usable code until replacement delivery succeeds (handled optimistically here)
        await _consume_other_otps(client, email_lower, purpose, record_id)

        await write_audit_log(
            action_type="otp_requested",
            actor_id=user["id"],
            actor_role=user["role"],
            resource_type="otp",
            ip_address=ip_address,
        )
        logger.info("otp_sent", role=user["role"])
    else:
        logger.info("otp_request_unknown_email")  # Don't log the email for privacy

    return {"message": "If this email is registered, a verification code has been sent."}


async def request_registration_otp(
    email: str, student_number: str, ip_address: Optional[str] = None,
    role: str = "student", name: Optional[str] = None,
) -> dict:
    """Send a registration code to any unused, syntactically valid email."""
    from fastapi import HTTPException
    client = get_supabase_client()
    email_lower = email.lower().strip()
    generic = {"message": "A verification code has been sent to your email."}
    if role == "supervisor":
        if client.table("supervisors").select("id").eq("email", email_lower).execute().data:
            raise HTTPException(409, detail={"error_code":"ACCOUNT_EXISTS", "message":"An account already uses this email. Sign in or reset its password."})
    else:
        existing_email = client.table("students").select("id").eq("email", email_lower).execute()
        existing_roll = client.table("students").select("id").ilike("student_number", student_number.strip()).execute()
        if existing_email.data or existing_roll.data:
            raise HTTPException(409, detail={"error_code":"ACCOUNT_EXISTS", "message":"A Student account already exists for this email or roll number."})
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent = client.table("otp_records").select("id").eq("email", email_lower).gte("created_at", one_hour_ago).execute()
    if len(recent.data or []) >= settings.OTP_REQUESTS_PER_HOUR:
        raise HTTPException(429, detail={"error_code":"OTP_RATE_LIMIT", "message":"Too many verification requests. Try again later."})
    code = generate_otp()
    code_hash, salt = hash_otp(code)
    inserted = client.table("otp_records").insert({
        "email": email_lower, "otp_hash": _encode_otp_hash("registration", code_hash, salt),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)).isoformat(),
        "consumed": False, "attempt_count": 0, "ip_address": ip_address, "purpose": "registration",
    }).execute()
    record_id = (inserted.data or [{}])[0].get("id")
    recipient_name = (name or "").strip() or ("Prospective Supervisor" if role == "supervisor" else "Prospective Student")
    asyncio.create_task(send_otp_email(
        email_lower, recipient_name, code, settings.OTP_EXPIRY_MINUTES,
        purpose="registration",
    ))
    
    await _consume_other_otps(client, email_lower, "registration", record_id)
    return generic


async def complete_student_registration(
    email: str, student_number: str, name: str, code: str,
    registration_number: str, phone: str, department_id: str, program_id: str,
    semester_id: str, session_id: str, section_id: str, supervisor_id: str,
    password: str, profile_photo_url: Optional[str] = None, ip_address: Optional[str] = None,
) -> dict:
    """Verify the emailed code and create a student account."""
    from fastapi import HTTPException
    client = get_supabase_client()
    email_lower = email.lower().strip()
    record = await _active_otp(client, email_lower, "registration")
    if not record:
        raise HTTPException(401, detail={"error_code": "OTP_INVALID", "message": "No active verification code found."})
    expires = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(401, detail={"error_code": "OTP_EXPIRED", "message": "Verification code has expired."})
    try:
        _, stored_hash, salt = _decode_otp_hash(record["otp_hash"])
    except (AttributeError, ValueError):
        client.table("otp_records").update({"consumed": True}).eq("id", record["id"]).execute()
        raise HTTPException(401, detail={"error_code": "OTP_INVALID", "message": "Verification code is invalid. Request a new code."})
    if not _verify_encoded_otp(code, record["otp_hash"]):
        attempts = record["attempt_count"] + 1
        client.table("otp_records").update({"attempt_count": attempts, "consumed": attempts >= settings.OTP_ATTEMPTS_PER_CODE}).eq("id", record["id"]).execute()
        raise HTTPException(401, detail={"error_code": "OTP_WRONG_CODE", "message": "Incorrect verification code."})
    section_check = client.table("sections").select("id").eq("id", section_id).eq("session_id", session_id).eq("status","active").execute()
    semester_check = client.table("semesters").select("id").eq("id",semester_id).eq("status","active").execute()
    program_check = client.table("programs").select("id").eq("id", program_id).eq("department_id", department_id).eq("status", "active").execute()
    supervisor_check = client.table("supervisors").select("id").eq("id", supervisor_id).eq("department_id", department_id).eq("status", "active").execute()
    if not section_check.data or not semester_check.data or not program_check.data or not supervisor_check.data:
        raise HTTPException(422, detail={"error_code": "INVALID_ACADEMIC_SELECTION", "message": "One or more academic selections are invalid or inactive."})
    if client.table("students").select("id").eq("email", email_lower).execute().data or \
       client.table("students").select("id").ilike("student_number", student_number.strip()).execute().data or \
       client.table("students").select("id").ilike("registration_number", registration_number.strip()).execute().data:
        raise HTTPException(409, detail={"error_code": "ACCOUNT_EXISTS", "message": "An account already exists for these credentials."})
    created = client.table("students").insert({
        "email": email_lower, "name": name.strip(), "student_number": student_number.strip(),
        "registration_number": registration_number.strip(), "phone": phone.strip(),
        "department_id": department_id, "program_id": program_id, "semester_id": semester_id,
        "session_id": session_id, "section_id": section_id, "profile_photo_url": profile_photo_url,
        "password_hash": hash_password(password), "must_change_password": False,
    }).execute()
    student_id = created.data[0]["id"]
    project = client.table("projects").insert({
        "student_id": student_id,
        "supervisor_id": None,
        "title": None,
    }).execute().data[0]
    try:
        client.table("project_members").insert({"project_id": project["id"], "student_id": student_id}).execute()
    except Exception:
        # Compatibility until the supplied team migration is applied.
        pass
    try:
        client.rpc("assign_project_supervisor", {
            "p_project_id": project["id"], "p_supervisor_id": supervisor_id,
            "p_changed_by": None, "p_reason": "Initial supervisor selected during registration",
        }).execute()
    except Exception as exc:
        # Compatibility fallback for installations that still have the older
        # RPC whose output column name conflicts with projects.supervisor_id.
        # The database trigger still enforces approval and the 30-student cap.
        if 'column reference "supervisor_id" is ambiguous' in str(exc):
            try:
                client.table("projects").update({"supervisor_id": supervisor_id}).eq("id", project["id"]).execute()
                client.table("student_supervisor_assignment_history").insert({
                    "student_id": student_id, "project_id": project["id"],
                    "previous_supervisor_id": None, "new_supervisor_id": supervisor_id,
                    "changed_by": None, "reason": "Initial supervisor selected during registration",
                }).execute()
            except Exception as fallback_exc:
                client.table("projects").delete().eq("id", project["id"]).execute()
                client.table("students").delete().eq("id", student_id).execute()
                if "CAPACITY_FULL" in str(fallback_exc):
                    raise HTTPException(409, detail={"error_code": "CAPACITY_FULL", "message": "This supervisor is now full (30 / 30). Please select another supervisor."}) from fallback_exc
                raise
        else:
            # Compensate so a failed final-slot race does not leave a half-created
            # account/project behind.
            client.table("projects").delete().eq("id", project["id"]).execute()
            client.table("students").delete().eq("id", student_id).execute()
            if "CAPACITY_FULL" in str(exc):
                raise HTTPException(409, detail={"error_code": "CAPACITY_FULL", "message": "This supervisor is now full (30 / 30). Please select another supervisor."}) from exc
            raise
    client.table("otp_records").update({"consumed": True}).eq("id", record["id"]).execute()
    await write_audit_log("account_created", student_id, "student", "student", student_id, ip_address=ip_address)
    return {"message": "Registration completed. You can now sign in with your roll number."}


async def complete_supervisor_registration(email: str, name: str, phone: str, department_id: str,
    designation: str, password: str, code: str, program_id: Optional[str] = None,
    profile_photo_url: Optional[str] = None, supervisor_id: Optional[str] = None) -> dict:
    """Create and activate an email-verified supervisor account."""
    from fastapi import HTTPException
    client = get_supabase_client(); email_lower = email.lower().strip()
    record = await _active_otp(client, email_lower, "registration")
    if not record: raise HTTPException(401, detail={"error_code":"OTP_INVALID","message":"No active verification code found."})
    expires=datetime.fromisoformat(record["expires_at"].replace("Z","+00:00"))
    if datetime.now(timezone.utc)>expires: raise HTTPException(401, detail={"error_code":"OTP_EXPIRED","message":"Verification code has expired."})
    if record.get("attempt_count", 0) >= settings.OTP_ATTEMPTS_PER_CODE:
        client.table("otp_records").update({"consumed":True}).eq("id",record["id"]).execute()
        raise HTTPException(429, detail={"error_code":"OTP_MAX_ATTEMPTS","message":"Too many attempts. Request a new code."})
    try: _,stored_hash,salt=_decode_otp_hash(record["otp_hash"])
    except (AttributeError, ValueError):
        client.table("otp_records").update({"consumed":True}).eq("id",record["id"]).execute()
        raise HTTPException(401, detail={"error_code":"OTP_INVALID","message":"Verification code is invalid. Request a new code."})
    if not _verify_encoded_otp(code, record["otp_hash"]):
        attempts=record.get("attempt_count",0)+1
        client.table("otp_records").update({"attempt_count":attempts,"consumed":attempts>=settings.OTP_ATTEMPTS_PER_CODE}).eq("id",record["id"]).execute()
        raise HTTPException(401, detail={"error_code":"OTP_WRONG_CODE","message":"Incorrect verification code."})
    if not supervisor_id:
        raise HTTPException(422, detail={"error_code":"SUPERVISOR_SELECTION_REQUIRED","message":"Select your official supervisor record."})
    try:
        claimed = client.rpc("claim_supervisor_account", {
            "p_supervisor_id": supervisor_id, "p_email": email_lower,
            "p_password_hash": hash_password(password), "p_phone": phone.strip(),
            "p_designation": designation.strip(), "p_profile_photo_url": profile_photo_url,
        }).execute()
    except Exception as exc:
        message = str(exc)
        if "SUPERVISOR_RECORD_ALREADY_LINKED" in message:
            raise HTTPException(409, detail={"error_code":"SUPERVISOR_RECORD_ALREADY_LINKED","message":"This supervisor record is already linked to an account."}) from exc
        if "SUPERVISOR_RECORD_UNAVAILABLE" in message:
            raise HTTPException(422, detail={"error_code":"SUPERVISOR_RECORD_UNAVAILABLE","message":"Selected supervisor record is unavailable."}) from exc
        if "SUPERVISOR_EMAIL_ALREADY_USED" in message:
            raise HTTPException(409, detail={"error_code":"SUPERVISOR_EMAIL_ALREADY_USED","message":"This email is already linked to another supervisor record."}) from exc
        raise
    if program_id and claimed.data:
        existing = client.table("supervisor_program_eligibility").select("supervisor_id").eq("supervisor_id", supervisor_id).eq("program_id",program_id).execute()
        if not existing.data: client.table("supervisor_program_eligibility").insert({"supervisor_id":supervisor_id,"program_id":program_id}).execute()
    # Email verification is the activation gate for self-registration.
    client.table("supervisors").update({"approval_status":"approved", "status":"active"}).eq("id", supervisor_id).execute()
    client.table("otp_records").update({"consumed":True}).eq("id",record["id"]).execute()
    return {"message":"Registration completed. You can now sign in."}


# =============================================================================
# OTP Verification — Steps 5-6 of PRD Section 23
# =============================================================================

async def verify_otp_and_create_session(
    email: str,
    code: str,
    purpose: str = "sign-in",
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Verify submitted OTP and issue a session token on success.
    Returns session token + user identity, or raises appropriate error.
    """
    from fastapi import HTTPException
    client = get_supabase_client()
    email_lower = email.lower().strip()

    if purpose not in {"sign-in", "admin-login"}:
        raise HTTPException(status_code=422, detail={
            "error_code": "INVALID_OTP_PURPOSE", "message": "Invalid verification workflow.",
        })

    otp_record = await _active_otp(client, email_lower, purpose)
    if not otp_record:
        await write_audit_log(action_type="otp_failed", resource_type="otp",
                              metadata={"reason": "no_pending_otp"}, ip_address=ip_address)
        raise HTTPException(status_code=401, detail={
            "error_code": "OTP_INVALID",
            "message": "No pending verification code found. Please request a new one.",
        })

    # Check expiry
    expires_at = datetime.fromisoformat(otp_record["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires_at:
        await write_audit_log(action_type="otp_failed", resource_type="otp",
                              metadata={"reason": "expired"}, ip_address=ip_address)
        raise HTTPException(status_code=401, detail={
            "error_code": "OTP_EXPIRED",
            "message": "Your verification code has expired. Please request a new one.",
        })

    # Check attempt count (max 5 per PRD Section 23)
    attempt_count = otp_record["attempt_count"]
    if attempt_count >= settings.OTP_ATTEMPTS_PER_CODE:
        # Invalidate this OTP
        client.table("otp_records").update({"consumed": True}).eq("id", otp_record["id"]).execute()
        raise HTTPException(status_code=429, detail={
            "error_code": "OTP_MAX_ATTEMPTS",
            "message": "Too many failed attempts. Please request a new verification code.",
        })

    # Verify OTP hash (constant-time comparison)
    try:
        _, stored_hash, stored_salt = _decode_otp_hash(otp_record["otp_hash"])
    except (AttributeError, ValueError):
        client.table("otp_records").update({"consumed": True}).eq("id", otp_record["id"]).execute()
        logger.error("invalid_otp_record", record_id=otp_record["id"])
        raise HTTPException(status_code=401, detail={
            "error_code": "OTP_INVALID",
            "message": "The verification code is invalid. Please request a new one.",
        })
    if not _verify_encoded_otp(code, otp_record["otp_hash"]):
        # Increment attempt count
        client.table("otp_records").update({"attempt_count": attempt_count + 1}) \
            .eq("id", otp_record["id"]).execute()
        await write_audit_log(action_type="otp_failed", resource_type="otp",
                              metadata={"reason": "wrong_code", "attempt": attempt_count + 1},
                              ip_address=ip_address)
        remaining = settings.OTP_ATTEMPTS_PER_CODE - attempt_count - 1
        raise HTTPException(status_code=401, detail={
            "error_code": "OTP_WRONG_CODE",
            "message": f"Incorrect verification code. {remaining} attempts remaining.",
        })

    # OTP verified — mark as consumed immediately
    # Consume the one-time code and resolve identity concurrently.
    _, user = await asyncio.gather(
        asyncio.to_thread(lambda: client.table("otp_records").update({"consumed": True}).eq("id", otp_record["id"]).execute()),
        find_user_by_email(email_lower),
    )
    if not user:
        raise HTTPException(status_code=401, detail={
            "error_code": "ACCOUNT_NOT_FOUND",
            "message": "Account not found or deactivated.",
        })

    # Create session record
    session_id = str(uuid.uuid4())
    token = create_session_token(user["id"], user["role"], session_id)
    token_hash = hash_token_for_storage(token)

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=settings.SESSION_INACTIVITY_HOURS)).isoformat()
    session_row = {
        "id": session_id,
        "user_id": user["id"],
        "user_role": user["role"],
        "token_hash": token_hash,
        "expires_at": expires_at,
        "ip_address": ip_address,
        "user_agent": user_agent,
    }
    from app.utils.auth_middleware import cache_new_session
    cache_new_session(token, {"id":session_id,"user_id":user["id"],"user_role":user["role"],
                              "expires_at":expires_at,"last_active":datetime.now(timezone.utc).isoformat()})
    await asyncio.to_thread(lambda: client.table("user_sessions").insert(session_row).execute())

    asyncio.create_task(write_audit_log(
        action_type="otp_verified",
        actor_id=user["id"],
        actor_role=user["role"],
        resource_type="session",
        resource_id=session_id,
        ip_address=ip_address,
    ))
    logger.info("session_created", user_id=user["id"], role=user["role"])

    user_data = {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
    }
    if user.get("profile_photo_url"):
        try:
            signed = client.storage.from_("profile-images").create_signed_url(user["profile_photo_url"], 86400)
            user_data["profile_photo_preview_url"] = signed.get("signed_url") or signed.get("signedUrl") or signed.get("signedURL")
        except:
            pass

    return {
        "token": token,
        "user": user_data,
    }


# =============================================================================
# Password login and recovery
# =============================================================================

async def login_with_password(
    identifier: str,
    password: str,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    role_hint: Optional[str] = None,
) -> dict:
    """Authenticate an active account and issue the same revocable session used by OTP login."""
    from fastapi import HTTPException
    client = get_supabase_client()
    user = await find_user_by_role(identifier, role_hint)
    is_admin = bool(user and user.get("role") == "admin")
    # The configured hash remains a safe bootstrap fallback for the authorized
    # administrator, while a database hash takes precedence after a password
    # reset/change so the new password actually persists across logins.
    valid_hash = (
        ((user or {}).get("password_hash") or settings.ADMIN_PASSWORD_HASH)
        if is_admin else (user or {}).get("password_hash")
    )
    # An active record in the protected admins table is the authoritative
    # identity. ADMIN_EMAIL is only bootstrap configuration; requiring it to
    # match forever breaks legitimate database-managed password resets and
    # email changes.
    password_ok = bool(user and valid_hash and await asyncio.to_thread(verify_password, password, valid_hash))
    if not password_ok:
        await write_audit_log(action_type="login_failed", resource_type="session",
                              metadata={"reason": "invalid_credentials"}, ip_address=ip_address)
        raise HTTPException(status_code=401, detail={
            "error_code": "INVALID_CREDENTIALS",
            "message": "Invalid email or password.",
        })

    if is_admin:
        await request_otp(user["email"], ip_address, purpose="admin-login")
        await write_audit_log("otp_requested", user["id"], "admin", "admin_login", user["id"], ip_address=ip_address)
        return {"requires_otp": True, "email": user["email"], "role": "admin", "expires_in_seconds": 300}

    session_id = str(uuid.uuid4())
    token = create_session_token(user["id"], user["role"], session_id)
    session_row = {
        "id": session_id,
        "user_id": user["id"],
        "user_role": user["role"],
        "token_hash": hash_token_for_storage(token),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=settings.SESSION_INACTIVITY_HOURS)).isoformat(),
        "ip_address": ip_address,
        "user_agent": user_agent,
    }
    from app.utils.auth_middleware import cache_new_session
    now = datetime.now(timezone.utc).isoformat()
    cache_new_session(token, {"id":session_id,"user_id":user["id"],"user_role":user["role"],
                              "expires_at":(datetime.now(timezone.utc)+timedelta(hours=settings.SESSION_INACTIVITY_HOURS)).isoformat(),
                              "last_active":now})
    await asyncio.to_thread(lambda: client.table("user_sessions").insert(session_row).execute())
    asyncio.create_task(write_audit_log("login_succeeded", user["id"], user["role"], "session", session_id,
                          ip_address=ip_address))
    user_data = {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}
    if user.get("profile_photo_url"):
        try:
            signed = client.storage.from_("profile-images").create_signed_url(user["profile_photo_url"], 86400)
            user_data["profile_photo_preview_url"] = signed.get("signed_url") or signed.get("signedUrl") or signed.get("signedURL")
        except:
            pass

    return {
        "token": token,
        "must_change_password": bool(user.get("must_change_password")),
        "user": user_data,
    }


async def complete_password_recovery(email: str, code: str, new_password: str, ip_address: Optional[str] = None) -> dict:
    """Verify the emailed OTP and let the account owner set a new hashed password."""
    from fastapi import HTTPException
    client = get_supabase_client()
    email_lower = email.lower().strip()
    record = await _active_otp(client, email_lower, "password-reset")
    if not record:
        raise HTTPException(401, detail={"error_code": "OTP_INVALID", "message": "No active verification code found."})
    expires = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(401, detail={"error_code": "OTP_EXPIRED", "message": "Verification code has expired."})
    if record["attempt_count"] >= settings.OTP_ATTEMPTS_PER_CODE:
        raise HTTPException(429, detail={"error_code": "OTP_MAX_ATTEMPTS", "message": "Too many attempts. Request a new code."})
    try:
        _, stored_hash, salt = _decode_otp_hash(record["otp_hash"])
    except (AttributeError, ValueError):
        client.table("otp_records").update({"consumed": True}).eq("id", record["id"]).execute()
        raise HTTPException(401, detail={"error_code": "OTP_INVALID", "message": "Verification code is invalid. Request a new code."})
    if not _verify_encoded_otp(code, record["otp_hash"]):
        client.table("otp_records").update({"attempt_count": record["attempt_count"] + 1}).eq("id", record["id"]).execute()
        raise HTTPException(401, detail={"error_code": "OTP_WRONG_CODE", "message": "Incorrect verification code."})

    user = await find_user_by_email(email_lower)
    if not user:
        raise HTTPException(401, detail={"error_code": "ACCOUNT_NOT_FOUND", "message": "Account is unavailable."})
    table = {"admin": "admins", "supervisor": "supervisors", "student": "students"}[user["role"]]
    client.table(table).update({
        "password_hash": hash_password(new_password),
        "must_change_password": False,
        "password_changed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", user["id"]).execute()
    client.table("otp_records").update({"consumed": True}).eq("id", record["id"]).execute()
    client.table("user_sessions").delete().eq("user_id", user["id"]).execute()
    await write_audit_log("password_reset", user["id"], user["role"], table, user["id"], ip_address=ip_address)
    return {"message": "Password reset successfully. You can now sign in."}


async def change_password(user_id: str, role: str, current_password: str, new_password: str) -> None:
    from fastapi import HTTPException
    client = get_supabase_client()
    table = {"admin": "admins", "supervisor": "supervisors", "student": "students"}[role]
    result = client.table(table).select("password_hash").eq("id", user_id).single().execute()
    if not result.data or not verify_password(current_password, result.data.get("password_hash")):
        raise HTTPException(401, detail={"error_code": "INVALID_PASSWORD", "message": "Current password is incorrect."})
    client.table(table).update({
        "password_hash": hash_password(new_password), "must_change_password": False,
        "password_changed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", user_id).execute()
    await write_audit_log("password_changed", user_id, role, table, user_id)


# =============================================================================
# Logout
# =============================================================================

async def logout_session(token_hash: str, user_id: str, user_role: str, all_devices: bool = False) -> None:
    """Revoke the current session (or all sessions for this user)."""
    client = get_supabase_client()
    if all_devices:
        client.table("user_sessions").delete().eq("user_id", user_id).execute()
    else:
        client.table("user_sessions").delete().eq("token_hash", token_hash).execute()
    from app.utils.auth_middleware import invalidate_session_cache
    invalidate_session_cache(user_id=user_id if all_devices else None,
                             token_hash=None if all_devices else token_hash)

    await write_audit_log(
        action_type="logout",
        actor_id=user_id,
        actor_role=user_role,
        resource_type="session",
        metadata={"all_devices": all_devices},
    )

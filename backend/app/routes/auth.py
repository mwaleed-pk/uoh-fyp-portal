"""
Authentication Routes — OTP request and verification endpoints.
Rate-limited per PRD Section 26.
"""
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from typing import Literal, Optional
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.services.auth_service import (
    request_otp, verify_otp_and_create_session, logout_session,
    login_with_password, complete_password_recovery, change_password,
    request_registration_otp, complete_student_registration, complete_supervisor_registration,
)
from app.utils.auth_middleware import get_current_user
from app.utils.security import hash_token_for_storage

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


class OTPRequestBody(BaseModel):
    email: EmailStr


class OTPVerifyBody(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")
    purpose: Literal["sign-in", "admin-login"] = "sign-in"


class LogoutBody(BaseModel):
    all_devices: bool = False


class LoginBody(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    role: Optional[Literal["student", "supervisor", "admin"]] = None


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)

class ResetPasswordBody(OTPVerifyBody):
    new_password: str
    confirm_password: str


class RegistrationRequestBody(BaseModel):
    email: EmailStr
    student_number: str = Field(default="", max_length=64)
    role: Literal["student", "supervisor"] = "student"
    name: Optional[str] = Field(default=None, min_length=2, max_length=160)


class RegistrationCompleteBody(RegistrationRequestBody):
    role: Literal["student", "supervisor"] = "student"
    name: str = Field(min_length=2, max_length=160)
    code: str = Field(pattern=r"^\d{6}$")
    registration_number: str = Field(default="", max_length=64)
    phone: str = Field(min_length=7, max_length=32)
    department_id: str
    program_id: str = ""
    semester_id: str = ""
    session_id: str = ""
    section_id: str = ""
    supervisor_id: str = ""
    password: str = Field(min_length=10, max_length=128)
    confirm_password: str = Field(min_length=10, max_length=128)
    designation: Optional[str] = None
    profile_photo_url: Optional[str] = None


@router.post("/login")
@limiter.limit("10/minute")
async def login_endpoint(body: LoginBody, request: Request):
    if len(body.password) > 128:
        raise HTTPException(422, detail={"error_code": "VALIDATION_ERROR", "message": "Invalid password."})
    return await login_with_password(
        body.identifier, body.password, role_hint=body.role,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )


@router.post("/register/request-code")
@limiter.limit("5/minute")
async def registration_code_endpoint(body: RegistrationRequestBody, request: Request):
    if body.role == "student" and not body.student_number.strip():
        raise HTTPException(422, detail={"error_code": "VALIDATION_ERROR", "message": "Roll number is required."})
    return await request_registration_otp(body.email, body.student_number, request.client.host if request.client else None, body.role, body.name)


@router.post("/register/complete")
@limiter.limit("5/minute")
async def registration_complete_endpoint(body: RegistrationCompleteBody, request: Request):
    if len(body.name.strip()) < 2:
        raise HTTPException(422, detail={"error_code": "VALIDATION_ERROR", "message": "Full name is required."})
    if not body.code.isdigit() or len(body.code) != 6:
        raise HTTPException(422, detail={"error_code": "VALIDATION_ERROR", "message": "Verification code must be exactly 6 digits."})
    if body.password != body.confirm_password:
        raise HTTPException(422, detail={"error_code": "PASSWORD_MISMATCH", "message": "Passwords do not match."})
    if len(body.password) < 10 or not any(c.isupper() for c in body.password) or not any(c.islower() for c in body.password) or not any(c.isdigit() for c in body.password):
        raise HTTPException(422, detail={"error_code": "WEAK_PASSWORD", "message": "Use at least 10 characters with uppercase, lowercase, and a number."})
    if body.role == "supervisor":
        return await complete_supervisor_registration(body.email, body.name, body.phone, body.department_id, body.designation or "", body.password, body.code, body.program_id or None, body.profile_photo_url, body.supervisor_id)
    return await complete_student_registration(
        body.email, body.student_number, body.name, body.code,
        registration_number=body.registration_number, phone=body.phone,
        department_id=body.department_id, program_id=body.program_id, semester_id=body.semester_id,
        session_id=body.session_id, section_id=body.section_id, supervisor_id=body.supervisor_id,
        password=body.password, profile_photo_url=body.profile_photo_url,
        ip_address=request.client.host if request.client else None,
    )


@router.post("/forgot-password")
@limiter.limit("5/minute")
async def forgot_password_endpoint(body: OTPRequestBody, request: Request):
    return await request_otp(body.email, request.client.host if request.client else None, purpose="password-reset")


@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password_endpoint(body: ResetPasswordBody, request: Request):
    if not body.code.isdigit() or len(body.code) != 6:
        raise HTTPException(422, detail={"error_code": "VALIDATION_ERROR", "message": "Verification code must be exactly 6 digits."})
    if body.new_password != body.confirm_password:
        raise HTTPException(422, detail={"error_code":"PASSWORD_MISMATCH","message":"Passwords do not match."})
    if len(body.new_password) < 10 or not any(c.isupper() for c in body.new_password) or not any(c.islower() for c in body.new_password) or not any(c.isdigit() for c in body.new_password):
        raise HTTPException(422, detail={"error_code":"WEAK_PASSWORD","message":"Use at least 10 characters with uppercase, lowercase, and a number."})
    return await complete_password_recovery(body.email, body.code, body.new_password, request.client.host if request.client else None)


@router.post("/change-password")
async def change_password_endpoint(body: ChangePasswordBody, current_user: dict = Depends(get_current_user)):
    password = body.new_password
    if len(password) < 10 or not any(c.isupper() for c in password) or not any(c.islower() for c in password) or not any(c.isdigit() for c in password):
        raise HTTPException(422, detail={"error_code": "WEAK_PASSWORD", "message": "Use at least 10 characters with uppercase, lowercase, and a number."})
    if body.current_password == password:
        raise HTTPException(422, detail={"error_code": "PASSWORD_REUSED", "message": "New password must be different."})
    await change_password(current_user["user_id"], current_user["role"], body.current_password, password)
    return {"message": "Password changed successfully."}


@router.post("/request-otp")
@limiter.limit("10/minute")
async def request_otp_endpoint(body: OTPRequestBody, request: Request):
    """
    Step 1 of OTP flow: request a verification code.
    Returns a generic message regardless of whether email exists (anti-enumeration).
    Rate limited: 10/minute per IP.
    """
    ip = request.client.host if request.client else None
    result = await request_otp(body.email, ip_address=ip)
    return result


@router.post("/verify-otp")
@limiter.limit("5/minute")
async def verify_otp_endpoint(body: OTPVerifyBody, request: Request):
    """
    Step 2 of OTP flow: submit the 6-digit code.
    Returns a session token and user identity on success.
    Rate limited: 5/minute per IP.
    """
    # Validate code format
    if not body.code.isdigit() or len(body.code) != 6:
        raise HTTPException(status_code=422, detail={
            "error_code": "VALIDATION_ERROR",
            "message": "Verification code must be exactly 6 digits.",
        })

    ip = request.client.host if request.client else None
    ua = request.headers.get("User-Agent")
    result = await verify_otp_and_create_session(
        email=body.email,
        code=body.code,
        purpose=body.purpose,
        ip_address=ip,
        user_agent=ua,
    )
    return result


@router.post("/logout")
async def logout_endpoint(
    body: LogoutBody,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Revoke the current session (or all sessions for this user)."""
    from app.utils.auth_middleware import get_token_from_request
    token = get_token_from_request(request)
    token_hash = hash_token_for_storage(token) if token else ""

    await logout_session(
        token_hash=token_hash,
        user_id=current_user["user_id"],
        user_role=current_user["role"],
        all_devices=body.all_devices,
    )
    return {"message": "Logged out successfully."}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Return current session user identity (used by frontend on page refresh)."""
    from app.db.supabase_client import get_supabase_client
    client = get_supabase_client()

    role = current_user["role"]
    table_map = {"admin": "admins", "supervisor": "supervisors", "student": "students"}
    table = table_map[role]

    columns = "id, name, email, profile_photo_url" if role in {"student", "supervisor"} else "id, name, email"
    result = client.table(table).select(columns) \
        .eq("id", current_user["user_id"]).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail={
            "error_code": "USER_NOT_FOUND",
            "message": "User account not found.",
        })

    user_data = result.data
    if user_data.get("profile_photo_url"):
        try:
            signed = client.storage.from_("profile-images").create_signed_url(user_data["profile_photo_url"], 86400)
            user_data["profile_photo_preview_url"] = signed.get("signed_url") or signed.get("signedUrl") or signed.get("signedURL")
        except:
            pass

    return {
        "user": {**user_data, "role": role},
    }

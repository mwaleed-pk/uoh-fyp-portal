"""
HTML Email Template Rendering and SMTP dispatch.
Uses Jinja2 for templates. Dispatches asynchronously.
OTP code is NEVER logged. Only sent once, in the email body.
"""
import asyncio
import aiosmtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr, formatdate, make_msgid
from html import unescape
import re
from jinja2 import Environment, FileSystemLoader, select_autoescape
import structlog
from pathlib import Path

from app.utils.config import settings

logger = structlog.get_logger(__name__)

# Jinja2 environment for email templates
_template_dir = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_template_dir)),
    autoescape=select_autoescape(["html"]),
)


def _smtp_from_header() -> str:
    """Use the authenticated mailbox unless a matching sender is configured.

    Consumer SMTP providers commonly reject or spam-folder mail that claims an
    unrelated From address. A friendly display name is retained.
    """
    display_name, configured_address = parseaddr(settings.SMTP_FROM_ADDRESS)
    authenticated_address = settings.SMTP_USERNAME.strip()
    if not configured_address or configured_address.lower() != authenticated_address.lower():
        return formataddr((display_name or settings.PORTAL_NAME, authenticated_address))
    return settings.SMTP_FROM_ADDRESS


def _render_template(template_name: str, context: dict) -> str:
    """Render an HTML email template with the given context."""
    template = _jinja_env.get_template(template_name)
    return template.render(**context, university_name=settings.UNIVERSITY_NAME,
                           portal_name=settings.PORTAL_NAME)


def _plain_text_from_html(html_body: str) -> str:
    """Create a conservative text alternative for strict mail clients."""
    text = re.sub(r"<(br|/p|/div|/tr|/h[1-6])\b[^>]*>", "\n", html_body, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


async def _send_email_async(to_email: str, subject: str, html_body: str, retries: int = 3) -> bool:
    """
    Send an HTML email via SMTP with bounded retry on failure.
    Returns True on success, False after all retries exhausted.
    """
    for attempt in range(1, retries + 1):
        try:
            message = EmailMessage()
            message["From"] = _smtp_from_header()
            message["To"] = to_email
            message["Subject"] = subject
            message["Date"] = formatdate(localtime=False)
            message["Message-ID"] = make_msgid(domain=settings.SMTP_USERNAME.split("@")[-1])
            message["Reply-To"] = settings.SMTP_USERNAME
            message["Auto-Submitted"] = "auto-generated"
            message["X-Auto-Response-Suppress"] = "All"
            message.set_content(_plain_text_from_html(html_body))
            message.add_alternative(html_body, subtype="html")

            errors, response = await aiosmtplib.send(
                message,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME,
                password=settings.SMTP_PASSWORD,
                use_tls=False,
                start_tls=settings.SMTP_USE_TLS,
                # Keep an OTP request responsive even when a local network or
                # SMTP provider silently drops outbound connections.
                timeout=12,
            )
            if errors:
                raise RuntimeError("SMTP rejected one or more recipients")
            logger.info("email_accepted_by_smtp", attempt=attempt,
                        response_type=type(response).__name__)
            return True
        except Exception as exc:
            logger.warning("email_send_failed", attempt=attempt, error_type=type(exc).__name__)
            if attempt < retries:
                await asyncio.sleep(attempt)  # bounded backoff: 1s, then 2s

    logger.error("email_send_exhausted")
    return False


async def send_otp_email(
    to_email: str,
    to_name: str,
    otp_code: str,
    expiry_minutes: int,
    purpose: str = "sign-in",
) -> bool:
    """
    Send OTP verification email.
    The OTP code is displayed prominently per PRD Section 25.
    The plaintext code is NEVER logged here.
    """
    safe_name = (to_name or "Portal user").strip()
    purpose_copy = {
        "registration": ("Complete your registration", "complete your account registration"),
        "password-reset": ("Reset your password", "reset your password"),
        "admin-login": ("Approve your administrator sign-in", "complete your administrator sign-in"),
        "sign-in": ("Complete your sign-in", "complete your sign-in"),
    }
    heading, instruction = purpose_copy.get(purpose, purpose_copy["sign-in"])
    html = _render_template("otp_email.html", {
        "name": safe_name,
        "otp_code": otp_code,
        "expiry_minutes": expiry_minutes,
        "heading": heading,
        "instruction": instruction,
    })
    return await _send_email_async(
        to_email=to_email,
        subject=f"{safe_name}, your {settings.PORTAL_NAME} verification code",
        html_body=html,
    )


async def send_temporary_password_email(to_email: str, to_name: str, temporary_password: str) -> None:
    """Send a newly generated temporary password after OTP verification."""
    html = _render_template("temporary_password_email.html", {
        "name": to_name,
        "temporary_password": temporary_password,
        "portal_url": settings.FRONTEND_BASE_URL,
    })
    await _send_email_async(
        to_email=to_email,
        subject=f"Your {settings.PORTAL_NAME} Temporary Password",
        html_body=html,
    )


async def send_welcome_email(to_email: str, to_name: str, role: str) -> None:
    """Send account creation welcome email."""
    html = _render_template("welcome_email.html", {
        "name": to_name,
        "role": role,
        "portal_url": settings.FRONTEND_BASE_URL,
    })
    await _send_email_async(
        to_email=to_email,
        subject=f"Welcome to {settings.PORTAL_NAME}",
        html_body=html,
    )


async def send_supervisor_assignment_email(
    to_email: str, to_name: str, supervisor_name: str, student_name: str, is_supervisor: bool
) -> None:
    """Notify both student and supervisor of assignment."""
    html = _render_template("assignment_email.html", {
        "name": to_name,
        "supervisor_name": supervisor_name,
        "student_name": student_name,
        "is_supervisor": is_supervisor,
        "portal_url": settings.FRONTEND_BASE_URL,
    })
    subject = (
        f"New Student Assignment — {student_name}" if is_supervisor
        else f"Supervisor Assigned: {supervisor_name}"
    )
    await _send_email_async(to_email=to_email, subject=subject, html_body=html)


async def send_review_decision_email(
    to_email: str, to_name: str, decision: str, project_title: str, comment: str = ""
) -> None:
    """Notify student of supervisor's review decision."""
    html = _render_template("review_email.html", {
        "name": to_name,
        "decision": decision,
        "project_title": project_title,
        "comment": comment,
        "portal_url": settings.FRONTEND_BASE_URL,
    })
    subject = f"FYP Review Update: {decision.replace('_', ' ').title()} — {project_title}"
    await _send_email_async(to_email=to_email, subject=subject, html_body=html)


async def send_notification_email(
    to_email: str, to_name: str, event_type: str, title: str, body: str
) -> None:
    """Generic notification email for in-app events."""
    html = _render_template("notification_email.html", {
        "name": to_name,
        "event_type": event_type,
        "notification_title": title,
        "notification_body": body,
        "portal_url": settings.FRONTEND_BASE_URL,
    })
    await _send_email_async(to_email=to_email, subject=f"{settings.PORTAL_NAME}: {title}", html_body=html)

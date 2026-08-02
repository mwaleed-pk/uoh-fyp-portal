from unittest.mock import AsyncMock
from time import perf_counter

import pytest

from app.email import smtp_client
from app.routes.auth import OTPVerifyBody, RegistrationRequestBody
from app.utils.security import (
    generate_temporary_password,
    hash_otp,
    hash_password,
    verify_otp,
    verify_password,
)


@pytest.mark.asyncio
async def test_otp_email_is_name_and_purpose_aware_without_smtp(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(smtp_client, "_send_email_async", send)

    delivered = await smtp_client.send_otp_email(
        "user@example.com", "Ali <Student>", "123456", 10,
        purpose="password-reset",
    )
    assert delivered is True

    message = send.await_args.kwargs
    assert message["to_email"] == "user@example.com"
    assert message["subject"].startswith("Ali <Student>,")
    assert "Hello Ali &lt;Student&gt;" in message["html_body"]
    assert "reset your password" in message["html_body"]
    assert "123456" in message["html_body"]


@pytest.mark.asyncio
async def test_otp_email_propagates_delivery_failure(monkeypatch):
    monkeypatch.setattr(
        smtp_client, "_send_email_async", AsyncMock(return_value=False)
    )
    delivered = await smtp_client.send_otp_email(
        "user@example.com", "Ali", "123456", 5
    )
    assert delivered is False


def test_registration_code_request_accepts_recipient_name():
    body = RegistrationRequestBody(
        email="student@example.com", student_number="FA22-001",
        role="student", name="Ali Khan",
    )
    assert body.name == "Ali Khan"


def test_login_otp_contract_has_explicit_safe_purposes():
    regular = OTPVerifyBody(email="student@example.com", code="123456")
    admin = OTPVerifyBody(
        email="admin@example.com", code="123456", purpose="admin-login"
    )
    assert regular.purpose == "sign-in"
    assert admin.purpose == "admin-login"
    with pytest.raises(Exception):
        OTPVerifyBody(
            email="student@example.com", code="123456", purpose="password-reset"
        )


def test_otp_purpose_is_persisted_and_filtered_in_every_auth_workflow():
    from pathlib import Path
    from app.services.auth_service import _decode_otp_hash, _encode_otp_hash

    stored = _encode_otp_hash("password-reset", "digest", "salt")
    assert _decode_otp_hash(stored) == ("password-reset", "digest", "salt")
    # Pre-deployment records remain valid only for their historical sign-in use.
    assert _decode_otp_hash("digest:salt") == ("sign-in", "digest", "salt")
    assert _decode_otp_hash("otp1$registration$digest:salt") == ("registration", "digest", "salt")

    schema = (Path(__file__).parents[2] / "database" / "init.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS purpose" in schema
    for purpose in ("sign-in", "admin-login", "password-reset", "registration"):
        assert f"'{purpose}'" in schema


def test_smtp_sender_uses_authenticated_mailbox_when_configured_from_differs(monkeypatch):
    monkeypatch.setattr(smtp_client.settings, "SMTP_USERNAME", "sender@example.com")
    monkeypatch.setattr(
        smtp_client.settings, "SMTP_FROM_ADDRESS", "FYP Portal <other@example.org>"
    )
    assert smtp_client._smtp_from_header() == "FYP Portal <sender@example.com>"


@pytest.mark.asyncio
async def test_smtp_message_has_outlook_compatible_headers_and_text_part(monkeypatch):
    captured = {}

    async def accepted(message, **kwargs):
        captured["message"] = message
        return {}, "250 2.0.0 accepted"

    monkeypatch.setattr(smtp_client.aiosmtplib, "send", accepted)
    monkeypatch.setattr(smtp_client.settings, "SMTP_USERNAME", "sender@gmail.com")
    monkeypatch.setattr(smtp_client.settings, "SMTP_FROM_ADDRESS", "FYP Portal <sender@gmail.com>")
    assert await smtp_client._send_email_async(
        "student@uoh.edu.pk", "Verification", "<p>Hello <b>Student</b></p>", retries=1
    ) is True
    message = captured["message"]
    assert message["Date"] and message["Message-ID"]
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["X-Auto-Response-Suppress"] == "All"
    assert message.is_multipart()
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "Hello Student"
    assert message.get_body(preferencelist=("html",)) is not None


def test_passwords_and_otps_are_one_way_and_verifiable():
    # Longer than bcrypt's native 72-byte limit: bcrypt_sha256 must preserve it.
    password = "A-secure-password-123!" * 5
    digest = hash_password(password)
    assert password not in digest
    assert verify_password(password, digest)
    assert not verify_password(password + "x", digest)

    otp_digest, salt = hash_otp("654321")
    assert verify_otp("654321", otp_digest, salt)
    assert not verify_otp("654322", otp_digest, salt)


def test_temporary_password_meets_required_character_classes():
    password = generate_temporary_password()
    assert len(password) >= 12
    assert any(char.isupper() for char in password)
    assert any(char.islower() for char in password)
    assert any(char.isdigit() for char in password)


def test_ten_thousand_otp_hash_verifications_are_cpu_fast():
    digest, salt = hash_otp("654321")
    started = perf_counter()
    assert all(verify_otp("654321", digest, salt) for _ in range(10_000))
    # Leaves substantial CI headroom while guarding against reintroducing a
    # deliberately slow password KDF into the high-concurrency OTP hot path.
    assert perf_counter() - started < 2.0

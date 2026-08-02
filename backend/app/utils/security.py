"""
Security utilities: OTP generation/hashing, JWT session tokens.
OTP codes are NEVER stored in plaintext — only salted hashes.
"""
import secrets
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from jose import jwt, JWTError
from passlib.context import CryptContext
from app.utils.config import settings

logger = structlog.get_logger(__name__)
# bcrypt_sha256 removes bcrypt's 72-byte password truncation while retaining
# verification support for existing bcrypt account hashes.
password_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return a one-way password hash. Plaintext passwords are never stored."""
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time password verification with a safe failure mode."""
    try:
        return password_context.verify(password, password_hash)
    except (TypeError, ValueError):
        return False


def generate_temporary_password(length: int = 14) -> str:
    """Generate a readable password containing every required character class."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%"
    required = [
        secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"),
        secrets.choice("abcdefghijkmnopqrstuvwxyz"),
        secrets.choice("23456789"),
        secrets.choice("!@#$%"),
    ]
    chars = required + [secrets.choice(alphabet) for _ in range(max(length, 12) - len(required))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# =============================================================================
# OTP Generation & Hashing
# =============================================================================

def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit OTP."""
    return str(secrets.randbelow(900000) + 100000)  # Always 6 digits: 100000–999999


def hash_otp(otp: str, salt: Optional[str] = None) -> tuple[str, str]:
    """
    Hash an OTP code with a random salt.
    Returns (hash_hex, salt) tuple.
    The hash is stored; the plaintext OTP is discarded after email dispatch.
    """
    if salt is None:
        salt = secrets.token_hex(16)
    # OTPs are short-lived and attempt-limited. A keyed digest avoids making
    # every verification deliberately CPU-heavy while still preventing an
    # attacker with only a database dump from testing the six-digit space.
    message = f"{salt}:{otp}".encode("utf-8")
    digest = hmac.new(settings.SESSION_SECRET.encode("utf-8"), message, hashlib.sha256)
    return digest.hexdigest(), salt


def verify_otp(submitted: str, stored_hash: str, salt: str) -> bool:
    """
    Verify a submitted OTP against a stored hash using constant-time comparison
    to prevent timing attacks.
    """
    computed_hash, _ = hash_otp(submitted, salt)
    return hmac.compare_digest(computed_hash, stored_hash)


def verify_legacy_otp(submitted: str, stored_hash: str, salt: str) -> bool:
    """Verify otp1 PBKDF2 records during a safe rolling upgrade to otp2."""
    computed = hashlib.pbkdf2_hmac("sha256", submitted.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return hmac.compare_digest(computed.hex(), stored_hash)


# =============================================================================
# JWT Session Tokens
# =============================================================================

ALGORITHM = "HS256"


def create_session_token(user_id: str, user_role: str, session_id: str) -> str:
    """
    Issue a signed JWT session token encoding user identity and role.
    The session_id is included to support per-device logout.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": user_role,
        "sid": session_id,
        "iat": now,
        # The database session enforces the rolling inactivity timeout.  Keep
        # the JWT valid only until the absolute session lifetime so activity
        # can extend a session without ever exceeding SESSION_MAX_DAYS.
        "exp": now + timedelta(days=settings.SESSION_MAX_DAYS),
        "abs_exp": (now + timedelta(days=settings.SESSION_MAX_DAYS)).isoformat(),
    }
    token = jwt.encode(payload, settings.SESSION_SECRET, algorithm=ALGORITHM)
    return token


def decode_session_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT session token.
    Returns the payload dict, or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, settings.SESSION_SECRET, algorithms=[ALGORITHM])
        # Check absolute expiry (in addition to JWT exp)
        abs_exp = datetime.fromisoformat(payload.get("abs_exp", ""))
        if datetime.now(timezone.utc) > abs_exp:
            return None
        return payload
    except (JWTError, TypeError, ValueError):
        logger.debug("jwt_decode_failed")
        return None


def hash_token_for_storage(token: str) -> str:
    """
    Hash a session token for storage in user_sessions table.
    We store only the hash, never the raw token, as an additional security layer.
    """
    return hashlib.sha256(token.encode()).hexdigest()

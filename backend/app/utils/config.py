"""
Application configuration — reads from environment variables.
All secrets and configuration are injected via .env (never hardcoded).
"""
from pydantic import model_validator
from pydantic_settings import BaseSettings
from typing import Optional
from pathlib import Path


class Settings(BaseSettings):
    # Supabase
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # SMTP
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str
    SMTP_PASSWORD: str
    SMTP_FROM_ADDRESS: str
    SMTP_USE_TLS: bool = True

    # Email
    ADMIN_NOTIFICATION_EMAIL: str
    ADMIN_EMAIL: str
    ADMIN_PASSWORD_HASH: str

    # Session
    SESSION_SECRET: str
    OTP_EXPIRY_MINUTES: int = 5
    SESSION_INACTIVITY_HOURS: int = 24
    SESSION_MAX_DAYS: int = 7

    # URLs
    BACKEND_BASE_URL: str = "http://localhost:8000"
    FRONTEND_BASE_URL: str = "http://localhost:5173"

    # Environment
    ENVIRONMENT: str = "development"

    # Rate limiting
    OTP_REQUESTS_PER_HOUR: int = 5
    OTP_ATTEMPTS_PER_CODE: int = 5
    LOGIN_ATTEMPTS_PER_MINUTE: int = 10

    # File upload
    MAX_FILE_SIZE_MB: int = 25
    ALLOWED_MIME_TYPES: str = "application/pdf"

    # University
    UNIVERSITY_NAME: str = "University of Haripur"
    PORTAL_NAME: str = "UOH FYP Portal"
    INSTITUTIONAL_EMAIL_DOMAIN: str = "uoh.edu.pk"

    class Config:
        env_file = Path(__file__).resolve().parents[2] / ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

    @model_validator(mode="after")
    def validate_security_configuration(self):
        """Fail fast when production would start with unsafe auth settings."""
        if self.ENVIRONMENT.lower() == "production":
            if len(self.SESSION_SECRET) < 32:
                raise ValueError("SESSION_SECRET must contain at least 32 characters in production")
            if not self.ADMIN_PASSWORD_HASH.startswith(("$2a$", "$2b$", "$2y$")):
                raise ValueError("ADMIN_PASSWORD_HASH must be a bcrypt hash in production")
            if self.FRONTEND_BASE_URL.startswith("http://"):
                raise ValueError("FRONTEND_BASE_URL must use HTTPS in production")
        return self

    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def allowed_mime_types_list(self) -> list[str]:
        return [m.strip() for m in self.ALLOWED_MIME_TYPES.split(",")]


settings = Settings()

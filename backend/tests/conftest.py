import os
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# The contract suite never connects to Supabase or SMTP.  These values only
# allow application configuration to be imported in a clean CI environment.
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("SMTP_USERNAME", "test@example.com")
os.environ.setdefault("SMTP_PASSWORD", "test-password")
os.environ.setdefault("SMTP_FROM_ADDRESS", "test@example.com")
os.environ.setdefault("ADMIN_NOTIFICATION_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "$2b$12$abcdefghijklmnopqrstuuuuuuuuuuuuuuuuuuuuuuuuuuuuu")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-long-enough-for-tests")
os.environ.setdefault("ENVIRONMENT", "development")


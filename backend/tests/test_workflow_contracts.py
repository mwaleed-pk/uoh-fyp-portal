"""Offline cross-role API and persistence contract checks.

These tests intentionally do not contact Supabase, storage, or SMTP.  They
guard the routes and database objects required by the main FYP workflows and
verify that protected endpoints reject anonymous callers before business logic.
"""
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from app.routes.auth import RegistrationCompleteBody, RegistrationRequestBody
from app.routes.meetings import MeetingRespondBody, OutcomeBody, add_outcome, respond_to_meeting
from app.routes.projects import ProjectUpdateBody, update_project
from app.utils.auth_middleware import (
    require_admin,
    require_student,
    require_supervisor,
    require_supervisor_or_admin,
)


client = TestClient(app)


def test_registration_contract_supports_only_public_roles():
    student = RegistrationRequestBody(
        email="student@uoh.edu.pk", student_number="FA22-BCS-001", role="student"
    )
    supervisor = RegistrationRequestBody(
        email="teacher@uoh.edu.pk", role="supervisor"
    )
    assert student.role == "student"
    assert supervisor.role == "supervisor"
    with pytest.raises(Exception):
        RegistrationRequestBody(email="admin@uoh.edu.pk", role="admin")


def test_registration_complete_requires_identity_and_academic_fields():
    schema = RegistrationCompleteBody.model_json_schema()
    required = set(schema["required"])
    assert {"email", "name", "code", "phone", "department_id", "password", "confirm_password"} <= required


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guard", "allowed"),
    [
        (require_admin, {"admin"}),
        (require_student, {"student"}),
        (require_supervisor, {"supervisor"}),
        (require_supervisor_or_admin, {"supervisor", "admin"}),
    ],
)
async def test_role_guards_enforce_role_matrix(guard, allowed):
    for role in {"student", "supervisor", "admin"}:
        identity = {"user_id": f"{role}-id", "role": role}
        if role in allowed:
            assert await guard(identity) == identity
        else:
            with pytest.raises(HTTPException) as error:
                await guard(identity)
            assert error.value.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/auth/me"),
        ("post", "/api/auth/logout"),
        ("get", "/api/projects/"),
        ("get", "/api/documents/project/project-id"),
        ("get", "/api/reviews/comments?project_id=project-id"),
        ("get", "/api/meetings/project/project-id"),
        ("get", "/api/notifications/"),
        ("get", "/api/supervisor/dashboard"),
        ("get", "/api/admin/dashboard"),
    ],
)
def test_workflow_endpoints_are_protected(method, path):
    kwargs = {"json": {"all_devices": False}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHENTICATED"


def test_required_cross_role_routes_are_registered():
    paths = app.openapi()["paths"]
    required = {
        "/api/auth/login",
        "/api/auth/register/request-code",
        "/api/auth/register/complete",
        "/api/auth/me",
        "/api/auth/logout",
        "/api/projects/{project_id}/assign-supervisor",
        "/api/documents/upload-url",
        "/api/documents/confirm-upload",
        "/api/reviews/decisions",
        "/api/meetings/",
        "/api/meetings/{meeting_id}",
        "/api/notifications/",
        "/api/admin/students/{student_id}/assignment",
    }
    assert required <= set(paths)


def test_database_contains_workflow_persistence_objects():
    sql = (Path(__file__).resolve().parents[2] / "database" / "init.sql").read_text(encoding="utf-8")
    required_tables = {
        "admins", "supervisors", "students", "otp_records", "user_sessions",
        "projects", "project_documents", "approvals", "comments", "meetings",
        "notifications", "student_supervisor_assignment_history", "audit_logs",
    }
    for table in required_tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql


def test_sensitive_tables_are_backend_only_under_rls():
    sql = (Path(__file__).resolve().parents[2] / "database" / "init.sql").read_text(encoding="utf-8")
    # Session/OTP tables deliberately have RLS enabled with no client policy at
    # all; the remaining workflow tables use an explicit always-false policy.
    for table in ("project_documents", "meetings", "notifications"):
        assert f'CREATE POLICY "{table}_backend_only" ON {table} FOR ALL USING (FALSE) WITH CHECK (FALSE)' in sql
    for table in ("user_sessions", "otp_records"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY" not in "\n".join(
            line for line in sql.splitlines() if f" ON {table}" in line
        )


class _Result:
    def __init__(self, data):
        self.data = data


class _MeetingQuery:
    def __init__(self, data):
        self.data = data

    def select(self, *_args): return self
    def eq(self, *_args): return self
    def single(self): return self
    def execute(self): return _Result(self.data)


class _MeetingClient:
    def __init__(self, data): self.data = data
    def table(self, _name): return _MeetingQuery(self.data)


@pytest.mark.asyncio
async def test_meeting_outcome_rejects_non_member(monkeypatch):
    import app.routes.meetings as routes
    monkeypatch.setattr(routes, "get_supabase_client", lambda: _MeetingClient({
        "id": "meeting-1", "status": "accepted", "proposed_time": "2020-01-01T10:00:00Z",
        "projects": {"student_id": "student-1", "supervisor_id": "supervisor-1"},
    }))
    with pytest.raises(HTTPException) as error:
        await add_outcome("meeting-1", OutcomeBody(outcome_notes="Discussed progress"),
                          {"user_id": "student-2", "role": "student"})
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_meeting_reschedule_rejects_past_time(monkeypatch):
    import app.routes.meetings as routes
    monkeypatch.setattr(routes, "get_supabase_client", lambda: _MeetingClient({
        "id": "meeting-1", "projects": {"student_id": "student-1", "supervisor_id": "supervisor-1"},
    }))
    with pytest.raises(HTTPException) as error:
        await respond_to_meeting("meeting-1", MeetingRespondBody(status="rescheduled", new_time="2020-01-01T10:00:00Z"),
                                 {"user_id": "student-1", "role": "student"})
    assert error.value.status_code == 422
    assert error.value.detail["error_code"] == "PAST_TIME"


@pytest.mark.asyncio
async def test_supervisor_cannot_edit_project(monkeypatch):
    import app.routes.projects as routes
    monkeypatch.setattr(routes, "get_supabase_client", lambda: object())
    async def project_for_user(*_args):
        return {"id": "project-1", "student_id": "student-1", "supervisor_id": "supervisor-1", "lifecycle_stage": "idea"}
    monkeypatch.setattr(routes, "get_project_for_user", project_for_user)
    with pytest.raises(HTTPException) as error:
        await update_project("project-1", ProjectUpdateBody(title="Changed"),
                             {"user_id": "supervisor-1", "role": "supervisor"})
    assert error.value.status_code == 403


def test_notification_mutations_are_scoped_by_role():
    source = (Path(__file__).resolve().parents[1] / "app" / "routes" / "notifications.py").read_text(encoding="utf-8")
    assert source.count('.eq("user_role", current_user["role"])') == 3

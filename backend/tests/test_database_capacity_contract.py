"""Static contracts for the manually-applied Supabase migration.

These tests intentionally do not connect to or mutate the live Supabase project.
"""

from pathlib import Path


SQL = (Path(__file__).parents[2] / "database" / "init.sql").read_text(encoding="utf-8")


def test_supervisor_capacity_is_fixed_at_thirty():
    assert "max_students        INTEGER NOT NULL DEFAULT 30" in SQL
    assert "CHECK (max_students = 30)" in SQL
    assert "('max_students_per_supervisor', '30'" in SQL
    assert "WHERE projects.supervisor_id = p_supervisor_id" in SQL
    assert "AND lifecycle_stage <> 'closed'" in SQL
    assert "MESSAGE = 'CAPACITY_FULL'" in SQL


def test_assignment_rpc_is_atomic_and_preserves_history():
    body = SQL.split("CREATE OR REPLACE FUNCTION assign_project_supervisor(", 1)[1]
    body = body.split("CREATE OR REPLACE FUNCTION enforce_supervisor_capacity()", 1)[0]
    assert "FROM projects WHERE id = p_project_id FOR UPDATE" in body
    assert "FROM supervisors" in body and "FOR UPDATE" in body
    assert "UPDATE projects SET supervisor_id" in body
    assert "INSERT INTO student_supervisor_assignment_history" in body
    assert "SUPERVISOR_UNAVAILABLE" in body
    assert "WHERE projects.supervisor_id = p_supervisor_id" in body


def test_direct_assignment_writes_have_capacity_backstop():
    assert "CREATE OR REPLACE FUNCTION enforce_supervisor_capacity()" in SQL
    assert "CREATE TRIGGER trg_projects_supervisor_capacity" in SQL
    assert "BEFORE INSERT OR UPDATE OF supervisor_id, lifecycle_stage ON projects" in SQL


def test_supervisor_claim_accepts_verified_email_but_is_single_use_and_service_role_only():
    body = SQL.split("CREATE OR REPLACE FUNCTION claim_supervisor_account(", 1)[1]
    body = body.split("CREATE OR REPLACE FUNCTION assign_project_supervisor(", 1)[0]
    assert "WHERE id = p_supervisor_id" in body and "FOR UPDATE" in body
    assert "SUPERVISOR_IDENTITY_MISMATCH" not in body
    assert "SUPERVISOR_INSTITUTIONAL_EMAIL_REQUIRED" not in body
    assert "SUPERVISOR_RECORD_ALREADY_LINKED" in body
    assert "account_linked_at = NOW()" in body
    assert "REVOKE ALL ON FUNCTION claim_supervisor_account" in SQL
    assert "GRANT EXECUTE ON FUNCTION claim_supervisor_account" in SQL


def test_verified_supervisor_email_is_replaced_and_activated():
    body = SQL.split("CREATE OR REPLACE FUNCTION claim_supervisor_account(", 1)[1]
    body = body.split("CREATE OR REPLACE FUNCTION assign_project_supervisor(", 1)[0]
    assert "@supervisor.local" in body
    assert "SUPERVISOR_EMAIL_ALREADY_USED" in body
    assert "SET email = LOWER(BTRIM(p_email))" in body
    assert "approval_status = 'approved'" in body


def test_project_team_join_is_atomic_and_one_project_per_student():
    assert "CREATE TABLE IF NOT EXISTS project_members" in SQL
    assert "student_id UUID NOT NULL UNIQUE" in SQL
    body = SQL.split("CREATE OR REPLACE FUNCTION join_project_team(", 1)[1]
    assert "FOR UPDATE" in body
    assert "STUDENT_PROJECT_ALREADY_STARTED" in body
    assert "INVITE_CODE_INVALID" in body
    assert "GRANT EXECUTE ON FUNCTION join_project_team" in SQL


def test_active_otp_lookup_has_partial_composite_index():
    assert "idx_otp_active_lookup" in SQL
    assert "LOWER(email), purpose, created_at DESC" in SQL
    assert "WHERE consumed = FALSE" in SQL

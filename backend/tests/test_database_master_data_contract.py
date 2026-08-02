from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "database" / "init.sql").read_text(encoding="utf-8")
PREFLIGHT = (ROOT / "database" / "preflight.sql").read_text(encoding="utf-8")


def test_master_data_normalized_uniqueness_and_scoped_relationships():
    for index in (
        "uq_departments_name_normalized",
        "uq_programs_department_name_normalized",
        "uq_semesters_name_normalized",
        "uq_sessions_program_semester_year_normalized",
        "uq_sections_session_name_normalized",
    ):
        assert f"CREATE UNIQUE INDEX IF NOT EXISTS {index}" in SQL
    assert "ON programs (department_id, LOWER(BTRIM(name)))" in SQL
    assert "ON sections (session_id, LOWER(BTRIM(name)))" in SQL


def test_student_taxonomy_is_enforced_by_database():
    assert "CREATE OR REPLACE FUNCTION validate_student_taxonomy()" in SQL
    assert "se.program_id = p.id" in SQL
    assert "sm.id = se.semester_id" in SQL
    assert "sc.session_id = se.id" in SQL
    assert "MESSAGE = 'INVALID_STUDENT_TAXONOMY'" in SQL
    assert "BEFORE INSERT OR UPDATE OF department_id, program_id, session_id, semester_id, section_id" in SQL


def test_preflight_is_read_only_and_covers_migration_risks():
    upper = PREFLIGHT.upper()
    for mutation in ("INSERT INTO", "UPDATE ", "DELETE FROM", "ALTER TABLE", "CREATE ", "DROP "):
        assert mutation not in upper
    for issue in (
        "duplicate_department_name",
        "duplicate_program_name_in_department",
        "duplicate_semester_name",
        "duplicate_session",
        "duplicate_section_in_session",
        "duplicate_supervisor_email",
        "invalid_student_taxonomy",
    ):
        assert issue in PREFLIGHT
    assert "information_schema.columns" in PREFLIGHT
    assert "pg_policies" in PREFLIGHT
    assert "claim_supervisor_account" in PREFLIGHT

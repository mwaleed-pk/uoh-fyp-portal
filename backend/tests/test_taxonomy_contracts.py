import pytest
import inspect
from pydantic import ValidationError

from app.routes.taxonomy import (
    DepartmentBody,
    ProgramBody,
    SemesterBody,
    SessionBody,
    SectionBody,
    router,
    list_sessions,
    list_sections,
)


def test_taxonomy_labels_are_trimmed_and_codes_are_available_for_canonicalization():
    department = DepartmentBody(name="  Computer   Science  ", code=" cs ")
    assert department.name == "Computer Science"
    assert department.code == "cs"


@pytest.mark.parametrize(
    "body",
    [
        lambda: DepartmentBody(name=" ", code="CS"),
        lambda: ProgramBody(department_id="d", name="BSCS", code=" "),
        lambda: SemesterBody(name="\t"),
        lambda: SessionBody(program_id="p", semester_id="s", academic_year=" "),
        lambda: SectionBody(session_id="s", name=" "),
    ],
)
def test_taxonomy_rejects_blank_required_labels(body):
    with pytest.raises(ValidationError):
        body()


def test_taxonomy_rejects_unknown_status():
    with pytest.raises(ValidationError):
        SemesterBody(name="Semester 1", status="deleted")


def test_all_master_data_types_have_admin_mutation_routes():
    paths_by_method = {
        (method, route.path)
        for route in router.routes
        for method in (route.methods or set())
    }
    for plural in ("departments", "programs", "semesters", "sessions", "sections"):
        assert ("GET", f"/{plural}") in paths_by_method
        assert ("POST", f"/{plural}") in paths_by_method
        assert any(method == "PUT" and path.startswith(f"/{plural}/") for method, path in paths_by_method)
        assert any(method == "DELETE" and path.startswith(f"/{plural}/") for method, path in paths_by_method)


def test_taxonomy_list_routes_do_not_reference_mutation_body():
    assert "body." not in inspect.getsource(list_sessions)
    assert "body." not in inspect.getsource(list_sections)

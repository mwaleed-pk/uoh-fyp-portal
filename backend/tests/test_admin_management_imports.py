from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from app.routes import admin as routes


class Result:
    def __init__(self, data=None):
        self.data = data or []


class Query:
    def __init__(self, db, table):
        self.db, self.table_name, self.operation, self.payload = db, table, "select", None

    def select(self, *_args, **_kwargs): return self
    def eq(self, *_args, **_kwargs): return self
    def insert(self, payload): self.operation, self.payload = "insert", payload; return self
    def execute(self):
        if self.operation == "insert":
            row = {**self.payload, "id": f"supervisor-{len(self.db.inserted) + 1}"}
            self.db.inserted.append(row); return Result([row])
        if self.table_name == "departments": return Result([{"id": "dep-1", "name": "Computer Science", "code": "CS"}])
        if self.table_name == "supervisors": return Result([{"email": "existing@uoh.edu.pk"}])
        return Result()


class FakeDB:
    def __init__(self): self.inserted = []
    def table(self, name): return Query(self, name)


@pytest.mark.asyncio
async def test_supervisor_csv_import_reports_row_errors_and_saves_valid_rows(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(routes, "get_supabase_client", lambda: db)
    async def no_audit(*_args, **_kwargs): return None
    monkeypatch.setattr(routes, "write_audit_log", no_audit)
    content = (
        "name,email,phone,department,designation,areas_of_interest,status\n"
        "Dr Valid,valid@uoh.edu.pk,0300,CS,Lecturer,AI,active\n"
        "Dr Duplicate,existing@uoh.edu.pk,,CS,Lecturer,Networks,active\n"
        "Dr Bad,bad-email,,Unknown,Lecturer,,active\n"
    ).encode()
    result = await routes.import_supervisors(UploadFile(filename="supervisors.csv", file=BytesIO(content)),
                                               {"user_id": "admin-1"})
    assert result["imported_count"] == 1
    assert result["failed_count"] == 2
    assert db.inserted[0]["email"] == "valid@uoh.edu.pk"
    assert db.inserted[0]["max_students"] == 30
    assert all("password" not in row for row in db.inserted)
    assert result["errors"][0]["row"] == 3


@pytest.mark.asyncio
async def test_supervisor_import_rejects_missing_required_columns():
    upload = UploadFile(filename="bad.csv", file=BytesIO(b"name,email\nDr X,x@uoh.edu.pk\n"))
    with pytest.raises(HTTPException) as exc:
        await routes.import_supervisors(upload, {"user_id": "admin-1"})
    assert exc.value.status_code == 422
    assert "department" in exc.value.detail


@pytest.mark.asyncio
async def test_supervisor_template_has_safe_readable_columns():
    response = await routes.supervisor_import_template("csv", {"user_id": "admin-1"})
    body = response.body.decode("utf-8-sig")
    assert body.startswith("name,email,phone,department,designation,areas_of_interest,status")
    assert "password" not in body.lower()
    assert response.headers["content-disposition"].endswith('supervisor-import-template.csv"')

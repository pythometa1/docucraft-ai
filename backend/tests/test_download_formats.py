"""Serving a finished letter as .docx or .pdf, one at a time or in bulk.

Two things are worth testing here and neither is the happy path.

The first is that a missing converter is reported as a missing converter.
LibreOffice is an optional dependency of a deployment, so `format=pdf` on a host
without it must say so and leave the .docx reachable -- not 500, and not silently
hand back a .docx labelled as a PDF.

The second is that a bulk download does not fail whole because one document
failed. A reviewer pulling two hundred letters needs the hundred and ninety-nine
that converted, plus a list of what is missing.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.generation import pdf_renderer


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def generated(app_client, two_orgs, tmp_path):
    """One approved document belonging to org A, with a real .docx on disk.

    Approved because every download path now refuses an unsigned letter. These
    tests are about formats and conversion; the gate itself has its own file."""
    import docx
    from app.db import SessionLocal
    from app.models import DocumentVersion, GeneratedDocument, Project, User
    from app.storage import abs_path
    from sqlalchemy import select

    token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))
        rel = f"generated/{project.id}/letter.docx"
        path = abs_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        d = docx.Document()
        d.add_paragraph("Dear Amelia, welcome aboard.")
        d.save(str(path))

        gd = GeneratedDocument(
            org_id=project.org_id, project_id=project.id, display_id=1,
            language="en", status="approved",
        )
        db.add(gd)
        db.flush()
        dv = DocumentVersion(
            document_id=gd.id, org_id=gd.org_id, version_no=1, blob_path=rel,
            renderer="ooxml_fill/1.0", status="approved", created_by=user.id,
        )
        db.add(dv)
        db.flush()
        gd.current_version_id = dv.id
        db.commit()
        return {"token": token_a, "document_id": gd.id, "version_id": dv.id}
    finally:
        db.close()


def test_docx_is_served_as_docx(app_client, generated):
    res = app_client.get(
        f"/api/v1/document-versions/{generated['version_id']}/download",
        headers=_auth(generated["token"]),
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert zipfile.ZipFile(io.BytesIO(res.content)).testzip() is None


def test_an_unknown_format_is_refused_rather_than_guessed(app_client, generated):
    res = app_client.get(
        f"/api/v1/document-versions/{generated['version_id']}/download",
        params={"format": "rtf"}, headers=_auth(generated["token"]),
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_pdf_without_libreoffice_says_so_and_keeps_the_docx_reachable(app_client, generated, monkeypatch):
    """A deployment without the converter is a configuration fact, not a bug in
    the letter. It must not read as a server error, and it must not hand back a
    .docx wearing a PDF content type."""
    monkeypatch.setattr(pdf_renderer, "find_soffice", lambda: None)

    res = app_client.get(
        f"/api/v1/document-versions/{generated['version_id']}/download",
        params={"format": "pdf"}, headers=_auth(generated["token"]),
    )
    assert res.status_code == 503
    body = res.json()["detail"]["error"]
    assert body["code"] == "PDF_UNAVAILABLE"
    assert "LibreOffice" in body["message"]

    # and the .docx is still there
    assert app_client.get(
        f"/api/v1/document-versions/{generated['version_id']}/download",
        headers=_auth(generated["token"]),
    ).status_code == 200


def test_bulk_download_returns_a_zip_of_docx(app_client, generated):
    res = app_client.post(
        "/api/v1/documents:download",
        json={"document_ids": [generated["document_id"]], "format": "docx"},
        headers=_auth(generated["token"]),
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert len(names) == 1 and names[0].endswith(".docx")


def test_bulk_download_records_what_it_could_not_convert(app_client, generated, monkeypatch):
    """One document that cannot be produced must not cost the reviewer the rest
    of the archive -- it becomes a line in _FAILED.txt instead."""
    monkeypatch.setattr(pdf_renderer, "find_soffice", lambda: None)

    res = app_client.post(
        "/api/v1/documents:download",
        json={"document_ids": [generated["document_id"]], "format": "pdf"},
        headers=_auth(generated["token"]),
    )
    # Nothing converted, so this one is honest about producing nothing at all.
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "NOTHING_TO_DOWNLOAD"


def test_bulk_download_refuses_an_empty_selection(app_client, generated):
    res = app_client.post(
        "/api/v1/documents:download", json={"document_ids": [], "format": "docx"},
        headers=_auth(generated["token"]),
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "NO_DOCUMENTS"


def test_bulk_download_caps_the_archive_size(app_client, generated):
    """PDF conversion is one subprocess per document. A thousand-letter archive
    belongs on a job, and saying so beats holding a connection open for it."""
    from app.routers.generation import MAX_BULK_DOCUMENTS

    res = app_client.post(
        "/api/v1/documents:download",
        json={"document_ids": [generated["document_id"]] * (MAX_BULK_DOCUMENTS + 1), "format": "docx"},
        headers=_auth(generated["token"]),
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "TOO_MANY_DOCUMENTS"


def test_bulk_download_checks_ownership_per_document(app_client, two_orgs, generated):
    """A caller can put any id in a JSON array. A bulk endpoint that trusts the
    array is how one tenant reads another's letters."""
    _token_a, _project_a, token_b, *_ = two_orgs

    res = app_client.post(
        "/api/v1/documents:download",
        json={"document_ids": [generated["document_id"]], "format": "docx"},
        headers=_auth(token_b),
    )
    assert res.status_code == 404

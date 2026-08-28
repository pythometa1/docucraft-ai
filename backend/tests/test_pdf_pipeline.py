"""A PDF template, from upload to generated document.

`render_overlay` implemented §12's PDF path correctly and had no caller: nothing
uploaded a PDF, nothing persisted a region inventory, nothing generated from
one. A renderer nothing calls is a library, and §12's path was still missing
from the product however good the renderer was.

These tests drive the two ends that were missing -- onboarding derives the
inventory, generation writes into it -- because the property worth pinning is
not "the renderer works" but "a PDF template is a thing this system can
actually use".
"""

import os
import pathlib

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.generation.pdf_fill import build_fills, fill_pdf_template, regions_from_rows
from app.models import Project, TemplateFile, TemplateManifest, TemplateVersion, User
from app.storage import abs_path
from app.templates.ingest import parse_template_version


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def pdf_template_bytes(tmp_path_factory):
    """A one-page PDF carrying a label and a placeholder.

    Built with the overlay suite's own builder rather than a second one: two
    fixtures that drift apart would let this suite pass against a document the
    renderer's own tests never see.
    """
    from tests.test_pdf_overlay import LETTER, _build_pdf

    path = _build_pdf(tmp_path_factory.mktemp("pdf") / "approved.pdf", LETTER)
    return pathlib.Path(path).read_bytes()


@pytest.fixture()
def onboarded_pdf(app_client, two_orgs, pdf_template_bytes):
    """A PDF template uploaded and parsed the way the product does it."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))

        rel = f"templates/{project.id}/offer.pdf"
        os.makedirs(str(abs_path(rel).parent), exist_ok=True)
        abs_path(rel).write_bytes(pdf_template_bytes)

        template_file = TemplateFile(
            org_id=project.org_id, project_id=project.id, name="offer.pdf",
            status="parsing", created_by=user.id,
        )
        db.add(template_file)
        db.flush()
        version = TemplateVersion(
            template_file_id=template_file.id, org_id=project.org_id,
            version_no=1, blob_path=rel, created_by=user.id,
        )
        db.add(version)
        db.flush()

        parse_template_version(db, template_file, version)
        db.commit()
        return {
            "org_id": project.org_id, "project_id": project.id, "user_id": user.id,
            "template_file_id": template_file.id, "version_id": version.id,
            "status": template_file.status, "kind": version.template_kind,
            "regions": list(version.page_regions or []),
            "blob_path": rel,
        }
    finally:
        db.close()


# ------------------------------------------------------------- onboarding

def test_onboarding_a_pdf_derives_the_region_inventory(onboarded_pdf):
    """§12's second step: "stored dynamic regions / anchors / bounding boxes".
    Nothing derived them before, so there was nowhere an overlay was allowed to
    write."""
    assert onboarded_pdf["status"] == "ready", onboarded_pdf
    assert onboarded_pdf["kind"] == "pdf_overlay"
    assert onboarded_pdf["regions"], "a PDF with text must yield at least one region"

    first = onboarded_pdf["regions"][0]
    assert {"page", "x0", "y0", "x1", "y1"} <= set(first)
    assert first["page"] >= 1


def test_a_pdf_is_not_run_through_the_docx_parser(onboarded_pdf):
    """It has no runs, no headings and no MERGEFIELDs. Looking for them and
    finding none used to leave the upload `failed` with a parse error about a
    document structure that was never going to be there."""
    assert onboarded_pdf["kind"] != "heading"
    assert onboarded_pdf["status"] != "failed"


def test_the_inventory_survives_a_round_trip_through_the_database(onboarded_pdf):
    """The coordinates are approved once and read back at every render; a lossy
    round trip would move the text a reviewer signed off on."""
    rebuilt = regions_from_rows(onboarded_pdf["regions"])
    assert len(rebuilt) == len(onboarded_pdf["regions"])
    original = onboarded_pdf["regions"][0]
    assert rebuilt[0].page == original["page"]
    assert rebuilt[0].x0 == pytest.approx(original["x0"])


# --------------------------------------------------------------- generation

def _manifest(field_id="employee_name"):
    return {
        "fields": [{"id": field_id, "type": "text", "required": True, "slots": []}],
        "conditions": [], "blocks": [], "delete_always": [],
    }


def test_a_manifest_generates_a_pdf_through_the_overlay(onboarded_pdf, tmp_path):
    """The end that was missing. The renderer was correct and unreachable."""
    out = tmp_path / "letter.pdf"
    result = fill_pdf_template(
        str(abs_path(onboarded_pdf["blob_path"])), str(out),
        _manifest(), {"employee_name": "Priya Sharma"},
        page_regions=onboarded_pdf["regions"],
    )

    assert out.exists() and out.stat().st_size > 0
    assert result.qa_passed, result.qa_notes


def test_a_field_with_no_approved_region_is_reported_not_dropped(onboarded_pdf, tmp_path):
    """Silently skipping it produces a letter missing a value that QA cannot see
    is missing, because nothing on the page says anything should be there."""
    manifest = _manifest()
    manifest["fields"].append({"id": "unplaceable", "type": "text", "slots": []})
    # More fields than the inventory has regions for.
    manifest["fields"].extend(
        {"id": f"extra_{i}", "type": "text", "slots": []}
        for i in range(len(onboarded_pdf["regions"]) + 2)
    )

    result = fill_pdf_template(
        str(abs_path(onboarded_pdf["blob_path"])), str(tmp_path / "letter.pdf"),
        manifest, {"employee_name": "Priya Sharma"},
        page_regions=onboarded_pdf["regions"],
    )

    assert result.qa_passed is False
    assert any("was not written" in note for note in result.qa_notes), result.qa_notes


def test_a_template_with_no_inventory_refuses_rather_than_guessing(tmp_path, onboarded_pdf):
    """§12's claim is that the renderer writes only into approved regions. With
    no inventory there are none, and inventing coordinates would break exactly
    the property the path exists for."""
    with pytest.raises(ValueError, match="no approved region inventory"):
        fill_pdf_template(
            str(abs_path(onboarded_pdf["blob_path"])), str(tmp_path / "letter.pdf"),
            _manifest(), {"employee_name": "Priya Sharma"}, page_regions=[],
        )


def test_a_missing_required_value_blocks_the_pdf_too(onboarded_pdf, tmp_path):
    """The DOCX path blocks on this; a second renderer that did not would make
    the guarantee depend on which format the customer happened to use."""
    result = fill_pdf_template(
        str(abs_path(onboarded_pdf["blob_path"])), str(tmp_path / "letter.pdf"),
        _manifest(), {},  # the required field has no value
        page_regions=onboarded_pdf["regions"],
    )

    assert result.qa_passed is False
    assert any("BLOCK" in note or "not written" in note for note in result.qa_notes), result.qa_notes


def test_build_fills_pairs_fields_with_regions_in_order(onboarded_pdf):
    fills, unplaced = build_fills(
        _manifest(), {"employee_name": "Priya Sharma"},
        regions_from_rows(onboarded_pdf["regions"]),
    )
    assert [f.object_id for f in fills] == ["employee_name"]
    assert unplaced == ()
    assert fills[0].mask is True, "the placeholder underneath has to be removed, not painted over"

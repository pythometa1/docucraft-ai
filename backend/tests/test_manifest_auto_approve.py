"""A compile approves its own reading -- where doing so walks past nothing.

Nothing generates from an unapproved manifest, and asking someone to press
Approve on a manifest they had just compiled themselves was bookkeeping rather
than a decision. So the compile does it.

The whole content of this file is the "where doing so walks past nothing" half.
`compile-manifest` requires no capability at all and `:approve` requires
APPROVE_MANIFEST, so an unconditional self-approval would hand every mapper an
approval right by way of an upload button -- and a legally-binding template asks
for two people by definition, which one request cannot be.

These call `try_auto_approve` directly rather than through the endpoint, because
the endpoint's other half is a live model call. The decision is the part worth
pinning, and it is all in this function.
"""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    Project, SuggestionLog, TemplateFile, TemplateManifest, TemplateVersion, User,
)
from app.routers.manifests import try_auto_approve


def _ok_manifest():
    """The smallest manifest that passes every rule. Mirrors the one in
    `test_manifest_validation`, deliberately: these two files are about the same
    set of checks reached two different ways."""
    return {
        "fields": [{
            "id": "full_name", "type": "text",
            "slots": [{"paragraph_index": 0, "span_index": 0, "text": "<full_name>", "field_id": "full_name"}],
        }],
        "conditions": [{"id": "c1", "expression": "region == 'EU'", "keeps_blocks": ["b1"]}],
        "blocks": [{"id": "b1", "start_paragraph": 1, "end_paragraph": 2}],
    }


@pytest.fixture()
def compiled(app_client, two_orgs):
    """A factory returning `(db, manifest, template_file, user)` inside a session.

    The caller closes it. Everything here is written straight to the database:
    what is under test is the decision `try_auto_approve` makes about a row, not
    how the row got there.
    """
    _token_a, project_a, *_ = two_orgs
    sessions = []

    def _make(*, role="org_admin", legally_binding=False, warnings=(), version_no=1,
              manifest=None, status="draft"):
        db = SessionLocal()
        sessions.append(db)
        project = db.get(Project, project_a)
        user = db.scalar(select(User).where(User.org_id == project.org_id))
        # Not committed: the role is wanted for this call only, and a role left
        # changed in a session-lived database is a 403 in some other file later.
        user.role_key = role
        template_file = TemplateFile(
            org_id=project.org_id, project_id=project.id, name="auto.docx",
            status="parsed", legally_binding=legally_binding, created_by=user.id)
        db.add(template_file)
        db.flush()
        version = TemplateVersion(
            template_file_id=template_file.id, org_id=project.org_id, version_no=1,
            blob_path=f"templates/{project.id}/auto.docx", created_by=user.id)
        db.add(version)
        db.flush()
        payload = manifest if manifest is not None else _ok_manifest()
        row = TemplateManifest(
            org_id=project.org_id, template_file_id=template_file.id,
            template_version_id=version.id, version_no=version_no, status=status,
            fields=payload["fields"], conditions=payload["conditions"],
            blocks=payload["blocks"], delete_always=[], warnings=list(warnings),
            created_by=user.id)
        db.add(row)
        db.flush()
        return db, row, template_file, user

    yield _make
    for db in sessions:
        db.rollback()
        db.close()


# ------------------------------------------------------------ it lands

def test_a_clean_compile_lands_approved(compiled):
    db, manifest, template_file, user = compiled()
    assert try_auto_approve(db, user, manifest, template_file) is None
    assert manifest.status == "approved"
    assert manifest.approved_by == user.id
    assert manifest.approved_at is not None
    assert [a["user_id"] for a in manifest.approvals] == [user.id]


def test_it_supersedes_whatever_it_replaces(compiled):
    """Exactly one approved manifest per template, or production has a choice to
    make and no way to make it. This matters more now than it did: it used to
    happen once in a template's life, and now it happens on every recompile."""
    db, first, template_file, user = compiled()
    assert try_auto_approve(db, user, first, template_file) is None

    second = TemplateManifest(
        org_id=first.org_id, template_file_id=template_file.id,
        template_version_id=first.template_version_id, version_no=2, status="draft",
        fields=first.fields, conditions=first.conditions, blocks=first.blocks,
        delete_always=[], warnings=[], created_by=user.id)
    db.add(second)
    db.flush()

    assert try_auto_approve(db, user, second, template_file) is None
    assert second.status == "approved"
    assert first.status == "superseded"


# ------------------------------------------------------------ it declines

def test_a_role_that_cannot_approve_gets_its_manifest_anyway(compiled):
    """Not a 403. The compile succeeded and the manifest is the deliverable --
    a mapper's whole job is producing one. It simply is not theirs to sign."""
    db, manifest, template_file, user = compiled(role="mapper")
    reason = try_auto_approve(db, user, manifest, template_file)
    assert reason is not None
    assert "cannot approve" in reason
    assert manifest.status == "draft"


def test_the_compiler_agent_cannot_approve_by_compiling(compiled):
    """The service identity holds COMPILE_MANIFEST and no approval capability at
    all, on purpose. Compiling is the one thing it does, so this is the route by
    which it would have got an approval right."""
    db, manifest, template_file, user = compiled(role="compiler_agent")
    assert try_auto_approve(db, user, manifest, template_file) is not None
    assert manifest.status == "draft"


def test_a_legally_binding_template_is_never_auto_approved(compiled):
    """Four eyes cannot be satisfied by one request. The rule is that two people
    are involved, and self-approval makes the compiler and the approver the same
    person by construction -- so this declines rather than finding a way."""
    db, manifest, template_file, user = compiled(legally_binding=True)
    reason = try_auto_approve(db, user, manifest, template_file)
    assert reason is not None
    assert "two different people" in reason
    assert manifest.status == "draft"


def test_an_unanswered_warning_still_stops_it(compiled):
    """The ordinary case, not an exception: a fresh compile has warnings and no
    dispositions by construction, and answering them is the reviewer's job."""
    db, manifest, template_file, user = compiled(
        warnings=[{"code": "W-UNSLOTTED-FIELD", "paragraph_index": -1, "message": "field has no slot"}])
    reason = try_auto_approve(db, user, manifest, template_file)
    assert reason is not None
    assert "cannot be approved until" in reason
    assert manifest.status == "draft"


def test_an_incoherent_manifest_is_not_approved(compiled):
    broken = _ok_manifest()
    broken["fields"][0].pop("slots")
    db, manifest, template_file, user = compiled(manifest=broken)
    assert try_auto_approve(db, user, manifest, template_file) is not None
    assert manifest.status == "draft"


def test_a_blocked_mapping_stops_it(compiled):
    """§13's block band exists to stop a lock. It has to stop this one too, or
    the automatic path is the way around the band."""
    db, manifest, template_file, user = compiled()
    db.add(SuggestionLog(
        org_id=manifest.org_id, manifest_id=manifest.id, object_id="full_name",
        band="BLOCK", reviewer_decision="pending", score=0.1, method="llm"))
    db.flush()
    reason = try_auto_approve(db, user, manifest, template_file)
    assert reason is not None
    assert "human decision" in reason
    assert manifest.status == "draft"


def test_declining_records_no_phantom_approval(compiled):
    """A refusal must not leave the compiler counted as a first approver. On the
    four-eyes path a *person* pressing Approve does record one -- that is real
    work by a real reviewer -- but nobody pressed anything here."""
    db, manifest, template_file, user = compiled(legally_binding=True)
    try_auto_approve(db, user, manifest, template_file)
    assert not (manifest.approvals or [])

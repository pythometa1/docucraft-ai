"""Separation of duties: the roles, and the things nobody does alone.

`User.role_key` existed and was shown on the team screen from the beginning. It
was never consulted for authorization — every authenticated user in an
organisation could compile a template, edit its manifest, approve it and
generate from it. §16 asks for distinct roles, for the approver role not to be
self-assignable, and for four-eyes on any template flagged legally binding.
"""

import pytest
from sqlalchemy import select

from app.authz import (
    ALL_CAPABILITIES, APPROVE_MANIFEST, COMPILE_MANIFEST, EDIT_MANIFEST,
    GENERATE_DOCUMENT, ROLE_CAPABILITIES, capabilities_of, check_manifest_approval,
    check_role_assignment, has_capability,
)
from app.db import SessionLocal
from app.models import Project, TemplateFile, TemplateManifest, TemplateVersion, User


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------- the role table

def test_every_role_grants_only_known_capabilities():
    """A typo in the table would silently grant nothing, or worse, read as if it
    granted something."""
    for role, caps in ROLE_CAPABILITIES.items():
        unknown = caps - ALL_CAPABILITIES
        assert not unknown, f"role {role!r} grants unknown capabilities {unknown}"


@pytest.mark.parametrize("role", ["", None, "  ", "not_a_role", "ADMIN", "Approver"])
def test_an_unrecognised_role_gets_nothing(role):
    """Fails closed. A role removed from the table while users still carry it,
    or a typo in a seed script, must not read as unrestricted."""
    assert capabilities_of(role) == frozenset()


def test_the_separations_that_matter_hold():
    """These four are the point of the table; if any of them stops holding, the
    roles are decorative."""
    caps = ROLE_CAPABILITIES
    # A mapper edits but cannot sign off their own work.
    assert EDIT_MANIFEST in caps["mapper"] and APPROVE_MANIFEST not in caps["mapper"]
    # An approver signs off but cannot edit, so what is approved is what was reviewed.
    assert APPROVE_MANIFEST in caps["approver"] and EDIT_MANIFEST not in caps["approver"]
    # A generator runs production but cannot change what production executes.
    assert GENERATE_DOCUMENT in caps["generator"] and EDIT_MANIFEST not in caps["generator"]
    # An auditor reads and changes nothing.
    assert not (caps["auditor"] & {EDIT_MANIFEST, APPROVE_MANIFEST, GENERATE_DOCUMENT})


def test_the_compiler_agent_identity_can_never_approve():
    """P5 as an IAM constraint rather than a coding convention: the agent may
    inspect, propose and test; it may not approve its own proposal."""
    agent = ROLE_CAPABILITIES["compiler_agent"]
    assert COMPILE_MANIFEST in agent and EDIT_MANIFEST in agent
    assert APPROVE_MANIFEST not in agent
    assert GENERATE_DOCUMENT not in agent


# --------------------------------------------------------- self-assignment

def _user(uid="u1", role="org_admin"):
    return User(id=uid, org_id="o1", email=f"{uid}@x.test", full_name="X",
                password_hash="x", role_key=role)


@pytest.mark.parametrize("role", ["approver", "org_admin"])
def test_the_approver_role_cannot_be_self_assigned(role):
    """Self-granting approval turns four eyes into two."""
    actor = _user("u1")
    decision = check_role_assignment(actor=actor, target_user_id="u1", new_role=role)
    assert decision.allowed is False
    assert "yourself" in decision.reason


@pytest.mark.parametrize("role", ["approver", "org_admin"])
def test_granting_the_same_role_to_someone_else_is_fine(role):
    actor = _user("u1")
    assert check_role_assignment(actor=actor, target_user_id="u2", new_role=role).allowed is True


def test_a_harmless_role_can_be_self_assigned():
    actor = _user("u1")
    assert check_role_assignment(actor=actor, target_user_id="u1", new_role="auditor").allowed is True


def test_an_unknown_role_cannot_be_granted_at_all():
    actor = _user("u1")
    assert check_role_assignment(actor=actor, target_user_id="u2", new_role="superuser").allowed is False


# ------------------------------------------------------------------ four eyes

def test_an_ordinary_template_needs_only_the_capability():
    """§16 scopes four-eyes to legally-binding templates. Applying it to
    everything is a rule teams learn to route around."""
    assert check_manifest_approval(
        approver_id="u1", compiled_by_id="u1", legally_binding=False,
    ).allowed is True


def test_a_binding_template_refuses_the_person_who_compiled_it():
    decision = check_manifest_approval(
        approver_id="u1", compiled_by_id="u1", legally_binding=True,
    )
    assert decision.allowed is False
    assert "cannot also approve" in decision.reason


def test_a_binding_template_refuses_a_lone_approver():
    """A second person, not a second click."""
    decision = check_manifest_approval(
        approver_id="u2", compiled_by_id="u1", prior_approver_ids=(), legally_binding=True,
    )
    assert decision.allowed is False
    assert "two different people" in decision.reason


def test_the_same_approver_twice_is_still_one_pair_of_eyes():
    decision = check_manifest_approval(
        approver_id="u2", compiled_by_id="u1", prior_approver_ids=("u2", "u2"), legally_binding=True,
    )
    assert decision.allowed is False


def test_two_distinct_approvers_clear_a_binding_template():
    decision = check_manifest_approval(
        approver_id="u3", compiled_by_id="u1", prior_approver_ids=("u2",), legally_binding=True,
    )
    assert decision.allowed is True


# ------------------------------------------------------------ over the API

@pytest.fixture()
def binding_template(app_client, two_orgs):
    """A legally-binding template with a compiled manifest, plus a second user."""
    _token_a, project_a, *_ = two_orgs
    db = SessionLocal()
    try:
        project = db.get(Project, project_a)
        author = db.scalar(select(User).where(User.org_id == project.org_id))

        template_file = TemplateFile(
            org_id=project.org_id, project_id=project.id, name="offer.docx",
            status="parsed", legally_binding=True, created_by=author.id,
        )
        db.add(template_file)
        db.flush()
        version = TemplateVersion(
            template_file_id=template_file.id, org_id=project.org_id, version_no=1,
            blob_path="x.docx", created_by=author.id,
        )
        db.add(version)
        db.flush()
        manifest = TemplateManifest(
            org_id=project.org_id, template_file_id=template_file.id,
            template_version_id=version.id, version_no=1, status="draft",
            fields=[], conditions=[], blocks=[], delete_always=[], created_by=author.id,
        )
        db.add(manifest)
        db.commit()
        return manifest.id
    finally:
        db.close()


def test_approving_your_own_binding_manifest_is_refused_over_the_api(app_client, two_orgs, binding_template):
    token_a, *_ = two_orgs
    res = app_client.post(f"/api/v1/template-manifests/{binding_template}:approve", headers=_auth(token_a))

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "FOUR_EYES_REQUIRED"

    db = SessionLocal()
    try:
        manifest = db.get(TemplateManifest, binding_template)
        assert manifest.status == "draft", "a refused approval must not lock the manifest"
        # The attempt is still on record: the reviewer did real work and the
        # second approver needs to see that it happened.
        assert [a["user_id"] for a in manifest.approvals] != []
    finally:
        db.close()


def test_a_role_without_the_capability_is_refused(app_client, two_orgs, binding_template):
    """403, not 404. The caller is in the right organisation and the resource is
    genuinely theirs to know about — they hold the wrong role, and saying so is
    what lets them ask the right person."""
    token_a, *_ = two_orgs
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "user-a@tenant.test"))
        original = user.role_key
        user.role_key = "mapper"  # may edit a manifest, may not approve one
        db.commit()
    finally:
        db.close()

    try:
        res = app_client.post(f"/api/v1/template-manifests/{binding_template}:approve", headers=_auth(token_a))
        assert res.status_code == 403, res.text
        body = res.json()["detail"]["error"]
        assert body["code"] == "CAPABILITY_REQUIRED"
        assert body["details"]["required_capability"] == APPROVE_MANIFEST
    finally:
        db = SessionLocal()
        try:
            db.scalar(select(User).where(User.email == "user-a@tenant.test")).role_key = original
            db.commit()
        finally:
            db.close()


def test_has_capability_reads_the_users_own_role():
    assert has_capability(_user(role="approver"), APPROVE_MANIFEST) is True
    assert has_capability(_user(role="mapper"), APPROVE_MANIFEST) is False

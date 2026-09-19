"""Guards against the failure mode that makes this application untestable: a
code path that produces a plausible result without doing the work.

Each test here corresponds to something that was actually removed. They are
worth keeping because every one of these bypasses was originally added for a
good reason (run offline, don't crash, always return something), and each would
be re-added by the same reasoning.
"""

import pytest
from sqlalchemy import select


# ------------------------------------------------------------------ no seeding
def test_startup_seeds_nothing(app_client):
    """The app used to invent an org, ten colleagues, thirteen projects and a
    generated offer letter on every boot. A dashboard full of rows nobody
    created cannot tell you whether the application works."""
    import app.main as main

    assert not hasattr(main, "seed_demo_data")
    assert not any("seed" in getattr(h, "__name__", "") for h in app_client.app.router.on_startup)

    with pytest.raises(ImportError):
        import app.seed  # noqa: F401


def test_no_demo_credentials_survive_anywhere():
    """The frontend signed itself in with these; the backend created the account
    they unlocked. Either half alone is enough to make auth decorative."""
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        path
        for path in backend.rglob("*.py")
        if "demo1234" in path.read_text()
    ]
    assert offenders == []


# ------------------------------------------------------------------- bootstrap
def test_bootstrap_creates_a_real_administrator():
    from app.bootstrap import bootstrap
    from app.db import SessionLocal
    from app.models import LookupValue, Organization, User
    from app.security import verify_password

    result = bootstrap(
        org_name="Bootstrap Test Org", email="Admin@Example.COM",
        full_name="Real Person", password="a-genuinely-long-password",
    )

    db = SessionLocal()
    try:
        user = db.get(User, result["user_id"])
        assert user.email == "admin@example.com"  # normalised
        assert verify_password("a-genuinely-long-password", user.password_hash)
        assert user.role_key == "org_admin" and user.status == "active"

        org = db.get(Organization, result["org_id"])
        lookups = db.scalars(select(LookupValue).where(LookupValue.org_id == org.id)).all()
        assert {lv.kind for lv in lookups} == {"function", "region", "language", "document_type"}
    finally:
        db.close()


def test_bootstrap_refuses_to_touch_an_existing_install():
    """Re-running setup must never silently reset a password."""
    from app.bootstrap import bootstrap

    with pytest.raises(ValueError, match="already exists"):
        bootstrap(org_name="Bootstrap Test Org", email="other@example.com",
                  full_name="Someone Else", password="a-genuinely-long-password")

    with pytest.raises(ValueError, match="already exists"):
        bootstrap(org_name="A Different Org", email="admin@example.com",
                  full_name="Someone Else", password="a-genuinely-long-password")


def test_a_colleague_can_be_added_to_an_organisation_that_exists():
    """The separation-of-duties rules make a one-person organisation unable to
    finish its own work, and until this there was no way to create the second
    person: `bootstrap` refuses an existing org and the Team screen is read-only.
    """
    from app.bootstrap import add_user
    from app.db import SessionLocal
    from app.models import User
    from app.security import verify_password

    result = add_user(
        org_name="Bootstrap Test Org", email="Reviewer@Example.COM",
        full_name="Second Person", password="another-long-password",
        role_key="approver", job_title="Head of Reward",
    )

    db = SessionLocal()
    try:
        user = db.get(User, result["user_id"])
        assert user.email == "reviewer@example.com"
        assert verify_password("another-long-password", user.password_hash)
        assert user.role_key == "approver" and user.status == "active"
        assert user.job_title == "Head of Reward"
        # Same org as the administrator, or the two cannot see each other's work.
        assert user.org_id == result["org_id"]
        assert "review_document" in result["capabilities"]
    finally:
        db.close()


def test_a_typo_in_the_role_is_refused_rather_than_created():
    """`capabilities_of` fails closed, so an unknown role would produce an
    account that silently cannot do anything and gives no hint why."""
    from app.bootstrap import add_user

    with pytest.raises(ValueError, match="Unknown role"):
        add_user(org_name="Bootstrap Test Org", email="typo@example.com",
                 full_name="X", password="another-long-password", role_key="approvor")


def test_adding_a_colleague_twice_does_not_reset_their_password():
    from app.bootstrap import add_user

    with pytest.raises(ValueError, match="already exists"):
        add_user(org_name="Bootstrap Test Org", email="reviewer@example.com",
                 full_name="Someone Else", password="another-long-password",
                 role_key="approver")


def test_a_colleague_cannot_be_added_to_an_organisation_that_does_not_exist():
    from app.bootstrap import add_user

    with pytest.raises(ValueError, match="does not exist"):
        add_user(org_name="No Such Org", email="nobody@example.com",
                 full_name="X", password="another-long-password", role_key="approver")


def test_a_colleagues_password_has_the_same_floor_as_the_administrators():
    from app.bootstrap import add_user

    with pytest.raises(ValueError, match="at least"):
        add_user(org_name="Bootstrap Test Org", email="weak2@example.com",
                 full_name="X", password="short", role_key="approver")


def test_bootstrap_rejects_a_weak_password():
    from app.bootstrap import bootstrap

    with pytest.raises(ValueError, match="at least"):
        bootstrap(org_name="Weak Org", email="weak@example.com", full_name="X", password="short")


# ------------------------------------------------------------- no stub LLM
def test_provider_refuses_instead_of_returning_invented_text():
    """The StubProvider echoed the top retrieved chunk back as model output and
    returned data=None for every structured call, so with no key configured the
    whole pipeline appeared to work."""
    from app.llm import provider as llm

    assert not hasattr(llm, "StubProvider")
    assert llm.llm_configured() is False
    with pytest.raises(llm.LLMNotConfiguredError) as exc:
        llm.get_llm_provider("Narrative generation", allow_unscoped=True)
    assert "not configured" in str(exc.value)


def test_no_provider_is_configured_during_tests():
    """A live key reaching the suite means tests can pass by making real,
    billable calls -- and backend/.env is exactly where a working key lives.
    Every provider must be neutralised in conftest, including new ones."""
    from app.llm import provider as llm

    for provider in llm.PROVIDER_KEY_VARS:
        assert not getattr(llm.settings, f"{provider}_api_key", None), f"{provider} key leaked into the test run"


def test_chat_answers_503_rather_than_a_made_up_answer(app_client, two_orgs):
    token_a, project_a, _tb, _pb = two_orgs
    headers = {"Authorization": f"Bearer {token_a}"}

    created = app_client.post(f"/api/v1/projects/{project_a}/conversations", headers=headers, json={"title": "t"})
    assert created.status_code in (200, 201), created.text
    conversation_id = created.json()["id"]

    res = app_client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers, json={"text": "What is in this project?"},
    )
    assert res.status_code == 503, res.text
    body = res.json()
    assert body["error"]["code"] == "LLM_NOT_CONFIGURED"
    # An explicit refusal, not a made-up answer -- and one that names no vendor
    # or setting: which key is missing is the operator's to read in the log.
    assert body["error"]["message"] == (
        "AI features are not available right now. Please contact your administrator.")
    assert "ANTHROPIC_API_KEY" not in res.text


# ------------------------------------------------------- no silent re-scoping
def test_retrieval_returns_nothing_when_nothing_matches():
    """It used to return the single best chunk even at zero similarity, so a
    section could be 'grounded' in unrelated text and cite it."""
    from app.retrieval.lexical import retrieve

    class _Chunk:
        def __init__(self, text):
            self.id, self.text = text[:8], text

    chunks = [_Chunk("annual remuneration and superannuation details"), _Chunk("dates of effect for the offer")]
    assert retrieve(chunks, "zzzqqq unrelated vocabulary xyzzy") == []


# --------------------------------------------------------- compiler dispatch
def _scan_with(texts):
    """A pre-scan stub carrying only what dispatch inspects."""
    from app.templates.parsers.docx_prescan import RunSpan

    spans = [RunSpan(paragraph_index=i, span_index=0, color="red", text=t, run_elements=[])
             for i, t in enumerate(texts)]
    return type("Scan", (), {"spans": spans, "mergefields": [], "paragraphs": texts})()


class _Compiled:
    def __init__(self, conditions):
        self.conditions = conditions


def test_colour_alone_does_not_prove_the_rules_understood_the_template():
    """A fully colour-coded template can still express every condition in a
    dialect the rules cannot parse. Dispatching on colour then yields a
    confident manifest with all the fields and none of the conditional blocks --
    so every letter ships with every optional clause in it."""
    from app.compiler.llm_compiler import choose_compiler, rules_fell_short

    scan = _scan_with(["[[IF joining_bonus > 0]]", "You will receive a joining bonus.", "[[ENDIF]]"])
    assert choose_compiler(scan) == "rules"          # there is colour, so rules are tried
    assert rules_fell_short(scan, _Compiled([]))     # but they found nothing, and there IS logic


@pytest.mark.parametrize("marker", [
    "[[IF x > 0]]", "[[ELSE IF y = 1]]", "[[ENDIF]]",   # this compensation template
    "{% if x %}", "{{#if x}}", "<!-- IF x -->",          # other dialects in the wild
    "include the following text only if the Colleague is Full Time",
])
def test_every_known_conditional_dialect_triggers_escalation(marker):
    from app.compiler.llm_compiler import rules_fell_short

    assert rules_fell_short(_scan_with([marker]), _Compiled([]))


def test_a_template_with_no_conditional_logic_is_not_escalated():
    """Escalating every condition-free template would spend a model call on
    every plain mail-merge letter in the estate."""
    from app.compiler.llm_compiler import rules_fell_short

    scan = _scan_with(["Dear <first_name>,", "Your start date is <start_date>.", "Regards,"])
    assert rules_fell_short(scan, _Compiled([])) is None


def test_rules_that_found_conditions_are_left_alone():
    from app.compiler.llm_compiler import rules_fell_short

    scan = _scan_with(["include the following text only if the Colleague is Full Time"])
    assert rules_fell_short(scan, _Compiled([{"id": "cond_x"}])) is None


def test_unreadable_logic_is_flagged_when_no_model_can_be_consulted():
    """With no key the fields are still real and worth keeping -- but a manifest
    reporting 50 fields and 0 conditions must not look like a clean compile."""
    from app.compiler.mapping_agent import _escalate_if_rules_missed_the_logic
    from app.compiler.rule_compiler import CompiledManifest

    manifest = CompiledManifest(
        fields=[{"id": "first_name"}], conditions=[], blocks=[], delete_always=[],
        mergefield_paragraphs=[], hyperlink_paragraphs=[],
        confidence=1.0, compiled_by="rule_based", prescan_summary={},
    )
    scan = _scan_with(["[[IF joining_bonus > 0]]", "bonus text", "[[ENDIF]]"])
    out = _escalate_if_rules_missed_the_logic(scan, manifest, ["x"])

    assert out.confidence <= 0.3, "a manifest missing every condition must not read as confident"
    assert any("conditional markers" in n for n in out.notes)


# --------------------------------------------- no fabricated document content
def test_saving_html_cannot_destroy_a_template_generated_document(app_client, two_orgs):
    """The editor showed a hardcoded placeholder draft ("Replace this section
    with the executive summary") whenever a version had no html_content -- which
    is *every* document the manifest path produces, since those are filled
    copies of the original .docx and carry no HTML at all.

    So a real letter displayed invented content, and one edit plus Save rebuilt
    the .docx from that placeholder at the same blob_path. The backend refuses
    now, so no frontend mistake can overwrite a finished document.
    """
    from app.db import SessionLocal
    from app.models import DocumentVersion, GeneratedDocument, User

    token_a, project_a, _tb, _pb = two_orgs
    headers = {"Authorization": f"Bearer {token_a}"}

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "user-a@tenant.test").one()
        gd = GeneratedDocument(org_id=user.org_id, project_id=project_a, display_id=99001, language="en", status="draft")
        db.add(gd)
        db.flush()
        # A surgery-produced document: real blob, no HTML representation.
        dv = DocumentVersion(document_id=gd.id, org_id=gd.org_id, version_no=1, blob_path="generated/real-letter.docx",
                             html_content="", status="draft", created_by=user.id)
        db.add(dv)
        db.flush()
        gd.current_version_id = dv.id
        version_id, blob_before = dv.id, dv.blob_path
        db.commit()
    finally:
        db.close()

    res = app_client.patch(
        f"/api/v1/document-versions/{version_id}",
        headers=headers, json={"html_content": "<h1>Generated Draft</h1><p>Point one from your source data</p>"},
    )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"]["code"] == "DOCUMENT_NOT_HTML_EDITABLE"

    db = SessionLocal()
    try:
        dv = db.get(DocumentVersion, version_id)
        assert dv.blob_path == blob_before, "the original .docx was replaced"
        assert not dv.html_content, "placeholder HTML was written onto a template-generated document"
    finally:
        db.close()


def test_the_editor_ships_no_placeholder_document():
    """A fallback document is indistinguishable from real output once rendered.

    Checks the *markup* rather than the prose, so the comment in that file
    explaining what was removed (and why) does not trip the guard.
    """
    from pathlib import Path

    editor = Path(__file__).resolve().parents[2] / "src/routes/_app.projects.$id_.edit.$docId.tsx"
    body = editor.read_text()
    for markup in ("<h1>Generated Draft</h1>", "<li>Point one from your source data</li>",
                   "<li>Verify data accuracy</li>", "<blockquote>Any relevant citation"):
        assert markup not in body, f"placeholder document still shipped: {markup!r}"
    assert "DEFAULT_HTML" not in body.replace("a DEFAULT_HTML draft", ""), "fallback constant still referenced"

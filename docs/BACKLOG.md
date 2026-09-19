# DocuMind AI — Backlog

Unfinished work, pulled from [ARCHITECTURE.md](ARCHITECTURE.md) (the former root README): §12 ("What is wired, and what is a seam"), §11 (data model) and §14 (tests and CI). Nothing here is invented scope. Every "current state" traces to something that document says is mock, unwired, dormant or missing, or to a defect found in the end-to-end QA run (A7–A9).

## Status check (2026-09-20)

Checked against the code on branch `feat/ip-hardening`:

| Item | Finding |
|---|---|
| **A5** | Confirmed. None of the five capabilities is used by any router. |
| **A2** | Partly out of date. The customer-facing **AI Models** tab was removed on purpose (IP hardening), so it must not come back for customers. If admins need to see provider settings, put them on an internal/operator-only surface. Workspace, Notifications and Security are still static. |
| **A3, A4, A6** | Confirmed. There is no user-management API, no refresh or reset flow, and no API-key model. |
| **B2** | Confirmed. All three document-creation sites pass `draft_id=None` (`routers/generation.py:88`, `generation/single.py:140`, `generation/batch_runner.py:178`). |
| **D3** | Confirmed. CI uses `pgvector/pgvector:pg16` (`.github/workflows/ci.yml:30`). |
| **D5** | Partly done. The "watsonx" references were removed from `BACKEND_SPEC.md`. The §-number drift is still there. |
| **New: A7–A9** | These were found by the QA run (`~/Desktop/TemplateAI-QA/features/hr-offer-letter/REPORT.md`) and are still open in the code. |

## How to use this
- Each task is self-contained: **current state → acceptance criteria → pointers**. Paste one whole block into Claude Code as a prompt and work it to done.
- Do them in roughly A → B → C/D order. The IDs are stable, so you can say "do A5" or "do B3".
- Scope note: this hardens the product. It does **not** create demand or settle who owns the code. Those are separate and still gate any go-to-market. This file assumes you've decided to build regardless.

## Priority legend
- **A — Blocks it from being a real/paid product.** Without these you can't charge for it or safely onboard a customer.
- **B — "Does what it claims."** Features that are built but not wired, so the product under-delivers on its own pitch.
- **C — Production hardening & scale.** Needed before real load or enterprise buyers.
- **D — Testing & hygiene.** Reduces risk and drift. Do alongside the rest.

---

## Priority A — Blocks a real/paid product

### A1 [BILLING] Make it possible to charge
**Current state:** Settings › Billing is static JSX. There is no plan, no usage-to-invoice and no payment. `llm_calls` already meters model cost in integer micro-dollars, but nothing turns usage into a bill, so there is no way to take money.
**This is an epic — split it further before building.** A minimum viable slice is below.
**Acceptance criteria:**
- [ ] Define at least one paid plan with limits (e.g. documents/month or seats), stored per organisation.
- [ ] Meter the billable unit per org (reuse `llm_calls` where relevant) and expose current-period usage via an endpoint.
- [ ] Enforce plan limits (block or warn at a threshold) rather than letting usage run unbounded.
- [ ] Integrate a payment provider (e.g. Stripe): create customer, subscribe, handle the webhook for payment success/failure.
- [ ] The Billing tab reads real plan, usage and invoice data (no static JSX).
- [ ] Tests cover metering maths, limit enforcement and webhook handling.
**Pointers:** `llm_calls`, `org_model_rates`, Settings › Billing (`src/routes/_app.settings.tsx`).

### A2 [SETTINGS] Replace mock Settings tabs with real data
**Current state:** Only the Profile tab reads real data. Workspace, Notifications and Security are static JSX with no handlers. (API keys and Billing are covered by A6 and A1. The customer AI Models tab was deliberately removed; see the status check.)
**Acceptance criteria:**
- [ ] The Workspace tab reads and writes real org settings (name, locale/residency defaults).
- [ ] Provider configuration (`LLM_PROVIDER` / `LLM_COMPILE_PROVIDER`) is visible only on an operator/internal surface, never to customers. Respect the "assertion not preference" rule for residency and zero-retention.
- [ ] The Notifications tab persists real preferences (or is removed if out of scope for v1).
- [ ] The Security tab shows real values (session lifetime; a read-only RLS/boot-guard status is fine).
- [ ] Each tab has backend handlers and tests; no static-JSX settings screens remain.
**Pointers:** `org_data_policies`, `app/config.py`, `src/routes/_app.settings.tsx`.

### A3 [USER-MGMT] Self-serve user & role management in the app
**Current state:** There is no user-management API. Accounts and roles are created only via `python -m app.bootstrap` and `bootstrap add-user`. The team screen is read-only, with no invite or role-change controls.
**Acceptance criteria:**
- [ ] An admin can invite a user (email + role) from the UI; the invite creates a pending account.
- [ ] An admin can change a user's role and deactivate or remove a user from the UI.
- [ ] Backend endpoints enforce admin capability and tenant isolation (org-scoped, `owned_*` convention).
- [ ] The team screen is writable and reflects changes live.
- [ ] The four-eyes / `SELF_REVIEW_REFUSED` rule still holds after role changes (regression test).
**Pointers:** `bootstrap.py`, `users`, `authz.py`, `ownership.py`, `src/routes/_app.team.tsx`.

### A4 [AUTH-SELFSERVE] Password reset + refresh tokens
**Current state:** Auth is JWT-only, with no refresh tokens and no password reset. Tokens expire after 8 hours and there is no reset, so a locked-out user has no way back in. (SSO/OIDC is separate; see C8.)
**Acceptance criteria:**
- [ ] Password-reset flow: request → time-limited single-use token (Redis-backed, like download grants) → set new password (bcrypt) → invalidate token.
- [ ] A refresh-token flow renews sessions without re-login; refresh tokens are revocable via the existing Redis blacklist.
- [ ] Reset requests are rate-limited per IP/email (reuse the login limiter pattern).
- [ ] Tests cover token expiry, single use and revocation.
**Pointers:** `security.py`, `rate_limit.py`, Redis revocation blacklist.

### A5 [AUTHZ] Enforce the five declared-but-unenforced capabilities
**Current state:** Five of ten capabilities are declared but not enforced: `UPLOAD_TEMPLATE`, `UPLOAD_SOURCE`, `COMPILE_MANIFEST`, `EDIT_MANIFEST` and `GENERATE_DOCUMENT`. Any authenticated member can upload, compile and generate. Only the approval, review, audit-read and admin routes actually check.
**Acceptance criteria:**
- [ ] Each of the five capabilities gates its corresponding route(s) through the existing capability system.
- [ ] A member lacking a capability gets the project's standard authorization-failure response (403 for capability denial; keep the 404-for-not-yours tenancy convention distinct).
- [ ] A test asserts, per capability, that a role without it is refused and a role with it is allowed.
- [ ] The endpoint-classification meta-test still passes (every route classified).
**Pointers:** `authz.py` (roles at lines 61–68), the tenancy/classification meta-test.

### A6 [API-KEYS] Real API-key issuance and auth
**Current state:** The API keys tab is static JSX. The product advertises an API, but a customer has no way to create or use a key.
**Acceptance criteria:**
- [ ] An admin can create, list and revoke org-scoped API keys from the UI.
- [ ] Keys are stored hashed; the raw key is shown once, at creation.
- [ ] API requests can authenticate with a key (org-scoped, capability-checked) as an alternative to JWT.
- [ ] Key use is rate-limited and audited (creation, use, revocation).
- [ ] Tests cover creation, auth, revocation and scope isolation.
**Pointers:** existing JWT/`org_id` model, `audit_logs`, `rate_limit.py`.

### A7 [FOUR-EYES] An author can approve their own document  *(new, from QA)*
**Current state:** `approve_document_version` checks only `require(APPROVE_DOCUMENT)` and never compares the approver with the author. An org admin (who holds `approve_document`) can generate a letter and approve it themselves whenever no review is open. The four-eyes check exists only when a review is closed (`check_document_review_resolution`). Marketing copy has been softened to avoid claiming otherwise.
**Acceptance criteria:**
- [ ] Approving a document version refuses when the approver created it (`SELF_APPROVAL_REFUSED`, 403), consistent with `SELF_REVIEW_REFUSED`.
- [ ] Decide and document whether an org with only one user may override this (probably not for templates flagged legally binding).
- [ ] Regression test: the author is refused, a second person succeeds.
**Pointers:** `routers/generation.py` `approve_document_version`, `authz.py:190` `check_document_review_resolution`.

### A8 [PREVIEW-PII] Row preview leaves a filled document with personal data on disk  *(new, from QA)*
**Current state:** `POST /template-manifests/{id}/preview-row` promises "no trace", but `batch_runner` renders the filled document to `storage/generated/...-preview-row0.docx`. For `persist=False` it returns without deleting that file (`generation/batch_runner.py:159`), so a candidate's name, address and salary stay on disk with no record and no retention sweep.
**Acceptance criteria:**
- [ ] A non-persisted preview renders to a temporary location and is deleted in a `finally`, even on failure.
- [ ] Test: after a preview, no new file exists under storage.
**Pointers:** `generation/batch_runner.py:131-164`, `routers/bindings.py` preview-row route.

### A9 [AUDIT-GATE] `GET /audit-logs` isn't gated on `read_audit`  *(new, from QA)*
**Current state:** `list_audit_logs` depends only on `get_current_user` (`routers/admin.py:204-205`), so any member of the org can read the whole audit log. `/metrics` is correctly gated.
**Acceptance criteria:**
- [ ] The route requires `READ_AUDIT`.
- [ ] Test: a role without `read_audit` gets a 403, and one with it gets a 200.
**Pointers:** `routers/admin.py:204`, `routers/metrics.py:61` for the pattern. Fold into C6 if doing both.

---

## Priority B — "Does what it claims" (built but not wired)

### B1 [NARRATIVE] Wire the narrative / fuzzy resolution engine
**Current state:** The narrative/RAG half of the resolution engine is a seam. No caller injects a `narrative_resolver` or `fuzzy_resolver`, so every `narrative` unit and every fuzzy condition takes the refusal branch and waits for a person. Narrative generation doesn't run unattended, yet this is the AI capability meant to set the product apart.
**Acceptance criteria:**
- [ ] A `narrative_resolver` is injected into the resolution engine and actually resolves `narrative` units at generation time (schema-constrained, citation-carrying, invented chunk ids filtered).
- [ ] A `fuzzy_resolver` handles fuzzy conditions instead of always parking them.
- [ ] The human-loop safety still applies: low-confidence or poorly grounded output waits for review rather than shipping silently.
- [ ] End-to-end test: a template with a narrative section produces a grounded section unattended on the happy path, and waits for review on the low-confidence path.
**Pointers:** `generation/resolution_engine.py`, `llm/` provider + boundary, `retrieval/`, `review_tasks`.

### B2 [CITATIONS] Surface citation grounding
**Current state:** `GET /document-versions/{id}/citations` can only return an empty list. Every document-creation site hardcodes `draft_id=None`, and nothing writes the citations column. Grounding is real at the provider boundary but never shown.
**Acceptance criteria:**
- [ ] Document-creation sites pass a real draft/generation id instead of `None`.
- [ ] Citations produced at the provider boundary are saved to the citations column.
- [ ] The endpoint returns real citations for a generated document.
- [ ] A test asserts that a generated narrative document exposes non-empty, correct citations.
**Depends on:** B1 (narrative must run for citations to exist).
**Pointers:** `section_outputs`, `document_versions`, the three call sites in the status check.

### B3 [RETRIEVAL-COVERAGE] Feed retrieval evidence to all compile sites
**Current state:** Retrieval evidence reaches only 1 of 4 compile call sites. Only single-template compile passes it; bulk onboarding and both blueprint compiles don't. So bulk-onboarded templates compile with no knowledge of the org's existing column names, which is exactly the case the feature was built for.
**Acceptance criteria:**
- [ ] Bulk onboarding passes org retrieval evidence into compile.
- [ ] Both blueprint compile paths pass retrieval evidence.
- [ ] A test verifies that evidence reaches all four call sites (e.g. a spy/assertion per site).
- [ ] A bulk-onboarded template demonstrably names fields after existing org columns.
**Pointers:** `compiler/agentic_compiler.py`, bulk-onboard endpoint, blueprint compile paths, `retrieval/hybrid`.

### B4 [MAPPING-MEMORY] Persist mapping memory at runtime
**Current state:** Mapping memory isn't saved anywhere at runtime. On every request, the binding-suggestions endpoint rebuilds an in-memory copy from existing bindings. `mapping_memory`, `mapping_memory_sharing` and `reviewer_corrections` are modelled and migrated but never written. Cross-tenant structural-pattern sharing has no HTTP surface.
**Acceptance criteria:**
- [ ] Approved and rejected `(field, column, transform)` triples, with rejection counts, are written to `mapping_memory` / `reviewer_corrections`.
- [ ] Binding suggestions read the saved memory instead of rebuilding it on each request.
- [ ] The historical-approvals signal is driven by saved data.
- [ ] (Optional, later) An HTTP surface for opt-in cross-tenant structural-pattern sharing via `mapping_memory_sharing`.
- [ ] Tests cover saving, the effect of rejection counts, and that a rejected column stops being re-proposed.
**Pointers:** `retrieval/mapping_memory.py`, binding-suggestions endpoint, the three memory tables. The QA certificate run shows why this matters: 2 of 14 first suggestions were wrong.

### B5 [REPRO-PIN] Wire the manifest reproducibility envelope
**Current state:** The manifest envelope's `(template_hash, manifest_hash)` pin is implemented but unwired. The approval endpoint supersedes and stamps directly rather than building an envelope, so the pin exists only in `versioning.py` and the tests.
**Acceptance criteria:**
- [ ] The approval endpoint builds and stores the envelope with the hash pin.
- [ ] Generation verifies the pin (template and manifest bytes match what was approved) before filling.
- [ ] A mismatch is refused with a clear error rather than silently proceeding.
- [ ] Test: tampering with the template or manifest after approval blocks generation.
**Pointers:** `manifests/envelope.py`, `manifests/versioning.py`, approval endpoint.

### B6 [EMIT-STRUCTURAL] Structural edits for `emit_from_base`
**Current state:** `emit_from_base` publishes a customer's own file by editing the package, but refuses any structural change. There isn't yet a set of operations that can safely add or remove paragraphs in a real customer package.
**Acceptance criteria:**
- [ ] Define validated operations to add or remove paragraphs (and rows) in a base package without breaking the round-trip invariant (`blueprint → emit → prescan → compile → blueprint′ == blueprint`).
- [ ] `emit_from_base` accepts those structural operations and preserves layout and styling by construction.
- [ ] The emit round-trip assertion still passes for structurally edited blueprints.
- [ ] Tests cover add and remove at paragraph and table-row granularity.
**Pointers:** `templates/blueprint` (+ ops), `emit_from_base`, the 11 typed operations, emit round-trip assertion.

### B7 [KOREAN-POSTPOSITION] Persist the column that enables the Korean pass
**Current state:** The Korean postposition pass never runs, because the manifest column that would enable it isn't saved.
**Acceptance criteria:**
- [ ] The enabling column is saved on the manifest.
- [ ] The postposition pass runs for Korean templates when the column is set.
- [ ] Test: a Korean template gets the correct postpositions in its output.
**Pointers:** manifest model, `ko` convention YAML, fill/format path.

### B8 [DORMANT-CHECKS] Activate dormant QA/compiler checks
**Current state:** Three checks never run:
- `value_exceeds_max_len` stays silent because the compiler emits no `max_len`.
- The `scaffolding_conflict` assertion is declared but never raised.
- The compiler's optional test-fill assertion (`test_fill_failure`) can only be reached from tests, because the API never passes `compile_template(test_fill=...)`.

**Acceptance criteria:**
- [ ] The compiler emits `max_len` where it can be worked out, so `value_exceeds_max_len` can fire; a test proves it catches an over-length value.
- [ ] `scaffolding_conflict` is raised in the case it was meant for (or removed if it's genuinely obsolete, with a justification).
- [ ] The API path can turn on test-fill so `test_fill_failure` can fire in production compiles; a test proves it.
**Pointers:** `compiler/assertions.py`, `app/qa/policy.py`, `compiler/agentic_compiler.py`.

---

## Priority C — Production hardening & scale

### C1 [BATCH-QUEUE] Durable batch generation
**Current state:** Batch generation runs as an in-process background task with no worker queue. A restart mid-batch loses the run, and the only record of how far it got is the job row's progress.
**Acceptance criteria:**
- [ ] Batch work runs on a durable queue (e.g. RQ/Celery/Arq on the existing Redis) that survives process restarts.
- [ ] A mid-run restart resumes, or safely retries from committed progress; no rows are silently lost.
- [ ] Job status and progress stay accurate across workers.
- [ ] A test simulates a restart mid-batch and asserts recovery.
**Pointers:** `generation/batch_runner.py`, `generation_jobs`, Redis. The same applies to the new background template reading (`run_compile_in_background` in `routers/manifests.py`).

### C2 [COMPILE-PUSH] Push compile progress
**Current state:** Compile progress is polled, not pushed. There's no WebSocket or SSE; the client creates a token and reads progress back through the jobs endpoint.
**Acceptance criteria:**
- [ ] The server pushes compile progress via SSE or WebSocket.
- [ ] The frontend uses the pushed updates (polling can remain as a fallback).
- [ ] A test or manual check confirms live updates without polling.
**Pointers:** compile endpoint, jobs endpoint, `src/lib/background-tasks.ts`, `compile-progress.tsx`.

### C3 [OBJECT-STORAGE] Pluggable object storage
**Current state:** Object storage is the local filesystem (about 32 lines of shutil/pathlib), with no S3, MinIO, Azure or GCS. This blocks horizontal scaling and most cloud deploys.
**Acceptance criteria:**
- [ ] A storage interface with at least one cloud backend (S3-compatible) alongside the local one, chosen by config.
- [ ] Templates, sources and outputs all go through the interface.
- [ ] Download grants and download restrictions still work against the cloud backend.
- [ ] Tests run against the local backend; the cloud backend has integration coverage or a documented manual check.
**Pointers:** the filesystem store module, `downloads.py`.

### C4 [ANN-SEARCH] Use the HNSW / pgvector ANN path
**Current state:** The HNSW index is created but never used. Vector search fetches the tenant's rows and computes cosine similarity in numpy. That means pgvector currently provides only the column type and the tenant filter, not approximate nearest-neighbour (ANN) search. It's fine at small scale but won't hold up for customers with very large numbers of templates.
**Acceptance criteria:**
- [ ] Vector search uses the pgvector ANN index (tenant filter applied before or with scoring, never after).
- [ ] Results stay tenant-isolated and match the numpy path within tolerance on a fixture.
- [ ] A benchmark shows improved latency at realistic row counts.
**Pointers:** `retrieval/vector.py`, pgvector `vector(1024)` column, HNSW index.

### C5 [METRICS-UI] Surface escaped-error rate & calibration log
**Current state:** Escaped-error reporting and the calibration log have no UI. The backend ranks escaped-error rate first among its metrics, but the only endpoint that would make it measurable can't be reached from the shipped product.
**Acceptance criteria:**
- [ ] A metrics screen shows escaped-error rate and the calibration log, **admin-only**. Per the IP hardening, the customer-facing Quality page was removed, so this must not expose method or scoring internals to customers.
- [ ] The relevant endpoint(s) are reachable and org-scoped.
- [ ] A test confirms the endpoint returns real metric data.
**Pointers:** `metrics.py`, `analytics.py`, `qa_failure_logs`, `suggestion_logs`.

### C6 [AUDIT-UI] Wire audit-log filters, export, pagination
**Current state:** Audit-log filters, export and pagination have no handlers. The append-only log exists (49 call sites, about 44 event labels) but isn't usable from the UI.
**Acceptance criteria:**
- [ ] Backend handlers for filtering (by actor, event or date), pagination and export (CSV or JSON).
- [ ] The frontend audit view uses them.
- [ ] Access is gated on admin/audit-read and scoped to the org (see A9).
- [ ] A test covers filtering and export.
**Pointers:** `audit_logs`, `audit/`, admin routes.

### C7 [PDF-ROBUST] Harden PDF export
**Current state:** PDF export needs LibreOffice on the host. The container installs `libreoffice-writer`, but a dev machine without `soffice` returns `503 PDF_UNAVAILABLE`. The overlay path is limited to Helvetica/WinAnsi because there's no PDF-writing library in the dependency set.
**Acceptance criteria:**
- [ ] A clear, documented runtime requirement, and graceful degradation when `soffice` is absent (already a 503 plus `_FAILED.txt`; verify and document it).
- [ ] The overlay path supports fonts beyond Helvetica/WinAnsi (add a PDF-writing library or embed fonts) so non-Latin overlays render.
- [ ] The CJK survival check still refuses on font substitution.
- [ ] Tests cover a non-Latin overlay and the missing-`soffice` path.
**Pointers:** `generation/pdf_renderer.py`, `render_overlay`, Docker image.

### C8 [SSO-OIDC] Enterprise SSO
**Current state:** Auth is JWT-only, with no SSO/OIDC. Enterprise buyers in regulated industries will require it.
**Acceptance criteria:**
- [ ] OIDC login against at least one identity provider (e.g. Azure AD or Okta), org-scoped and mapped to the existing roles.
- [ ] JWT/local auth still works alongside SSO.
- [ ] Tests, or a documented integration check, cover the SSO flow.
**Pointers:** `security.py`, `users`, `authz.py`.

---

## Priority D — Testing & hygiene

### D1 [FRONTEND-TESTS] Add a frontend test suite + lint + build in CI
**Current state:** There is no frontend test suite. The only frontend CI check is `tsc --noEmit`; neither lint nor build runs.
**Acceptance criteria:**
- [ ] A frontend test runner is set up, with meaningful coverage of the core flows (mapping, batch progress, review bar, template upload dialog).
- [ ] CI runs frontend `lint` and `build` as well as `tsc --noEmit`.
- [ ] CI fails if a frontend test, lint or build fails.
**Pointers:** `src/`, `.github/workflows/ci.yml`, `backend/scripts/check.sh` as the pattern.

### D2 [PY-HYGIENE] Python linter, formatter, and pytest config
**Current state:** There's no Python linter, formatter or pytest config file, so a bare `pytest` runs with no coverage and no floors.
**Acceptance criteria:**
- [ ] A linter and formatter (e.g. ruff + black, or ruff-format) are configured and run in CI.
- [ ] A `pyproject.toml`/`pytest.ini` sets coverage and the per-module floors, so a bare `pytest` enforces them.
- [ ] CI fails on lint or coverage-floor violations.
**Pointers:** `backend/`, `scripts/check.sh`, the 34 per-module floors.

### D3 [CI-PG-VERSION] Align CI Postgres with compose
**Current state:** CI's PostgreSQL image is pg16 (`.github/workflows/ci.yml:30`), while compose uses pg17.
**Acceptance criteria:**
- [ ] CI runs the PostgreSQL job on pg17 (matching compose), or the mismatch is deliberately documented with a justification.
- [ ] The full suite passes on the aligned version.
**Pointers:** `.github/workflows/ci.yml`, `docker-compose.yml`.

### D4 [DEAD-TABLES] Remove or formally deprecate vestigial tables
**Current state:** `draft_documents` and `mappings` are leftovers from the removed draft and mapping-wizard pipeline. Nothing writes to either; existing rows are read-only history.
**Acceptance criteria:**
- [ ] Decide: drop them via a migration, or mark them clearly deprecated in the model with a comment and a plan.
- [ ] If dropped, the migration is reversible and no code references remain.
- [ ] Schema docs are updated.
**Pointers:** `models.py`, Alembic revisions.

### D5 [DOC-DRIFT] Fix stale documentation references
**Current state:**
- The `docs/BACKEND_SPEC.md` §-numbers cited in code comments don't point to anything in that document; the code quotes an architecture record that isn't in the repo.
- Parts of `BACKEND_SPEC.md` describe a target rather than the code.
- Older docs still describe the removed TF-IDF clustering, which has been replaced by structural-fingerprint clustering.

**Acceptance criteria:**
- [ ] Code comments cite references that can be found, or are marked as external provenance notes.
- [ ] `BACKEND_SPEC.md` clearly separates "target" from "implemented" (its status block is accurate).
- [ ] Clustering docs describe the structural-fingerprint approach, not TF-IDF.
**Pointers:** `docs/BACKEND_SPEC.md`, `templates/fingerprint`, code comments that cite §-numbers.

### D6 [FIXTURES] Cover modules currently gated on customer-owned files
**Current state:** `rule_compiler`, `docx_renderer` and `docx_prescan` can only reach their coverage floors with customer-owned template masters, which aren't published. So 345 tests are skipped on a fresh checkout, and those floors are reported as unmeasurable.
**Acceptance criteria:**
- [ ] Synthetic or anonymised template fixtures reproduce the structures those tests need, with no customer data.
- [ ] The three modules meet their floors in CI without customer-owned files.
- [ ] The skipped-test count on a fresh checkout drops accordingly.
**Pointers:** `conftest.py`, golden-DOCX fixtures, the three 100%-floor modules. The Veridane QA pack (`~/Desktop/TemplateAI-QA/lib/brandkit.py`) already builds realistic colour-coded masters and is a good starting point.

---

## Suggested sequence
1. **A7, A8, A9, A5**: small, high-risk fixes. A7 and A9 are one-route changes; A8 is a `finally`.
2. **A4, A6**: close the remaining auth gaps.
3. **A3, A2, A1**: make it operable and chargeable (A1 is an epic; slice it).
4. **B1 → B2**, then **B3, B4, B5**: deliver the AI features the product already claims.
5. **C1, C3** before any real load; **C4, C7, C8** as scale or enterprise demand requires.
6. **D1–D6** in parallel throughout: cheap insurance against regressions and drift.

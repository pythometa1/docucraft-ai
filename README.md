# DocuMind AI

**Turn manual, template-based document work into an audited, AI-assisted workflow — without rewriting a single template by hand.**

![status](https://img.shields.io/badge/status-active%20development-brightgreen) ![frontend](https://img.shields.io/badge/frontend-React%20%2F%20TanStack%20Start-blue) ![backend](https://img.shields.io/badge/backend-FastAPI%20%2F%20PostgreSQL%20%2F%20Redis-blue) ![license](https://img.shields.io/badge/license-proprietary-lightgrey)

> **This file is the living, canonical overview of the whole project.** Every time the product, architecture, or data model changes in a meaningful way, this README should be updated alongside it — it is the front door for anyone (teammate, reviewer, future you) trying to understand what DocuMind AI is, why it exists, and how the pieces fit together. Deep technical detail lives in the companion docs linked in [§10](#10-full-documentation-index); this file is the map to all of it.

---

## Table of Contents

1. [What This Project Is](#1-what-this-project-is)
2. [The Problem & Use Cases](#2-the-problem--use-cases)
3. [How It Works — End to End](#3-how-it-works--end-to-end)
4. [The Generation Engines](#4-the-generation-engines)
5. [Impact — Why This Matters](#5-impact--why-this-matters)
6. [System Architecture](#6-system-architecture)
7. [Technology Stack](#7-technology-stack)
8. [Data Model / Database](#8-data-model--database)
9. [Project Structure](#9-project-structure)
10. [Full Documentation Index](#10-full-documentation-index)
11. [Getting Started](#11-getting-started)
12. [Security & Compliance Posture](#12-security--compliance-posture)
13. [Current Status & Roadmap](#13-current-status--roadmap)
14. [Origins](#14-origins)

---

## 1. What This Project Is

DocuMind AI is an enterprise document-generation platform for regulated, template-heavy industries — HR, clinical research, quality/CMC, medical affairs, legal. Users upload a **template** (the structure/layout/legal boilerplate that must never change) and one or more **source files** (the data that fills it in), map the two together, and generate a finished document that keeps the template's exact formatting while the content comes from real, traceable data.

It ships as a full application, not a prototype: a polished React frontend and a real FastAPI backend running against PostgreSQL and Redis, with an LLM in the loop only where grounded generation is actually needed — never where deterministic data-filling would do.

---

## 2. The Problem & Use Cases

### 2.1 The manual process this replaces

Across HR, clinical, and regulatory teams, someone opens a Word template, reads instructions the template's author left behind (often literally colour-coded — blue for "put a value here," red for "read this and decide"), copies values across from a spreadsheet or system export by hand, deletes the sections that don't apply to this particular case, deletes the instructions themselves, and saves the result. It's repeated thousands of times a month per team, and at an industry level, across **lakhs of distinct templates**, each encoding its own fields and rules informally, for a human reader.

### 2.2 Concrete use cases this platform targets

| Function | Example documents | What's automated |
|---|---|---|
| **Human Resources** | Offer letters, termination letters, promotion memos, policy communications | Full/part-time/fixed-term conditional clauses, salary tables, compliance boilerplate |
| **Clinical Research** | Clinical study reports, protocol amendments, informed consent forms | Long narrative sections grounded in study data, standardized structure per ICH guidance |
| **Quality / CMC** | Batch records, deviation reports, CMC sections (ICH M4Q format) | Structured technical sections pulled from lab/quality systems |
| **Medical Affairs** | Medical letters, publication summaries, congress posters | Mixed narrative + data-driven content |
| **Marketing / Legal** | Product briefs, campaign copy, vendor agreements (MSAs) | Templated legal boilerplate with data-specific clauses |

### 2.3 The specific hard case this was built and proven against

A real Hospira Australia (Pfizer) HR offer-letter template was used as the design's acceptance test: a 207-paragraph legacy Word document where the original author colour-coded the text by hand — blue runs are exact-value placeholders (`<Colleague First Name>`), red runs are instructions for the human processor (`Include the following section only if the Colleague Type is Fixed Term:`), and the document embeds seven legacy Word `MERGEFIELD` codes across two remuneration tables. See [§10](#10-full-documentation-index) for the full research behind this.

---

## 3. How It Works — End to End

```mermaid
flowchart LR
  A[Upload template] --> B[Upload source data]
  B --> C[Compile the template\ninto a manifest]
  C --> D[Review and approve\nwhat the compiler understood]
  D --> E[Map manifest fields\nto spreadsheet columns]
  E --> F[Generate: one document per row\nno LLM in the loop]
  F --> G[Assembled DOCX]
  G --> H[Review & Approve]
  H --> I[Versioned, audited,\ndownloadable document]
```

1. **Import a template** — a DOCX blueprint defining structure, styles, headers/footers, and any embedded placeholders or instructions.
2. **Import source data** — a CSV or XLSX of rows (one row per document), or DOCX/PDF/text for narrative material.
3. **Compile** — the system reads the template's *own* embedded rules: which runs are placeholders, which are instructions to a human processor, which paragraphs a conditional block covers, where the `MERGEFIELD` codes are. The result is a **manifest**: a machine-executable description of what this template means.
4. **Review and approve** — a person reads the conditions in plain English ("Keep when the colleague type is Fixed Term"), sees exactly what is blocking approval, and signs off once. Nothing generates from a manifest nobody approved.
5. **Map** — each manifest field is bound to a spreadsheet column, with the system proposing matches and saying how much evidence each one has. Values that match no branch of the template are flagged before they can silently drop a section from someone's letter.
6. **Generate** — one document per row, produced by *mutating a copy* of the original template rather than rebuilding it, so layout is never at risk. A few canary rows are rendered and QA-checked first; the rest only run if they pass.
7. **Review & approve, and everything is audited** — full version history, and field-level lineage (which source value, which confidence) plus condition-level lineage (which rule fired and why) for every document.

Steps 3–6 are one screen — **Document Mapping**, stage 3 of every project.

---

## 4. The Generation Engines

This is the single most important architectural decision in the project: **not every document needs an LLM at generation time, and pretending otherwise wastes money, adds hallucination risk, and threatens layout fidelity.**

The deterministic engine below is the main path and produces the great majority of documents. Grounded generation still exists, but as a *component* rather than as a whole-document pipeline.

### 4.1 Grounded generation (RAG), where prose genuinely has to be written

An LLM is used where content must be **written** rather than **looked up**: project chat grounded in the source files, `prompt` tokens inside natively-authored templates, and manifest units of kind `narrative`. Retrieval is hybrid — a tenant/document-type filter first, then lexical (keyword/TF-IDF) matching for exact placeholder codes, then pgvector similarity for naming variation, merged and reranked — and every generated sentence must cite the source chunk it came from; a citation the model invents is dropped rather than trusted.

A `narrative` unit in a manifest does **not** get written unattended: it lands in the human review queue with enough context for a person to decide. A wrong number in a tox report is not an acceptable failure mode.

> **Removed.** An earlier version of this section described a *Section-Mapping Generation Engine*: parse the template into a heading tree, select sections in a mapping wizard, call an LLM once per section, splice the results back in. That whole pipeline — the wizard screen, the draft entity, and the `/drafts/…` endpoints behind it — has been deleted. It had no manifest, so it filled nothing: a template full of placeholders came back out of it unchanged.

### 4.2 Template Compiler + Universal Fill Engine (deterministic)

**This is the main path.** For legacy, colour-coded templates like the Hospira/Pfizer example, running an LLM over every generated letter would be slow, costly, non-deterministic, and risk hallucinating a salary figure. Instead:

1. **Pre-scan** (deterministic, no LLM): unzip the DOCX, classify every text run by colour, inventory every `MERGEFIELD`, hyperlink, and table, and assign every paragraph a stable position — the same way, every time.
2. **Compile a manifest** (once per template, LLM-optional): turn the colour/instruction inventory into a machine-executable JSON manifest — fields, their source-mapping hints, conditional expressions, and exactly which paragraphs each conditional block covers. A human reviews and approves it once.
3. **Fill deterministically** (every document, zero LLM calls): evaluate conditions, replace placeholder text in place (styling inherited automatically since only the text node changes), resolve `MERGEFIELD`s to formatted values, delete the instruction text, and run QA checks (no leftover placeholder brackets, no leftover instruction text, exactly the right blocks kept/dropped).

The template is compiled **once**; after that, generating a perfect letter is deterministic, auditable, and effectively free — whether it's the first letter or the ten-thousandth. Full design rationale, the actual manifest schema, and the exact bugs found and fixed while validating this against the real file are in `docs/TEMPLATE_COMPILER_RESEARCH.md`.

Two details that matter in practice:

- **Conditions are rendered into plain English for the approver.** The sentence is generated from the same expression tree the evaluator runs, never from a description written alongside it — a hand-written description drifts from its expression the first time the expression is edited, and a drifted description is a false statement of what was approved. An expression that cannot be rendered is shown as broken rather than omitted; it is the one that must not be approved, so it must not look like a blank.
- **A batch does not run blind.** Three canary rows, spread evenly across the batch rather than taken from the front (a spreadsheet arrives sorted, so the first three are usually the same department and the same branch of every condition), are rendered and QA-checked before the remaining rows are attempted.

### 4.3 Bulk onboarding

Given many legacy templates at once, the platform clusters them by structural similarity (TF-IDF over extracted text) so **one manifest is compiled per family of near-duplicate templates**, not per file — the same lever that makes onboarding lakhs of real-world templates tractable instead of a lakh-sized manual re-authoring project.

---

## 5. Impact — Why This Matters

| Dimension | Before | After |
|---|---|---|
| **Speed** | A skilled operations person manually fills one letter at a time, reading and interpreting instructions | Seconds per document; a batch of 500 source rows can produce 500 letters in one pass |
| **Consistency** | Human error in copying values, missed conditional deletions, inconsistent tone | Deterministic fill for data-driven sections; grounded, cited generation for narrative sections |
| **Auditability** | Tribal knowledge; no record of who changed what or why | Every generated document has field-level lineage (which source value, which confidence) and condition-level lineage (which rule fired and why), plus an immutable audit log |
| **Compliance** | Manual review is the only safety net | QA gates block a document from shipping if placeholders are left unfilled or instructions leak into the output; e-signature capture for regulated approvals |
| **Cost at scale** | Linear in headcount — more templates and more volume means more people | One LLM call to compile a template manifest (not per letter); after that, filling is pure CPU. Scales to "lakhs of templates" without lakhs of engineers or lakhs of LLM calls |
| **Change management** | A template edit means re-training whoever fills it out by hand | A changed template is recompiled; the manifest diff shows exactly what changed, ready for a quick human review |

---

## 6. System Architecture

```mermaid
flowchart TB
  subgraph Client
    FE[React 19 + TanStack Start frontend]
  end
  FE -->|HTTPS/JSON| API[FastAPI backend]
  API --> PG[(PostgreSQL 17 + pgvector\nAlembic migrations, row-level security)]
  API --> REDIS[(Redis\nrate limiting, session revocation,\ndownload grants)]
  API --> STORE[(Local/object storage\ntemplates, sources, generated docs)]
  API --> LLM[LLM Provider\nAnthropic / Gemini / OpenAI, pluggable]
  API --> TPL[Template Compiler +\nUniversal Fill Engine\nno LLM at generation time]
  API --> RAG[Grounded generation\nchat, prompt tokens, narrative units]
  RAG --> RET[Hybrid retrieval\nlexical + pgvector, tenant-filtered]
```

**Architecture style: modular monolith.** One FastAPI application with clearly bounded modules (`templates`, `compiler`, `manifests`, `expressions`, `retrieval`, `generation`, `qa`, `llm`, `audit`) rather than microservices — the domain is a single linear pipeline (template → sources → compile → approve → bind → generate → review), and splitting it into separate services today would add operational overhead without solving a scaling problem the project actually has. Heavy work (parsing, compiling, DOCX surgery) is where complexity is isolated, not the service boundaries.

**Tenant isolation is enforced twice, on purpose.** Every handler checks `org_id`, and underneath that PostgreSQL row-level security keys on a per-session `app.current_org` setting, so a forgotten `WHERE` clause fails closed instead of leaking. That only works if the application cannot bypass a policy: the app connects as `documind_app`, created **NOSUPERUSER NOBYPASSRLS**, while migrations run as a separate owner role. PostgreSQL ignores every RLS policy for a superuser *silently*, with the policies still listed in `pg_policies` — a deployment that connects as the owner has isolation that has never once worked and nothing about it looks wrong.

**Component responsibilities:**

| Component | Responsibility | Technology |
|---|---|---|
| Frontend | Project pipeline UI, Document Mapping, the template editor, review inbox, document review, dashboard, analytics and quality | React 19, TanStack Start/Router, Tailwind CSS v4, Zustand, TipTap, Recharts |
| Core API | Auth, CRUD, validation, orchestration | FastAPI, SQLAlchemy 2.x, Alembic |
| Database | System of record — every entity in the product, plus embeddings | PostgreSQL 17 + pgvector, row-level security |
| Cache / rate limiting | Token-bucket rate limiting, JWT revocation on logout, short-lived download grants | Redis 7 |
| Document parsing | DOCX structure parsing, colour-run classification, source extraction (PDF/XLSX/CSV/DOCX) | python-docx, lxml, PyMuPDF, pdfplumber, openpyxl |
| Retrieval | Tenant-filtered hybrid search: lexical + pgvector, merged and reranked; plus per-org mapping memory | scikit-learn TF-IDF + pgvector |
| LLM | Compiling templates, grounded generation | Anthropic / Gemini / OpenAI behind one provider interface. **No offline stub** — a missing key answers `503`, never invented output |

---

## 7. Technology Stack

**Frontend**

| Layer | Choice |
|---|---|
| Framework | React 19, TanStack Start (SSR) + TanStack Router (file-based routing) |
| Styling | Tailwind CSS v4 (CSS-first config), custom OKLCH design tokens, dark/light themes |
| State | Zustand (thin client over the real backend API) |
| Rich text | TipTap (draft editor + the colour-coded template token editor) |
| UI primitives | Radix UI (shadcn-style components) |
| Build | Vite 8, Bun |

**Backend**

| Layer | Choice |
|---|---|
| Language/Framework | Python 3.12, FastAPI |
| Database | PostgreSQL 17 + pgvector, SQLAlchemy 2.x ORM, Alembic migrations, row-level security. SQLite is dev/test only |
| Cache / rate limiting | Redis 7 (token-bucket limiter, session revocation, download grants) |
| Auth | JWT (bcrypt password hashing), Redis-backed logout revocation; partial RBAC incl. separation of duties on manifest approval |
| Document processing | python-docx, lxml (raw OOXML surgery), PyMuPDF, pdfplumber, openpyxl |
| LLM | Anthropic / Gemini / OpenAI behind one `LLMProvider` interface, with residency and zero-retention enforced at the boundary |
| Retrieval | Hybrid: scikit-learn TF-IDF + pgvector similarity, tenant-filtered before scoring |
| Testing | pytest — 47 modules incl. golden-DOCX fixtures per template family, RLS tests and a production-config guard |

---

## 8. Data Model / Database

The database is the single source of truth for every entity in the product — projects, templates (both kinds — see below), sources, manifests, bindings, generated documents and their versions, review tasks, embeddings, audit logs, and more: **39 tables**. Full DDL lives in `docs/BACKEND_SPEC.md` §5; the essential shape:

```mermaid
erDiagram
  ORGANIZATIONS ||--o{ USERS : employs
  ORGANIZATIONS ||--o{ PROJECTS : owns
  ORGANIZATIONS ||--o{ TEMPLATE_LIBRARY : owns
  PROJECTS ||--o{ TEMPLATE_FILES : has
  PROJECTS ||--o{ SOURCE_FILES : has
  PROJECTS ||--o{ GENERATION_JOBS : runs
  PROJECTS ||--o{ GENERATED_DOCUMENTS : yields
  TEMPLATE_FILES ||--o{ TEMPLATE_VERSIONS : versioned_as
  TEMPLATE_VERSIONS ||--o{ TEMPLATE_SECTIONS : parsed_into
  TEMPLATE_VERSIONS ||--o{ TEMPLATE_MANIFESTS : compiled_into
  SOURCE_FILES ||--o{ SOURCE_VERSIONS : versioned_as
  SOURCE_VERSIONS ||--o{ SOURCE_CHUNKS : chunked_into
  TEMPLATE_MANIFESTS ||--o{ MANIFEST_BINDINGS : bound_by
  SOURCE_VERSIONS ||--o{ MANIFEST_BINDINGS : feeds
  TEMPLATE_MANIFESTS ||--o{ MANIFEST_GENERATIONS : produces
  TEMPLATE_MANIFESTS ||--o{ REVIEW_TASKS : parks
  GENERATED_DOCUMENTS ||--o{ DOCUMENT_VERSIONS : versioned_as
  TEMPLATE_CLUSTERS ||--o{ TEMPLATE_CLUSTER_MEMBERS : groups
```

### 8.1 Two kinds of "template," on purpose

| | Template file (`template_files`) | Blueprint (`template_blueprints`) | ~~Template library~~ |
|---|---|---|---|
| Scope | One project | One project | Org-wide |
| Authored in | Microsoft Word (uploaded) | The template editor, or Word, or both | ~~DocuMind's token editor~~ |
| Understood via | Heading tree / jinja variables / colour-run manifest | The same colour-run manifest — a blueprint emits a real `.docx` | ~~Inline coloured tokens~~ |
| Used by | Document Mapping, via a compiled manifest | Publishes *into* the left-hand column: a template version and its manifest | ~~The Templates page~~ |

The third column is **retired**. Its editor produced HTML with `<span data-token>`
markers, and the fill engine works on OOXML runs addressed by position — so a
template authored that way could never fill a document, and `generateFromLibrary`
was never called by any screen. The rows are kept and readable, and
`POST /template-blueprints:from-library` migrates one into an editable template.

A **blueprint** is the answer to "I want to change this template", which the
product previously had no answer to. A legacy `.docx` is read into an editable
body, corrected, and published as a template version plus the manifest that fills
it — so authoring and the deterministic engine are the same path rather than two.

### 8.2 Core tables at a glance

| Table | Purpose |
|---|---|
| `organizations`, `users`, `roles` | Tenancy and identity |
| `projects` | The unit of work — region, function, document type, pipeline status |
| `template_files` / `template_versions` / `template_sections` | Uploaded DOCX templates and their parsed structure |
| `template_manifests` | Compiled field/condition/block rules for colour-coded templates ([§4.2](#42-template-compiler--universal-fill-engine-deterministic)) |
| `template_blueprints` / `template_blueprint_versions` | Templates being *written*: the editable body, the semantic objects over it, the findings against it, and where each version came from. Never mutated, so any earlier state can be forked back to |
| `template_clusters` / `template_cluster_members` | Bulk-onboarding template families ([§4.3](#43-bulk-onboarding)) |
| `source_files` / `source_versions` / `source_chunks` | Uploaded data files, chunked and indexed for retrieval |
| `manifest_bindings` | Which source column feeds which manifest field, for one (manifest × source version) pairing, plus the `value_map` that reconciles vocabulary ("FT" → "Full time") |
| `field_dictionary` / `mapping_memory` | The org's canonical field names, and the (field, column, transform) triples it has approved — with rejection counts, so a memory that only remembers acceptances cannot keep proposing the column a reviewer replaced |
| `embeddings` | pgvector store behind hybrid retrieval |
| `review_tasks` | Units the engine would not guess — a calculation needing sign-off, an ambiguous condition, a weak binding, poorly-grounded narrative — parked with enough context for a human to decide |
| `generation_jobs` / `section_outputs` | Generation runs, their progress and per-row results |
| `manifest_generations` | Audit record for one manifest-driven fill — field lineage, condition verdicts, QA result |
| `generated_documents` / `document_versions` | The output artifacts, fully versioned, each stamped with the `renderer` that produced it |
| `audit_logs` | Append-only record of every state-changing action |
| `org_data_policies` / `deletion_certificates` | Per-tenant retention and residency, and the record that accounts for a deletion |
| ~~`draft_documents` / `mappings`~~ | **Vestigial.** The storage behind the removed draft + mapping-wizard pipeline. Nothing writes to either table; the rows that exist are read-only history |

### 8.3 Design principles

- **JSONB for genuinely variable shape** (manifest fields/conditions/blocks, generation settings), real columns and indexes for everything queried or filtered on.
- **Soft deletes** everywhere a delete is reversible in spirit and referenced by immutable history.
- **Versioning over mutation** — templates, sources, library entries and documents are versioned rather than edited in place. (One deliberate exception: the editor's autosave mutates the current draft version, matching how people expect an editor to behave.)
- **Human-friendly display IDs** (`51255`, `50616`) generated via per-org counters, distinct from internal UUIDs.
- **Every row that can hold customer data carries `org_id`**, so a query filter is possible and row-level security has a column to key on. It is derived from the parent on write, never supplied by a caller — which would make it forgeable.

---

## 9. Project Structure

```
TemplateAI/
├── docker-compose.yml            # pgvector/pg17 + redis + migrate(owner role) + api(app role)
├── src/                          # Frontend (React + TanStack Start)
│   ├── routes/                   # File-based routes: login, dashboard, project pipeline,
│   │                             #   template editor, document editor, templates, review,
│   │                             #   chat, analytics, team, audit log, settings
│   ├── components/
│   │   ├── document-mapping.tsx  #   THE PRIMARY WORKFLOW: map columns -> generate
│   │   ├── app-shell.tsx, create-project-sheet.tsx
│   │   ├── template-editor.tsx   #   token UI for natively-authored templates
│   │   └── template-conversion-wizard.tsx, status-badge.tsx, ui/
│   └── lib/                      # api.ts (backend client), store.ts (Zustand), types.ts
├── backend/
│   ├── scripts/init-db/          # 01-app-role.sh — creates documind_app NOSUPERUSER NOBYPASSRLS
│   ├── tests/                    # pytest + golden DOCX fixtures per template family
│   ├── alembic/                  # Database migrations (incl. row-level security, pgvector)
│   └── app/
│       ├── main.py               # FastAPI app, middleware, router registration
│       ├── models.py             # SQLAlchemy models — the full data model (39 tables)
│       ├── tenancy.py            # RLS session scoping + LLM residency policy
│       ├── authz.py, ownership.py, security.py, rate_limit.py, downloads.py, retention.py
│       ├── routers/              # auth, projects, templates, sources, manifests, bindings,
│       │                         #   generation, review, chat, admin, metrics, downloads
│       ├── templates/            # ingest, semantic model, fingerprint, family matching,
│       │                         #   inheritance, parsers/{docx_parser,docx_prescan,docx_safety}
│       ├── compiler/             # rule_compiler, llm_compiler, mapping_agent (agentic loop),
│       │                         #   confidence (the AUTO_ACCEPT/CONFIRM/REVIEW/BLOCK bands)
│       ├── manifests/            # the manifest contract, validator, versioning, diff
│       ├── expressions/          # condition language, plain-English rendering, token parser
│       ├── retrieval/            # hybrid (lexical + pgvector), indexing, store, mapping memory
│       ├── generation/           # docx_renderer (the Universal Fill Engine), batch_runner,
│       │                         #   resolution_engine, narrative_engine, source ingestion,
│       │                         #   renderers, value formatting, pdf_fill
│       ├── qa/                   # placeholder / layout / overflow / lineage checks
│       ├── llm/                  # provider (Anthropic/Gemini/OpenAI), boundary, redaction
│       └── conventions/          # en/ja/ko/zh annotated locale rules
└── docs/
    ├── BACKEND_SPEC.md            # Full backend engineering specification
    └── TEMPLATE_COMPILER_RESEARCH.md  # Research behind the Template Compiler engine
```

> `backend/app/services/` no longer exists — it was a flat folder that became the module tree above. `APPLICATION_FLOW.md` §3 carries an old-path → new-path table if you are following a stale link.

---

## 10. Full Documentation Index

| Document | What's in it |
|---|---|
| **`README.md`** (this file) | The living project overview — use case, architecture, database, impact. Keep this current. |
| **`APPLICATION_FLOW.md`** | The self-contained technical briefing: what the code actually does today, layer by layer — repository map, data model, every flow, the full route inventory, and an explicit list of what was removed and why. Start here to understand the running system. |
| **`docs/BACKEND_SPEC.md`** | The backend engineering **specification**: the full database DDL, scalability/security/reliability design, the milestone roadmap, and a frontend-file-to-endpoint traceability table. Parts of it describe a target rather than the code; its "Implementation status" block at the top says which. |
| **`docs/TEMPLATE_COMPILER_RESEARCH.md`** | The research report behind the Template Compiler + Universal Fill Engine — grounded in a real analysis of a legacy Hospira/Pfizer HR template, including the RAG-vs-deterministic decision framework and the Azure/AWS service landscape for source extraction. |
| **`AGENTS.md`** | Notes for AI coding agents working in this repo (Lovable sync behavior). |
| API docs (running backend) | `http://localhost:8000/docs` — live OpenAPI/Swagger UI generated from the actual FastAPI routes. |

---

## 11. Getting Started

**Prerequisites**: Docker (recommended), or Python 3.12 + PostgreSQL 17 + Redis on the host. Node/Bun for the frontend either way.

### 11.1 The database is not optional, and it is not SQLite

`DATABASE_URL` still *defaults* to SQLite, and the test suite runs on it, but that is a development convenience only. SQLite cannot express row-level security, the `vector` column type that retrieval stores embeddings in, or a session timezone — all three of which have already hidden real defects in this codebase. `ENV=production` makes the process **refuse to start** on a non-PostgreSQL URL.

The stack also uses **two database roles**, and this is the part that is easy to skip:

- `documind_owner` runs migrations, because creating tables, enabling RLS and installing an extension all need privileges the application must not have.
- `documind_app` is what the API connects as: `NOSUPERUSER NOCREATEDB NOCREATEROLE **NOBYPASSRLS**`.

PostgreSQL ignores every row-level security policy for a superuser or a `BYPASSRLS` role — silently, with the policies still listed in `pg_policies`. Connect as the owner and tenant isolation has never once worked, and nothing about it looks wrong.

### 11.2 Fastest path: Docker Compose

`docker-compose.yml` brings up exactly those components: `pgvector/pgvector:pg17` (stock Postgres does not ship the extension, so the migration that creates it fails), Redis, a one-shot `migrate` service running as the owner, and the API running as the app role.

```sh
docker compose up -d db redis
docker compose run --rm migrate        # alembic upgrade head, as documind_owner
docker compose up api                  # uvicorn on :8000, as documind_app
```

Then create the first organisation and administrator — nothing is seeded on boot, so until this runs the database is genuinely empty and nobody can sign in:

```sh
docker compose exec api python -m app.bootstrap \
  --org "Your Organisation" \
  --email you@example.com \
  --name "Your Name" \
  --password 'choose-a-long-one'
```

```sh
# --- Frontend (second terminal, from repo root) ---
bun install
bun run dev
```

### 11.3 Running the backend on the host instead

Same two roles, same migration order:

```sh
brew install postgresql@17 redis        # macOS; use your package manager otherwise
brew services start postgresql@17
brew services start redis
createuser documind_owner -P
createdb documind -O documind_owner

# Creates the pgvector extension and the documind_app role (NOSUPERUSER NOBYPASSRLS).
# Docker runs this automatically via docker-entrypoint-initdb.d; on the host, run it yourself:
POSTGRES_USER=documind_owner POSTGRES_DB=documind APP_DB_PASSWORD='choose-another-one' \
  bash backend/scripts/init-db/01-app-role.sh

cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                    # set DATABASE_URL (documind_app), JWT_SECRET, CORS_ORIGINS
DATABASE_URL=postgresql+psycopg://documind_owner:...@localhost:5432/documind alembic upgrade head
python -m app.bootstrap --org "…" --email … --name "…" --password '…'

# Add at least one colleague. The separation-of-duties rules are unsatisfiable
# with a single account: the compiler may not sign off its own manifest, and an
# author may not close the review of their own letter.
python -m app.bootstrap add-user --org "…" --email … --name "…" \
  --role approver --password '…'

uvicorn app.main:app --reload --port 8000
```

`app/config.py` reads `.env` relative to the process working directory, so uvicorn must start from `backend/`.

`GET /readyz` reports whether Postgres and Redis are actually connected; `GET /healthz` is the static probe. Interactive API docs at `http://localhost:8000/docs`.

### 11.4 First run, end to end

Sign in at `/login` with the account you just created. There is no demo data and no seeded user: every project, template, source file and document in a running instance was put there by someone using it, which is what makes a green dashboard evidence that the application works.

Then, on one project:

1. **Stage 1 — Template.** Upload a `.docx`. A legacy colour-coded template (blue placeholder runs, red instructions, `MERGEFIELD` codes) is the case this was built for.
2. **Stage 2 — Sources.** Upload the `.csv`/`.xlsx` whose rows will become documents.
3. **Stage 3 — Document Mapping.** Map fields to columns, then generate. Two steps, because the template was already read at upload and generating from it does not wait on a signature. Anything the compiler was unsure about is listed beside the Generate button as advice — those are the places a document is most likely to come back with a QA failure — rather than as a gate in front of it.
4. **Stage 4 — Documents.** Where the batch's progress and its failures are shown, and where each letter is worked through a lane — work in progress, completed, approved, blocked, cancelled. **Only an approved document can be downloaded**, individually or as a ZIP.

There is no separate Template Studio. There was, reached from "Open in Studio" on a template row, and it offered the same compile/review/bind/generate steps a second time — two screens with the same name doing overlapping jobs. Templates are read at upload; the words of a template are edited in the template editor at `/templates/$blueprintId`.

### 11.5 Language models

A model is required for **compiling** templates that have no colour coding, and for chat, narrative sections, and fuzzy or context-dependent conditions. Colour, brackets and `MERGEFIELD`s are language-independent structure, so a coloured template — even in German or Korean — compiles on the free rule-based path, and **generation never calls a model at all**. Three vendors are supported:

```sh
LLM_PROVIDER=anthropic        # or: gemini, openai
# LLM_COMPILE_PROVIDER=gemini # optional — compile on one vendor, generate on another

ANTHROPIC_API_KEY=...         # needed when a provider above is "anthropic"
GEMINI_API_KEY=...            # needed when a provider above is "gemini"
OPENAI_API_KEY=...            # needed when a provider above is "openai"
```

`LLM_COMPILE_PROVIDER` exists because the two jobs have opposite economics: compiling a template family happens once and is worth the strongest model available, while per-document generation runs forever and wants the cheapest one that is good enough. A provider name that isn't `anthropic`, `gemini` or `openai` is rejected rather than defaulted — a typo must not silently route every document through a vendor nobody chose.

All three vendors sit behind the same `LLMProvider` interface in `app/llm/provider.py`, with the response *shape* constrained by a strict schema at the API level rather than asked for in prose. OpenAI uses the Responses API with strict `json_schema` structured outputs; the compile schemas already satisfy that subset, so no schema is weakened to fit it.

Two more settings are assertions about a contract, not preferences, so both default to the weakest claim and an organisation requiring more than the deployment offers gets a refusal rather than a prompt:

```sh
LLM_RESIDENCY=GLOBAL          # GLOBAL | EU | UK | IN — where the configured deployment runs
LLM_ZERO_RETENTION=false      # confirm contractually before setting true
```

There is no offline stub. Without a usable key those endpoints answer `503 LLM_NOT_CONFIGURED` instead of returning invented output. The deterministic path (pre-scan → manifest → fill → QA), which is how colour-coded templates become documents, makes no model call at all and needs no key.

---

## 12. Security & Compliance Posture

- **Auth**: JWT (bcrypt-hashed passwords), Redis-backed token revocation on logout, a stale or revoked token is detected on the next request, cleared, and the user returned to `/login`.
- **Rate limiting**: Redis-backed token-bucket limiter on every authenticated endpoint, plus a separate IP-based limiter on login to blunt brute force.
- **Audit trail**: append-only `audit_logs` table recording every state-changing action — who, what, when, from where.
- **Grounded generation**: every LLM-generated sentence in the RAG engine must cite the source chunk it came from; ungrounded claims are treated as failures, not warnings.
- **Deterministic filling**: the Template Compiler path makes zero LLM calls at generation time for data-driven templates, eliminating hallucination risk for those documents entirely.
- **QA gates**: a generated document from the Template Compiler path is checked for leftover placeholder brackets, leftover `MERGEFIELD` codes, and leftover instruction text before it's considered valid.
- **Approval is a real gate**: an approved manifest is immutable and subject to separation of duties; a manifest with an unrenderable condition or an unanswered compiler warning cannot be approved; a QA-blocked document cannot be approved; and a document that has been approved cannot be deleted until the approval is revoked, which is itself auditable.
- **Tenant isolation is enforced twice**: per-handler `org_id` checks, and PostgreSQL row-level security keyed on a per-session setting, with the application connecting as a role that cannot bypass a policy even by accident.
- **Data residency and retention**: per-organisation retention schedules with a sweep, a residency/zero-retention policy checked at the model boundary *before* a prompt is built, tenant offboarding, and deletion certificates that account for what was destroyed.
- **Regulated-industry features (in progress)**: document versioning with full history is live; e-signature capture and section-anchored review comments remain design targets (see `docs/BACKEND_SPEC.md` §13).

---

## 13. Current Status & Roadmap

**Working today**: the full frontend — sign-in, dashboard, the four-stage project pipeline with **Document Mapping** as its centre, the template editor, the document editor, a unified review inbox (documents somebody objected to alongside the values the engine parked), chat, analytics with real token and USD figures, a quality screen carrying §22's metrics and §18's timing targets, team and audit log — wired to a real backend running on PostgreSQL 17 + pgvector and Redis, with row-level security enforced by a non-bypassing application role. The deterministic read → bind → generate path is complete end to end — a template is compiled when it is uploaded, and generating from it needs no approval step: what the engine checks is that the reading is usable and current, not that somebody signed it. The one exception is a template flagged legally binding, where §16's four-eyes rule still applies. Also complete: plain-English condition review, confidence-banded binding suggestions, batch generation with a canary gate, and QA gates that block a document rather than shipping one with a placeholder still in it.

**Removed, deliberately**: the earlier "draft + mapping wizard" pipeline and the section-mapping RAG generator behind it. It had no manifest, so it filled nothing, and it stood in front of the path that works. Every `/drafts/…` endpoint went with it.

**Known simplifications** (see `docs/BACKEND_SPEC.md`'s "Implementation status" table for the full list): batch generation runs on an in-process background task rather than a durable job queue, so there is no retry, cancellation or survival across a restart; RBAC is enforced on manifest approval, document approval and review, user management and audit reads but not yet across every endpoint; there is no endpoint that creates a user, so colleagues are added from the command line; auth is JWT-only rather than enterprise SSO/OIDC. Each is a documented, deliberate scoping choice with a clear upgrade path, not an oversight.

**Natural next steps**: a background job queue for generation at higher volume; screens for the pieces that are built but still API-only (bulk onboarding and clustering, manifest inheritance, manifest diff, the admin data-policy and deletion-certificate surfaces); and OIDC/SSO for enterprise auth.

---

## 14. Origins

This project began as a Lovable-scaffolded frontend (`docucraft-ai`) and has since grown a complete, independent FastAPI backend, a deterministic template-compilation engine, and this documentation set. The Lovable sync notes in `AGENTS.md` still apply to the frontend half of the repo.

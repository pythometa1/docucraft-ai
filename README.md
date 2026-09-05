# DocuMind AI

**Upload a Word template and a spreadsheet. Get back one correct, audited document per row — with no language model anywhere near the letters themselves.**

![status](https://img.shields.io/badge/status-active%20development-brightgreen)
![frontend](https://img.shields.io/badge/frontend-React%2019%20%2F%20TanStack%20Start-blue)
![backend](https://img.shields.io/badge/backend-FastAPI%20%2F%20PostgreSQL%2017%20%2F%20Redis-blue)
![tests](https://img.shields.io/badge/tests-2%2C266%20%C2%B7%2088%25%20coverage-success)
![license](https://img.shields.io/badge/license-proprietary-lightgrey)

> **This file is the canonical overview of the project and is written against the code, not against a plan.** Every number, gate name, endpoint count and limit below was read out of the source at the time of writing. Where something is built but not wired up, [§12](#12-what-is-wired-and-what-is-a-seam) says so by name — a README that only lists what works is a sales page, and the first person it misleads is the next contributor.

---

## Table of Contents

1. [What this is](#1-what-this-is)
2. [The problem](#2-the-problem)
3. [How it works, end to end](#3-how-it-works-end-to-end)
4. [The compile → fill pipeline](#4-the-compile--fill-pipeline)
5. [Quality gates](#5-quality-gates)
6. [Template authoring and bulk onboarding](#6-template-authoring-and-bulk-onboarding)
7. [Mapping, review and the human loop](#7-mapping-review-and-the-human-loop)
8. [Retrieval, chat and the model boundary](#8-retrieval-chat-and-the-model-boundary)
9. [Security, tenancy and compliance](#9-security-tenancy-and-compliance)
10. [Architecture and stack](#10-architecture-and-stack)
11. [Data model](#11-data-model)
12. [What is wired, and what is a seam](#12-what-is-wired-and-what-is-a-seam)
13. [Getting started](#13-getting-started)
14. [Tests and CI](#14-tests-and-ci)
15. [Project structure](#15-project-structure)
16. [Documentation index](#16-documentation-index)

---

## 1. What this is

DocuMind AI is a document-generation platform for regulated, template-heavy work — HR, clinical research, quality/CMC, medical affairs, legal. You give it a **template** (the structure, styles and legal boilerplate that must not change) and **source data** (the values that fill it in). It reads the template once into a machine-executable **manifest**, you bind that manifest's fields to spreadsheet columns, and it produces one document per row by *editing a copy of your original file in place* — so the layout is never rebuilt and therefore never at risk.

The central design decision: **a model reads the template once; nothing reads the data.** Compiling a template is hard language understanding and happens once per template family. Filling it is deterministic OOXML surgery that runs per document, makes zero model calls, costs nothing but CPU, and cannot hallucinate a salary figure.

It is a running application, not a prototype: 148 HTTP endpoints across a FastAPI backend on PostgreSQL 17 + pgvector and Redis, a 17-route React 19 frontend, 48 database tables, 45 of them under row-level security, and a 2,266-test suite behind a coverage gate CI runs verbatim.

It also ships its first per-industry **service**: invoice generation. Describe your business, a model authors the invoice template (proven by the same emit round-trip every template passes, with shipped kits standing in when no model is configured), type the line items, and a numbered invoice comes back as DOCX or PDF - customer book, org-scoped INV-#### sequences and server-side Decimal money included. The primitive underneath it, the TABLE_ROW (one template row rendered once per record of a collection), is the same one clinical listings and batch records will need.

---

## 2. The problem

Someone opens a Word template. The author left instructions behind in it — often literally colour-coded: blue runs are values to substitute (`<Colleague First Name>`), red runs are directions to the human processor (*"Include the following section only if the Colleague Type is Fixed Term"*). They copy values across from a spreadsheet by hand, delete the sections that don't apply, delete the instructions themselves, and save. Thousands of times a month, per team, across an industry holding **lakhs of distinct templates**, each encoding its rules informally, for a human reader.

| Function | Documents | What gets automated |
|---|---|---|
| **Human Resources** | Offer letters, termination letters, promotion memos | Conditional clauses by employment type, salary tables, compliance boilerplate |
| **Clinical Research** | Study reports, protocol amendments, consent forms | Long narrative sections grounded in study data, ICH-standard structure |
| **Quality / CMC** | Batch records, deviation reports, ICH M4Q sections | Structured technical content from lab and quality systems |
| **Medical Affairs** | Medical letters, publication summaries | Mixed narrative and data-driven content |
| **Legal / Marketing** | MSAs, vendor agreements, product briefs | Boilerplate with data-specific clauses |

**The acceptance case.** A real Hospira Australia (Pfizer) HR offer letter: 207 paragraphs, hand-coloured runs, twelve legacy `MERGEFIELD` instructions resolving to seven distinct codes, six of them inside two remuneration tables. Alongside it, two ICC employment-contract masters. These three files are customer-owned and deliberately **not** in this repository — which is why parts of the test suite skip on a fresh checkout ([§14](#14-tests-and-ci)).

---

## 3. How it works, end to end

```mermaid
flowchart LR
  A[Upload .docx] -->|compiles in the same act| B[Manifest:<br/>fields · conditions · blocks]
  B --> C[Download the spreadsheet<br/>the manifest asks for]
  C --> D[Bind fields to columns<br/>ranked, confidence-banded]
  D --> E[3 canary rows<br/>rendered + QA'd first]
  E -->|pass| F[Full batch:<br/>one .docx per row]
  E -->|fail| G[Batch refuses to run]
  F --> H[16 QA gates<br/>on every document]
  H --> I[Review → approve → download]
```

The project screen is a **four-stage rail**: **Template → Sources → Document Mapping → Documents**.

1. **Template.** Upload a `.docx`. The zip archive is inspected for symlinks, path traversal and decompression ratios *before any parser opens it*, then it is pre-scanned and compiled into a manifest in the same act — with live staged progress showing which work is running and which of it costs money.
2. **Sources.** Upload the `.csv`/`.xlsx` whose rows become documents — or download the spreadsheet the manifest asks for, which ships one column per bindable field, headed by the field id itself, with Excel dropdowns offering exactly the strings each equality condition tests.
3. **Document Mapping.** Two steps: bind fields to columns, then generate. Not four — compile happens at upload, and generating does not wait on a signature.
4. **Documents.** Batch progress, per-row failures, and each letter worked through a lane. **Only an approved document can be downloaded**, enforced on all five egress paths including the unauthenticated grant handler and the bulk zip.

**Approval is not a precondition for generating, on purpose.** It used to be, and it was unmeetable: manifest validation turns every unacknowledged compiler warning into a failure, and a freshly compiled template has warnings and no acknowledgements by construction. So the gate did not mean *"somebody looked at this"* — it meant *"acknowledge each warning in writing, sign, then generate"*, on every template. Both generate endpoints now ask whether the reading is **usable and current**: `MANIFEST_NOT_READ` for a failed compile, `MANIFEST_RETIRED` for one a newer compile superseded, and `MANIFEST_NOT_APPROVED` only where a template is flagged legally binding and the four-eyes rule applies.

---

## 4. The compile → fill pipeline

### 4.1 Pre-scan — deterministic, no model

`prescan()` flattens the document body into document-order paragraphs and, for each, mechanically inventories:

- **Run colour and highlight roles** — which runs are placeholders, which are instructions — with adjacent same-role runs merged into spans.
- **`MERGEFIELD` sequences**, walked as a complex-field state machine (`fldChar begin → instrText → separate → cached result → end`).
- **Hyperlinks, tables, and a stable `(paragraph_index, span_index)` coordinate** for every span.

It never interprets meaning. Every coordinate the rest of the system uses is minted here, and `docx_prescan.py` is held at a **100% coverage floor** because a manifest whose coordinates are off by one addresses the wrong text for the rest of the file.

### 4.2 Compile — one agentic loop

`agentic_compiler.compile_template()` is the live compile path for every upload. It runs **chunk → write → reconcile → ground → assert → review**, repeating until a deterministic assertion set stops objecting, two rounds pass without the fault count falling to a new low, or the round budget (12) runs out.

- **Chunking** splits the template into 40,000-character windows with **8 paragraphs of overlap** — a conditional block straddles boundaries, and a writer that sees only the governed clause has no reason to make it conditional. Every chunk of a split template carries a whole-document instruction outline — up to 200 instruction-shaped lines — in front of it; a template that fits in one chunk is sent without one.
- **Grounding** re-locates every claim the model makes inside a real pre-scan span. Anything that cannot be found is **dropped, not guessed**.
- **Assertions** are the loop's only objective signal: nine mechanical checks — `uncovered_placeholder`, `uncovered_mergefield`, `surviving_instruction`, `paragraph_scoped_switch`, `unexecutable_condition`, `orphaned_field`, `field_without_slot`, `ungoverned_block`, `test_fill_failure`. Faults force another round; warnings ride along on the manifest because no further round can fix them.
- **Eight compiler warning codes** (`W-HL-GAP`, `W-MARKER-PARSE`, `W-MIXED-SYNTAX`, `W-FIELD-CODE`, `W-DUP-STATIC`, `W-NESTED-COND`, `W-UNSLOTTED-FIELD`, `W-SPLIT-PLACEHOLDER`) are raised per paragraph with evidence, and block auto-approval.

A full **rule-based compiler** also exists, built on the red/blue colour convention and per-language YAML grammars. On the live path it now runs only as a diagnostic — `rules_would_have_fallen_short`, recorded on the compile transcript, answering *"should this template have needed a model at all?"* It is not a fallback the product depends on.

> `ungoverned_block` is the newest assertion and worth the paragraph. The renderer drops a block only when a condition that *keeps* it decides False — so a block no condition references can never be dropped, and both halves of an `[[IF]] … [[ELSE]]` print, one contradicting the other. The rule compiler cannot produce this; the agentic path can, and did: eight blocks, three conditions, and all five ungoverned ones were the positive arms.

### 4.3 Fill — deterministic OOXML surgery

`fill_template()` opens a **copy of the approved template** and edits it. It never rebuilds a document.

| Mechanism | What happens |
|---|---|
| **Three-state conditions** | A verdict is `True`, `False` or `None`. Undecided is not false: it **blocks the document**, and never silently drops a section from someone's letter. |
| **Block keep/drop at three granularities** | A paragraph, a table row (`w:tr`, with the table removed once it has no rows left), or an inline span switch — two branches on one line. |
| **`MERGEFIELD` resolution** | The whole complex-field run sequence is replaced by the formatted value, so a surviving `instrText` is by construction one the fill never reached. |
| **Styling by construction** | Runs are edited rather than rebuilt, so fonts and sizes are inherited rather than reapplied. The only styling the fill touches is the author's own markup — the placeholder run's colour is stripped and highlight markup is swept, so a value never prints in the annotation's blue. |
| **Four missing-value policies** | `BLOCK`, `BLANK`, `DEFAULT`, `REMOVE_SENTENCE`, declared per field on an authored blueprint. A field that declares nothing falls back to `BLOCK` when it is marked required and `BLANK` when it is not — which is what compiled manifests get today, because the compiler emits no `on_missing`. `REMOVE_SENTENCE` plants Private Use Area markers at fill time and sweeps the sentence afterwards. |
| **Locale-aware formatting** | Currency, date, number and percentage formatting via babel, keyed on the field's declared type and the project's locale. |
| **Byte-for-byte reproducibility** | `normalise_docx()` rewrites the archive with a fixed 1980-01-01 timestamp, DEFLATE level 9 and a stable entry order, so the same manifest and row produce the same bytes. |

Five **resolution unit kinds** — `static`, `computed`, `conditional`, `narrative`, `human` — are resolved in topological dependency order. Computed fields evaluate through a whitelisted Python AST (`safe_eval_formula`), not `eval`.

**A batch does not run blind.** `run_batch` renders **3 canary rows spread evenly across the batch** — not taken from the front, because a spreadsheet arrives sorted and the first three rows are usually the same department and the same branch of every condition. If a canary fails QA, the remaining rows are never attempted. Progress is rewritten and committed to the job row after every row.

### 4.4 PDF

Immutable-PDF templates take a parallel path: `render_overlay` deletes the show-text operators inside an approved bounding box and appends a mask rectangle plus one text operator, passing every other operator through untouched. Content-stream operators are constructed by hand over pypdf — there is no PDF-writing library in the dependency set — which is why the overlay path is limited to Helvetica/WinAnsi.

DOCX→PDF conversion shells out to headless LibreOffice with a 120-second timeout and a private user profile per run, **and then reads its own output back**. A missing font does not make LibreOffice fail — it makes it substitute, so a Chinese letter converts to a PDF of its Latin fragments with exit code zero. If fewer than half the distinct source CJK characters survive, the renderer refuses rather than hand over a wrong document that looks like a right one.

---

## 5. Quality gates

Sixteen named checks live in one registry (`app/qa/policy.py`), each with a stable code, a default severity and an enabled flag. **Fifteen are on by default; fourteen block, one warns.** Gates run against the file that was actually **saved and archive-normalised**, re-opened and re-read — not against the in-memory tree the renderer believed it wrote.

| Check | Catches |
|---|---|
| `placeholder_remains` | A bracket placeholder survived the fill and is legible to the reader |
| `unresolved_mergefield` | A Word `MERGEFIELD` was never resolved — read from `w:instrText`, not from visible text, which Word never displays |
| `control_token_remains` | `[[IF]]` / `[[ELSE]]` / `[[ENDIF]]` printed in the output |
| `instruction_text_remains` | Prose addressed to the assembler leaked into the letter |
| `fill_mask_remains` | A slot **drawn** rather than bracketed — `xxxx年xx月xx日`, `xx个月`, `xx/xx/xxxx` — that the fill never replaced |
| `date_part_malformed` | A whole date written into a slot holding one part of one: `2026-09-01年2026-09-01月2026-09-01` |
| `required_value_missing` | A `BLOCK`-policy field resolved to nothing |
| `branch_selection` | A mutually exclusive branch set did not resolve to exactly one branch |
| `static_region_changed` | A package part the engine must not touch differs from the template — `word/document.xml` is the *only* editable part |
| `resolved_value_absent` | A field resolved to a real value that is nowhere in the finished document |
| `value_format_doubled` | A unit or currency printed twice: *"$AUD 76,800 per annum per annum"* |
| `doubled_word` | A word repeated adjacently (the one **warning**-severity check) |
| `orphaned_field` | A field whose every slot sits in a paragraph marked for unconditional deletion |
| `value_exceeds_max_len` | A value longer than the manifest's declared `max_len` |
| `value_overflows_cell` | Text estimated not to fit the declared table-cell width (**opt-in**, and an estimate — it cannot see auto-width cells, vertical overflow or real font metrics) |
| `value_overflows_region` | A value wider than the approved PDF region (PDF overlay path only) |

Two properties are deliberate. **There is no "off".** The severity vocabulary offers `blocking` or `warning` — a human still sees a warning — because a gate people can switch off protects nothing. And **an unknown or contradictory check name raises at resolution time** rather than being ignored, as does a finding produced for a check the policy did not enable, so a silently-skipped gate is impossible.

`fill_mask_remains`, `date_part_malformed` and `ungoverned_block` came from running 21 real templates end to end and *reading the letters that came out* rather than trusting that a green run meant a correct one. Every gate that existed before them asked what was **left over** or what was **absent**. These three are neither — they are present and should not be, or absent and should not be — and that asymmetry is why they were invisible. One eleven-template set contained 395 unfilled masks. Underscore runs are deliberately *not* matched: a signature line signed in ink is not an unfilled slot, and one contract had 294 of those.

---

## 6. Template authoring and bulk onboarding

### 6.1 Blueprints — the editable half of a template

A compiled manifest cannot be edited back into a document (a static object carries a `text_hash`, not text). So authoring has its own model: a **blueprint** is a JSON body of paragraphs, segments and tables stored beside its semantic objects, versioned append-only.

The whole design rests on one round-trip property, **asserted on every emit rather than only in tests**:

```
blueprint → emit → prescan → compile → blueprint′   must equal   blueprint
```

An authored template is therefore indistinguishable from a well-formed legacy one, and re-enters the same pre-scanner, compiler, fill engine and QA gates with no special case.

- **One segment means exactly one span** — the invariant that makes `(paragraph_index, span_index)` coordinates addressable, and the reason normalisation exists (Word runs do not map one-to-one onto them).
- **11 typed operations** (`set_segment_text`, `set_segment_role`, `set_segment_emit`, `add_field`, `remove_field`, `rename_field`, `retype_field`, `set_on_missing`, `rewrite_condition`, `remove_condition`, `set_block_range`) are the validated vocabulary for changing a blueprint: the editor's *Apply fix*, an API caller and the co-pilot all post the same payload to the operations endpoint. A plain Save still posts a whole revised body, which is normalised but applies no operations.
- **The co-pilot proposes operations and never holds the pen.** It has an explicit `author` / `explain` mode; in explain mode it answers with paragraph references and writes nothing.
- **Two emit modes.** `emit()` rebuilds from scratch. `emit_from_base()` publishes a customer's own file by editing the package — and refuses any structural change, because the operation vocabulary that could safely add or remove paragraphs in a real customer package does not exist yet.
- **Linting** has three severities (blocking / warning / advisory) and *calls* the approval-time validator rather than restating it, so the two cannot drift.
- **Five starter kits** (`blank`, `offer`, `contract`, `clinical`, `medaff`) and **four convention families** as YAML, not code: `en`, `ja`, `ko`, `zh`. Colour, brackets and `MERGEFIELD`s are language-independent structure.

### 6.2 Conditions in plain English

Conditions are written in **`documind-expr/1.0`**, a restricted-AST dialect with 8 comparison operators, 3 connectives, and 15 Python AST node classes rejected by name. The reviewer-facing sentence — *"Keep when the colleague type is Fixed Term"* — is **rendered from the same expression tree the evaluator runs**, never from a description written alongside it. A hand-written description drifts from its expression the first time the expression is edited, and a drifted description is a false statement about what was approved. An expression that cannot be rendered is shown as **broken**, not omitted: it is the one that must not be approved, so it must not look like a blank.

### 6.3 Bulk onboarding — the cost lever

`POST /projects/{id}/templates:bulk-onboard` takes an estate of `.docx` files at once, and clusters them by **structural fingerprint** — MERGEFIELD codes (0.5), structure (0.3: a paragraph-count band, table size and the blue/red run counts) and bracket tokens (0.2) — then compiles **exactly one representative per family**.

> This replaced TF-IDF-over-full-text clustering, and the older docs still describe the TF-IDF version. The fingerprint is language-independent, which the text-similarity approach was not.

Family inheritance then decides, per new template, whether to **reuse** an approved manifest (structural similarity ≥ 0.90), use one as **targeted-review evidence** (≥ 0.60), or **start a new family**. This is what makes onboarding lakhs of real templates a compile-per-*family* problem rather than a lakh-sized manual re-authoring project.

---

## 7. Mapping, review and the human loop

**Binding suggestions are scored, not guessed.** Seven weighted independent signals combine as `1 − Π(1 − sᵢwᵢ)` into four bands — **AUTO_ACCEPT** (≥ 0.97), **CONFIRM** (≥ 0.80), **REVIEW** (≥ 0.50), **BLOCK** — with four hard vetoes, a 0.05 ambiguity margin between the top two candidates, and a historical-approvals signal that saturates as `1 − e^(−n/8)` so a memory cannot be bullied by volume.

**Mapping memory** records the `(field, column, transform)` triples reviewers approved *and rejected*, with rejection counts — a memory that only remembers acceptances would keep proposing the column a reviewer just replaced.

**Two review queues, one inbox.** `/review` merges *document reviews* (a person objected to a letter) with *unit tasks* (the engine would not guess: a calculation needing sign-off, an ambiguous condition, a weak binding, poorly-grounded narrative). Resolving a unit task can **promote the decision back into the manifest** — in `formula` mode it rewrites the field to `computed` with the human's expression; in `condition_expression` mode it rewrites the expression and downgrades the condition from a judgement call to `exact`. A rationale is mandatory and is appended to the generation's field lineage with `source: "human"`, alongside everything the machine resolved. It refuses on an approved manifest, which is immutable.

**Documents carry two independent status axes, with one writer each.** `workflow_status` stores only the three states a person can assert (work in progress / completed / cancelled); `approved` and `blocked` are derived on read, because a signature is the approve endpoint's to record and a QA verdict is the fill engine's. Each column has exactly one writer, so they cannot fight.

---

## 8. Retrieval, chat and the model boundary

Retrieval is **hybrid and tenant-filtered before anything is scored** — never after. Lexical TF-IDF (weight 0.45) and vector similarity (0.55) are merged with a +0.10 agreement bonus, capped at 1.0.

What gets embedded is deliberately narrow: **source column *descriptions*** (one vector per column, with a synthetic sample value — never a customer row) and **template field context** (the sentence a placeholder sits in). Embeddings are 1024-dimensional from `HashingEmbedder`, a signed feature-hashing vectoriser over word unigrams plus 3- and 4-character n-grams that makes no network call; a hosted provider drops in behind the `EmbeddingProvider` protocol, and the index refuses to score across two providers. Storage is pgvector `vector(1024)` on PostgreSQL and a JSON float array on SQLite, through one `TypeDecorator`.

This evidence is injected into the compiler's prompt as advisory *"columns that already exist in this organisation's source data"*, so a field gets named after a column the tenant already has.

**Three vendors sit behind one provider interface** — Anthropic (`claude-opus-5` compile / `claude-sonnet-5` generate), Gemini (`gemini-2.5-pro` / `gemini-3.6-flash`) and OpenAI (`gpt-5` / `gpt-5-mini`). `LLM_COMPILE_PROVIDER` can name a *different vendor* from `LLM_PROVIDER`, because the two jobs have opposite economics: compiling a family happens once and is worth the strongest model available, while per-document generation runs forever and wants the cheapest one that is good enough. When they differ, both are wrapped in a router that dispatches on the `purpose` already threaded through every call. Response *shape* is constrained by a strict schema at the API level rather than asked for in prose.

**The boundary is a refusal, not a filter.** `get_llm_provider` is the unskippable choke point — it will not hand back a provider unless the caller names the tenant, and it checks the tenant's residency and zero-retention requirements there against what the configured deployment actually offers. It was moved to the factory precisely because the earlier gate lived in `prepare_context`, whose only caller is the chat route, so five of the six paths that reach a model skipped it. `prepare_context` still applies that check plus redaction to source-derived context. Sensitive values are redacted into deterministic, one-way, type-preserving synthetic samples. There is **no offline stub** — a missing key answers `503 LLM_NOT_CONFIGURED` rather than returning invented output. The deterministic fill path needs no key at all.

Generation is schema-constrained to blocks carrying citations, and **any chunk id the model invents is filtered out** against the ids actually sent. Every call is metered into an `llm_calls` row and costed in **integer micro-dollars** against a dated vendor rate table, by a wrapper that survives handlers which never commit.

---

## 9. Security, tenancy and compliance

- **Auth**: bcrypt password hashing, HS256 JWT carrying `sub` and `org_id`, 8-hour lifetime, revoked through a Redis blacklist on logout.
- **Tenant isolation is enforced twice.** Every handler goes through one of twelve `owned_*` guards that return the **same 404 for "not yours" as for "not there"** — a 403 confirms the resource exists to someone who should not know that. Underneath, PostgreSQL row-level security covers **42 of 45 tables** with an `org_isolation` policy keyed on the `app.current_org` session GUC — re-asserted on every transaction begin — plus an `rls_maintenance` policy keyed on the separate `app.rls_bypass` GUC.
- **A superuser silently ignores every RLS policy** — with the policies still listed in `pg_policies` and `FORCE` still set. So the app connects as `documind_app` (`NOSUPERUSER NOCREATEROLE NOBYPASSRLS`) while migrations run as a separate owner, and a **startup check refuses to serve** in production if the database role can bypass RLS. A deployment that connects as the owner has isolation that has never once worked, and nothing about it looks wrong.
- **A meta-test forces every new endpoint to be classified.** It reads the route table at test time and fails if any parametrized route appears in neither the tenancy walker's coverage table nor its exemption list. There is exactly one exemption — `GET /downloads/{token}` — carrying a written justification.
- **Rate limiting**: a Lua token bucket in Redis, 300 req/min sustained with a 60-request burst per organisation, plus 10/min per IP on login and download redemption.
- **Downloads**: 120-second, single-use, Redis-backed grants, audited on both mint and redemption. Only approved documents are downloadable, on every egress path.
- **Capabilities**: 10 constants across 8 roles. Four-eyes approval bites on legally-binding templates; an author can **never** close the review of their own document (`SELF_REVIEW_REFUSED`), unconditionally.
- **Audit**: an append-only log across 49 call sites and ~44 event labels — who, what, when, from where.
- **Retention and deletion**: per-tenant retention (sources default 30 days), residency and zero-retention policy; a deletion cascade computed by **walking the schema** rather than a hand-maintained list; SHA-256 deletion certificates whose id manifest is deliberately never stored; and tenant offboarding that reaches blobs, embeddings and mapping memory. `retention.py` and `downloads.py` are both held at a **100% coverage floor** — an untested branch there is a row a customer was told had been destroyed.
- **Upload safety**: `.docx` archives are inspected from the central directory only — never decompressed — for symlinks, path traversal, decompression ratio and entry count, before any parser opens the file.
- **Prompt injection posture**: the shared system prompt instructs the model to treat everything inside `<context>` as untrusted retrieved data.
- **Production config guard**: `ENV` in `{production, prod, staging}` makes the process **refuse to boot** on a SQLite URL, a default JWT secret, or wildcard CORS.

---

## 10. Architecture and stack

```mermaid
flowchart TB
  FE[React 19 · TanStack Start<br/>15 routes] -->|HTTPS/JSON · 133 endpoints| API[FastAPI · 14 routers]
  API --> PG[(PostgreSQL 17 + pgvector<br/>45 tables · RLS · Alembic)]
  API --> REDIS[(Redis 7<br/>rate limit · revocation · grants)]
  API --> STORE[(Local filesystem<br/>templates · sources · outputs)]
  API --> TPL[Pre-scan → Compile → Fill → QA<br/>NO model at generation time]
  API --> LLM[Anthropic / Gemini / OpenAI<br/>behind one provider + boundary]
  API --> RAG[Hybrid retrieval<br/>lexical + vector, tenant-filtered]
```

**Modular monolith, on purpose.** One FastAPI application with bounded modules — `templates`, `compiler`, `manifests`, `expressions`, `retrieval`, `generation`, `qa`, `llm`, `audit`. The domain is a single linear pipeline; splitting it into services today would add operational overhead without solving a scaling problem the project has. Complexity is isolated in the heavy work (parsing, compiling, DOCX surgery), not at service boundaries.

| Layer | Choice |
|---|---|
| **Frontend** | React 19.2, TanStack Start 1.168 + Router 1.170 (file-based, generated route tree), Tailwind CSS v4 (oklch tokens), Radix/shadcn primitives (17), Zustand, TipTap, Recharts, framer-motion, cmdk, sonner, lucide |
| **Build** | Bun + Vite 8 through `@lovable.dev/vite-tanstack-config`; `bun run build` emits a **Cloudflare Worker bundle via Nitro**, not a static `dist/` |
| **Backend** | Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, pydantic-settings |
| **Database** | PostgreSQL 17 + pgvector, row-level security. SQLite is dev/test only |
| **Cache** | Redis 7 — token bucket, JWT revocation, download grants |
| **Documents** | python-docx, lxml (raw OOXML surgery), pypdf, openpyxl, babel, beautifulsoup4 |
| **Retrieval** | scikit-learn TF-IDF + a local hashing embedder, pgvector storage |
| **Testing** | pytest — 89 modules, 2,266 tests, golden-DOCX comparison by C14N canonicalisation |

---

## 11. Data model

**45 tables in one declarative module, 20 Alembic revisions on a single head.** The schema is versioned-immutable by design: templates, sources, manifests, blueprints and documents each have a parent row plus an append-only `*_versions` child, so a signature or an approved manifest always names bytes that still exist.

```mermaid
erDiagram
  ORGANIZATIONS ||--o{ USERS : employs
  ORGANIZATIONS ||--o{ PROJECTS : owns
  PROJECTS ||--o{ TEMPLATE_FILES : has
  PROJECTS ||--o{ SOURCE_FILES : has
  PROJECTS ||--o{ GENERATION_JOBS : runs
  TEMPLATE_FILES ||--o{ TEMPLATE_VERSIONS : versioned_as
  TEMPLATE_VERSIONS ||--o{ TEMPLATE_MANIFESTS : compiled_into
  TEMPLATE_MANIFESTS ||--o{ MANIFEST_BINDINGS : bound_by
  TEMPLATE_MANIFESTS ||--o{ MANIFEST_GENERATIONS : produces
  TEMPLATE_MANIFESTS ||--o{ REVIEW_TASKS : parks
  SOURCE_FILES ||--o{ SOURCE_VERSIONS : versioned_as
  SOURCE_VERSIONS ||--o{ SOURCE_CHUNKS : chunked_into
  SOURCE_VERSIONS ||--o{ MANIFEST_BINDINGS : feeds
  GENERATED_DOCUMENTS ||--o{ DOCUMENT_VERSIONS : versioned_as
  DOCUMENT_VERSIONS ||--o{ DOCUMENT_REVIEWS : objected_to_by
  TEMPLATE_BLUEPRINTS ||--o{ TEMPLATE_BLUEPRINT_VERSIONS : versioned_as
  TEMPLATE_CLUSTERS ||--o{ TEMPLATE_CLUSTER_MEMBERS : groups
```

| Group | Tables |
|---|---|
| Identity & tenancy | `organizations`, `users`, `counters`, `lookup_values`, `org_data_policies`, `org_model_rates`, `deletion_certificates` |
| Work | `projects` |
| Templates | `template_files`, `template_versions`, `template_sections`, `template_library`, `template_library_versions`, `template_families`, `template_clusters`, `template_cluster_members` |
| Authoring | `template_blueprints`, `template_blueprint_versions` |
| Compilation | `template_manifests`, `manifest_generations`, `manifest_bindings`, `field_dictionary` |
| Sources | `source_files`, `source_versions`, `source_chunks` |
| Generation | `generation_jobs`, `generated_documents`, `document_versions`, `section_outputs` |
| Review | `review_tasks`, `document_reviews`, `review_comments` |
| Chat | `conversations`, `chat_messages` |
| Semantic memory | `embeddings`, `mapping_memory`, `mapping_memory_sharing`, `reviewer_corrections` |
| Instrumentation | `audit_logs`, `llm_calls`, `qa_failure_logs`, `operation_timings`, `suggestion_logs` |
| Vestigial | `draft_documents`, `mappings` — storage behind the removed draft + mapping-wizard pipeline. Nothing writes to either; the rows that exist are read-only history |

That is all 45.

**Design rules.** Generic SQLAlchemy `JSON` columns (not `JSONB` — the schema stays portable to SQLite) where shape is genuinely variable (manifest fields, conditions, blocks, generation settings), real indexed columns for anything queried. Soft deletes where history references the row. Versioning over mutation (the editor's autosave is the one deliberate exception). Human-friendly display ids distinct from internal UUIDs. And **`org_id` on all 43 tables that can hold customer data, derived from the parent on write** — never supplied by a caller, which would make it forgeable.

---

## 12. What is wired, and what is a seam

The repo contains **zero `TODO`, `FIXME`, `XXX`, `HACK` or `NotImplementedError` markers**. Absences are documented in prose inside module docstrings instead — which makes them easy to miss. Named here so they are not:

**Deliberate scoping choices**

- **Batch generation runs in an in-process background task.** No worker queue, so a restart mid-batch loses the run; the only recovery is the job row's progress.
- **Compile progress is polled, not pushed.** No WebSocket or SSE — the client mints a token, passes it to the compile endpoint, and reads it back through the jobs endpoint.
- **No user-management API.** Accounts and roles come from `python -m app.bootstrap` and `bootstrap add-user`. The team screen is read-only.
- **Object storage is the local filesystem** — 32 lines of `shutil`/`pathlib`. No S3/MinIO/Azure/GCS.
- **Auth is JWT-only.** No SSO/OIDC, no refresh tokens, no password reset.

**Built but not plugged in** — real code, real tests, no runtime caller:

- **Five of ten capabilities are declared but unenforced**: `UPLOAD_TEMPLATE`, `UPLOAD_SOURCE`, `COMPILE_MANIFEST`, `EDIT_MANIFEST`, `GENERATE_DOCUMENT`. Any authenticated member can upload, compile and generate. Only approval, review, audit-read and admin routes actually gate.
- **The narrative/RAG half of the resolution engine is a seam.** No caller injects a `narrative_resolver` or `fuzzy_resolver`, so every `narrative` unit and every fuzzy condition takes the refusal branch and parks for a human. That is a safe failure mode, not a silent one — but it means narrative generation does not currently run unattended.
- **The manifest envelope's `(template_hash, manifest_hash)` pin is implemented and unwired.** The approval endpoint supersedes and stamps directly rather than building an envelope, so the reproducibility pin exists in `versioning.py` and in tests only.
- **The HNSW index is created and never used.** Vector search fetches tenant-scoped rows and computes cosine in numpy; pgvector currently buys the column type and the tenant filter, not approximate nearest-neighbour search.
- **Mapping memory persists nowhere at runtime.** The binding-suggestions endpoint rebuilds an in-process memory from existing bindings on every request; the three memory tables are modelled and migrated but unwritten. Cross-tenant structural-pattern sharing has no HTTP surface at all.
- **`GET /document-versions/{id}/citations` can only return an empty list** — every document-creation site hardcodes `draft_id=None`, and nothing writes the citations column. Citation grounding is real at the provider boundary; the endpoint that would surface it is not.
- **Escaped-error reporting and the calibration log have no UI.** The backend ranks escaped error rate first among its metrics, and the only endpoint that can make it measurable is unreachable from the shipped product.
- **The Korean postposition pass never fires** — the manifest column that would enable it is not persisted.
- **`value_exceeds_max_len` is dormant** — nothing in the compiler emits a `max_len`, so it is silent until a manifest declares one.
- **A tenth assertion, `scaffolding_conflict`, is declared and never raised.** So is the compiler's optional test-fill assertion: `compile_template(test_fill=...)` is passed only from tests, so `test_fill_failure` cannot fire through the API.
- **Retrieval evidence reaches one of four compile call sites.** Only the single-template compile passes it; bulk onboarding and both blueprint compiles do not — so a template onboarded in bulk is read with no knowledge of the organisation's existing column names, which is the case the feature was built for.
- **Settings is mostly mock.** Only the Profile tab reads real data. Workspace, AI Models, Notifications, Security, API keys and Billing are static JSX. Audit-log filters, export and pagination have no handlers, and the team screen has no invite or role-change controls at all.
- **PDF export needs LibreOffice on the host.** The Docker image now installs `libreoffice-writer` (the invoice service delivers PDFs), so the container converts; a dev machine without `soffice` still answers `503 PDF_UNAVAILABLE` on the single download, and the bulk zip lists the failure per document in `_FAILED.txt`.
- **`docs/BACKEND_SPEC.md` §-numbers cited in code comments do not resolve to that document.** The code quotes an architecture record not present in this repository — treat those references as provenance notes, not lookups.

---

## 13. Getting started

**Prerequisites**: Docker (recommended) or Python 3.12 + PostgreSQL 17 + Redis on the host. Bun for the frontend either way.

### 13.1 The database is not optional, and it is not SQLite

`DATABASE_URL` defaults to SQLite and the test suite runs on it, but that is a development convenience. SQLite cannot express row-level security, the `vector` column type, foreign key enforcement or a session timezone — and every one of those has already hidden a real defect in this codebase while the SQLite run stayed green. `ENV=production` makes the process refuse to start on a SQLite URL.

The stack uses **two database roles**, and this is the part that is easy to skip:

- `documind_owner` runs migrations — creating tables, enabling RLS and installing an extension all need privileges the application must not hold.
- `documind_app` is what the API connects as: `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`.

### 13.2 Docker Compose

```sh
docker compose up -d db redis          # pgvector/pgvector:pg17 + redis:7-alpine
docker compose run --rm migrate        # alembic upgrade head, as documind_owner
docker compose up api                  # uvicorn on :8000, as documind_app
```

Nothing is seeded on boot, so until this runs the database is genuinely empty and nobody can sign in:

```sh
docker compose exec api python -m app.bootstrap \
  --org "Your Organisation" \
  --email you@example.com \
  --name "Your Name" \
  --password 'choose-a-long-one'

# Add a colleague. The separation-of-duties rules are unsatisfiable with one
# account: an author may never close the review of their own letter.
docker compose exec api python -m app.bootstrap add-user \
  --org "Your Organisation" --email colleague@example.com \
  --name "Their Name" --role approver --password '...'
```

Then, from the repo root:

```sh
bun install
bun run dev                            # Vite dev server
```

> Migrations are intentionally **not** run by the container `CMD` — `alembic upgrade head` must be a separate pre-deploy job, or a rolling deploy races itself. There is no frontend service in compose and no frontend Dockerfile; the UI runs on the host.

### 13.3 On the host instead

```sh
brew install postgresql@17 redis && brew services start postgresql@17 && brew services start redis
createuser documind_owner -P
createdb documind -O documind_owner

# Creates the pgvector extension and the documind_app role. Docker runs this
# automatically via docker-entrypoint-initdb.d; on the host, run it yourself:
POSTGRES_USER=documind_owner POSTGRES_DB=documind APP_DB_PASSWORD='another-long-one' \
  bash backend/scripts/init-db/01-app-role.sh

cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # set DATABASE_URL, JWT_SECRET, CORS_ORIGINS
DATABASE_URL=postgresql+psycopg://documind_owner:...@localhost:5432/documind alembic upgrade head
python -m app.bootstrap --org "..." --email ... --name "..." --password '...'
uvicorn app.main:app --reload --port 8000
```

`app/config.py` reads `.env` relative to the process working directory, and `alembic.ini` still carries a placeholder URL that `env.py` overrides from settings — so **both must be run from `backend/`**.

`GET /readyz` reports whether Postgres and Redis are actually connected; `GET /healthz` is a static probe. Interactive OpenAPI docs at `http://localhost:8000/docs`; a **public, login-free** product and API reference is served by the frontend at `/docs`.

### 13.4 Configuration

The frontend reads exactly one variable, `VITE_API_URL`, defaulting to `http://localhost:8000/api/v1`. The backend reads 31 settings; the ones that matter:

```sh
ENV=development               # production|prod|staging enable the boot guard
DATABASE_URL=...              # PostgreSQL in anything but dev
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=...                # the default is refused in production
CORS_ORIGINS=["http://localhost:3000"]   # a JSON list — a bare URL fails to parse

LLM_PROVIDER=anthropic        # anthropic | gemini | openai
LLM_COMPILE_PROVIDER=         # optional — compile on one vendor, generate on another
ANTHROPIC_API_KEY=...         # / GEMINI_API_KEY / OPENAI_API_KEY

LLM_RESIDENCY=GLOBAL          # GLOBAL | EU | UK | IN — what the deployment actually offers
LLM_ZERO_RETENTION=false      # confirm contractually before setting true
```

The last two are **assertions about a contract, not preferences**, so both default to the weakest claim: an organisation requiring more than the deployment offers gets a refusal rather than a prompt. A provider name that is not one of the three is rejected rather than defaulted — a typo must not silently route every document through a vendor nobody chose.

---

## 14. Tests and CI

```sh
cd backend && ./scripts/check.sh        # the gate. CI runs this exact script.
```

One script, run identically locally and in CI, so *"green locally"* and *"green in CI"* cannot drift into two definitions of done. It runs fully offline — `conftest.py` blanks every provider key, and any model-backed path is expected to refuse rather than reach out.

| | |
|---|---|
| Tests collected | **2,266** across 89 modules |
| Last verified run | 2,220 passed, 2 skipped, exit 0 |
| Measured coverage | **87.33%** (14,995 statements, 1,900 missed) |
| Global floor | 76%, env-overridable |
| Per-module floors | **34**, grouped under comments naming the failure modes they guard |
| Held at 100% | `docx_prescan`, `renderers`, `llm/pricing`, `compiler/confidence`, `downloads`, `retention` |

**CI runs the suite twice** — once on SQLite, once against real PostgreSQL — because row-level security, the pgvector column, foreign keys and the session timezone only exist on the latter, and each has hidden a real defect while the SQLite run stayed green. CI connects as a purpose-made non-superuser role: PostgreSQL ignores RLS for a superuser, so the whole PostgreSQL job would otherwise have asserted against a database that does not filter, and the RLS tests would have proved nothing while passing.

**Three coverage floors are reported as unmeasurable rather than failing.** `rule_compiler`, `docx_renderer` and `docx_prescan` can only reach their floors with the customer-owned template masters, which are deliberately not published here. `conftest.py` skips the 345 tests that need them, so on a checkout without those files those modules are legitimately lower. Printing them with a `--` marker, the real percentage and the floor they could not be checked against is not a silent pass — but failing on every machine that does *not* hold customer data is backwards, and is the kind of red that teaches people to stop reading CI.

**Known gaps in the gate**: there is no frontend test suite at all (the only frontend CI check is `tsc --noEmit` — neither `lint` nor `build` runs), no Python linter or formatter, and no pytest config file, so a bare `pytest` runs with no coverage and no floors. CI's PostgreSQL image is `pg16` while compose uses `pg17`.

---

## 15. Project structure

```
TemplateAI/
├── docker-compose.yml             # pg17+pgvector · redis · migrate(owner) · api(app role)
├── src/                           # Frontend — 15 routes, ~14.6k lines
│   ├── routes/                    #   3 public (/, /login, /docs) + 12 under the /_app guard
│   ├── components/
│   │   ├── document-mapping.tsx   #   bind columns → generate
│   │   ├── batch-progress.tsx     #   held above the stage that starts it, so it survives navigation
│   │   ├── review-bar.tsx, bulk-select.tsx, command-palette.tsx, motion.tsx
│   │   └── ui/                    #   17 Radix/shadcn primitives
│   └── lib/                       # api.ts (~110 methods), store.ts, types.ts, theme.tsx
├── backend/
│   ├── scripts/check.sh           # THE GATE — CI runs this verbatim
│   ├── scripts/init-db/           # 01-app-role.sh — documind_app NOSUPERUSER NOBYPASSRLS
│   ├── alembic/versions/          # 20 revisions, single head
│   ├── tests/                     # 81 modules, golden-DOCX fixtures, tenancy walker
│   └── app/                       # ~38.5k lines
│       ├── main.py                #   14 routers, CORS, startup RLS verification
│       ├── models.py              #   45 tables
│       ├── routers/               #   auth · projects · templates · sources · manifests ·
│       │                          #     bindings · generation · blueprints · review · reviews ·
│       │                          #     chat · admin · metrics · downloads
│       ├── templates/             #   read_docx · emit_docx · blueprint(+lint,+ops) ·
│       │                          #     kits · lift · fingerprint · family_matcher · inheritance
│       │   ├── parsers/           #     docx_parser · docx_prescan · docx_safety
│       │   └── kits/              #     5 starter kits (en/ja/ko/zh grammar YAML is at app/conventions/)
│       ├── compiler/              #   agentic_compiler · rule_compiler · llm_compiler ·
│       │                          #     assertions · confidence · blueprint_agent · mapping_agent
│       ├── manifests/             #   envelope · validator · versioning · diff
│       ├── expressions/           #   documind-expr/1.0 · plain_english · token_parser
│       ├── generation/            #   docx_renderer (the fill engine) · batch_runner ·
│       │                          #     resolution_engine · pdf_renderer · value_format ·
│       │                          #     missing_policy · source_ingestion · document_status
│       ├── qa/                    #   16 gates + policy routing + package diff
│       ├── retrieval/             #   hybrid · lexical · vector · embeddings · mapping_memory
│       ├── llm/                   #   provider (3 vendors) · boundary · redaction · metering · pricing
│       ├── audit/, tenancy.py, authz.py, ownership.py, security.py, retention.py
│       └── metrics.py, analytics.py, downloads.py, rate_limit.py, bootstrap.py
└── docs/
    ├── BACKEND_SPEC.md
    └── TEMPLATE_COMPILER_RESEARCH.md
```

---

## 16. Documentation index

| Document | What's in it |
|---|---|
| **`README.md`** (this file) | The living overview — verified against the code. Keep it current. |
| **`APPLICATION_FLOW.md`** | Layer-by-layer technical briefing: repository map, data model, every flow, the full route inventory, and what was removed and why. |
| **`docs/BACKEND_SPEC.md`** | The backend engineering **specification** — full DDL, scalability/security/reliability design, roadmap. Parts describe a target rather than the code; its "Implementation status" block says which. Its §-numbers do not match those cited in code comments ([§12](#12-what-is-wired-and-what-is-a-seam)). |
| **`docs/TEMPLATE_COMPILER_RESEARCH.md`** | The research behind the compiler + fill engine, grounded in analysis of the real Hospira/Pfizer template, including the RAG-vs-deterministic decision framework. |
| **`AGENTS.md`** | Lovable sync notes. **This repo syncs commits back to the Lovable editor — do not force-push or rebase.** |
| `/docs` (frontend) | Public, login-free product and API reference: the five steps, marking up a template, the condition grammar, both document status axes, and 35 endpoints with an end-to-end example, each checked against the running server's OpenAPI document. |
| `http://localhost:8000/docs` | Live Swagger UI generated from the actual FastAPI routes. |

---

<sub>Originally scaffolded as a Lovable frontend (`docucraft-ai`); it has since grown an independent FastAPI backend, a deterministic template-compilation engine, and this documentation set.</sub>

# BUILD PROMPT — Safety / Pharmacovigilance Document Generation Module

> Saved verbatim as supplied, the way `CSR_MODULE_SPEC.md` and
> `CMC_MODULE_SPEC.md` are. What the code does is answerable against this file
> rather than against anybody's memory of a conversation.

> **How to use:** Fill every `[BRACKETED]` placeholder, then paste this whole prompt into your AI coding assistant. Build ONE milestone at a time (§17) and stop for review between milestones.
>
> **Guideline codes below (ICH E2A/E2B(R3)/E2C(R2)/E2D/E2E/E2F; EU GVP Modules; 21 CFR 312.32, 314.80, 600.80; MedDRA; CIOMS) are structural pointers only.** Your pharmacovigilance/QPPV owner confirms the current revision of each before this module touches real data.

---

## 1. Context and role

You are a senior full-stack + GenAI engineer working inside my existing codebase.

I have a working AI document-generation portal with live services for **HR Letters, Financial, Legal**, a **Clinical Study Report (CSR)** module, and a **Quality/CMC** module. **Existing stack:** React (TanStack) frontend, FastAPI backend, PostgreSQL + pgvector, Redis, [LLM provider], JWT auth with Redis rate limiting, Alembic migrations, plus a RAG section generator with citations, a token template library, and a deterministic template compiler/fill engine.

**Before writing any code:** inspect the codebase — especially the CMC module — and summarise what you will reuse. The CMC module already established the pattern this module needs: **structured data store → human verification grid → deterministic table rendering → LLM writes only prose around locked tables.** Extract that into shared services if it is not already shared. **Do not fork CMC.**

**Your task:** build the **Safety / Pharmacovigilance Generator**.

**What makes this module structurally different from CSR and CMC:**

| | CSR | CMC | **Safety** |
|---|---|---|---|
| Unit of work | one study | one product dossier | **a recurring reporting interval** |
| Data source | study TLFs | CoAs, spec, stability | **a safety database (case-level), refreshed every cycle** |
| Time model | one-off | lifecycle versions | **interval + cumulative-since-IBD, dual accounting, hard data lock point** |
| Dominant risk | wrong wording | wrong number | **wrong case count, wrong expectedness, or leaked patient identity** |

Three consequences drive the whole design: (a) every report instance is **generated as a delta against the previous approved report**; (b) **de-identification is a mandatory pipeline stage**, not a warning banner; (c) **the LLM never counts cases, never assigns causality, seriousness or expectedness, and never computes a signal.**

---

## 2. Non-negotiable design principles

1. **The LLM does not do pharmacovigilance judgment.** Seriousness, seriousness criteria, expectedness/listedness against the Reference Safety Information, and causality are **human-owned fields**. The system may surface a suggestion with its basis, clearly labelled as a suggestion; a qualified user must confirm before it counts anywhere. No exceptions, no silent defaults.
2. **The LLM never counts.** Every case count, tabulation cell, rate, and exposure figure is computed in Python from the structured case store and rendered by the deterministic fill engine. Prose pulls figures from the same store via tokens, so prose and tables cannot disagree.
3. **No automated regulatory action.** No auto-submission to any gateway (EudraVigilance, FAERS, national authority). No authoritative expedited-reporting clock: due-date views are **informational only** and must carry a visible disclaimer that the sponsor's PV system of record governs reporting obligations.
4. **De-identification is a pipeline stage.** Patient identifiers, reporter identifiers, site/investigator names, and free-text narrative identifiers are detected and masked before content is indexed, embedded, or sent to any model. Detected-but-unmasked items block progression until reviewed.
5. **Data lock point is absolute.** No case with a receipt/version date after the DLP contributes to that report's interval or cumulative figures. Enforced in query layer, not by convention.
6. **RSI is pinned per report.** Expectedness is evaluated against one pinned Reference Safety Information version (CCDS / IB / SmPC / USPI). Changing RSI mid-cycle creates a new pinned version and flags all affected expectedness determinations for re-review.
7. **Delta-first generation.** Each report instance loads the previous approved report as baseline. Sections that are stable carry forward as editable baseline text; sections driven by interval data regenerate. The workspace shows, per section, what changed.
8. **Human-in-the-loop.** Draft → In Review → Approved → (QPPV sign-off where configured). Export requires approval. UI positions this as an AI-assisted drafting tool for PV/medical writers.
9. **Audit and data integrity.** Every ingestion, de-identification decision, field confirmation, generation, edit, approval, sign-off and export recorded (actor, timestamp, before/after). Design toward ALCOA+ and 21 CFR Part 11 direction. Do not claim compliance in UI copy; do not design it out.
10. **Truly async, and never rebuild DOCX from editor HTML.** Same as the other modules.

---

## 3. Output document types (registry — seed these)

Each entry ships a built-in section structure, a required-source profile, a section→data mapping and a section→source mapping. All editable as JSON config.

| key | Deliverable | Structure basis |
|---|---|---|
| `pbrer` | Periodic Benefit-Risk Evaluation Report (EU PSUR) | ICH E2C(R2) · GVP Module VII |
| `dsur` | Development Safety Update Report | ICH E2F |
| `pader` | Periodic Adverse Drug Experience Report (US) | 21 CFR 314.80 / 600.80 |
| `rmp` | Risk Management Plan (EU) | GVP Module V |
| `signal_eval` | Signal Evaluation / Assessment Report | GVP Module IX |
| `icsr_narrative` | ICSR case narrative (single or batch) | ICH E2B(R3) data elements · CIOMS I |
| `aco` | Addendum to Clinical Overview (renewal) | EU renewal guidance |
| `lit_review` | Safety literature monitoring summary | GVP Module VI |

Full built-in section trees for `pbrer`, `dsur`, `rmp` and `signal_eval` are in §15. One product safety profile may carry many report instances across many intervals; they share one case store, one signal log and one audit trail.

---

## 4. Data model

Adapt naming to existing conventions; one Alembic migration per milestone.

**Product and reporting cycle**
- **pv_product** — id, org_id, product_name, inn, mah_name, atc_code, ibd (international birth date), dibd (development IBD), formulations[], routes[], approved_indications[], development_indications[], regions[], status, timestamps
- **pv_rsi_version** — id, product_id, rsi_type (ccds | ib | smpc | uspi | other), version_label, effective_date, source_document_id, superseded_by
- **pv_rsi_listed_term** — id, rsi_version_id, meddra_pt, meddra_soc, condition_text (e.g. "serious only", "specific indication") — this table defines listedness
- **pv_report_instance** — id, product_id, doc_type_key (§3), sequence_number, period_start, period_end, data_lock_point, rsi_version_id, baseline_report_id (previous approved instance), regions[], status, qppv_signoff_by, qppv_signoff_at, timestamps
- **pv_due_date** — id, report_instance_id, region, submission_due_date, basis_note, is_informational (always true)

**Case data (the core store)**
- **pv_case** — id, product_id, worldwide_case_id, local_case_ids[], case_version, report_source (spontaneous | clinical_trial | non_interventional | literature | regulatory_authority | patient_support_programme | other), study_id, country_of_occurrence, primary_reporter_qualification, initial_receipt_date, latest_receipt_date, is_medically_confirmed, is_serious, seriousness_criteria[] (death | life_threatening | hospitalisation | disability | congenital_anomaly | other_medically_important), case_outcome, patient_age, patient_age_group, patient_sex, is_pregnancy_case, is_special_situation, special_situation_types[], deidentification_status, source_document_id, imported_from, confirmed_by, confirmed_at
- **pv_case_event** — id, case_id, verbatim_term, meddra_llt, meddra_pt, meddra_hlt, meddra_hlgt, meddra_soc, meddra_version, is_serious, seriousness_criteria[], expectedness (listed | unlisted | not_assessed), expectedness_rsi_version_id, causality_reporter, causality_company, onset_date, outcome, is_aesi (adverse event of special interest), suggested_by_system_json, confirmed_by, confirmed_at
- **pv_case_drug** — id, case_id, drug_name, is_company_product, role (suspect | concomitant | interacting), dose, dose_unit, frequency, route, indication, start_date, end_date, action_taken, dechallenge, rechallenge
- **pv_case_narrative** — id, case_id, version, raw_text_redacted, generated_text, created_by, created_at
- **pv_case_lab** — id, case_id, test_name, result, unit, reference_range, date

**Aggregation and evaluation**
- **pv_exposure** — id, report_instance_id, context (clinical_trial | marketing), region, population_descriptor, measure (subjects | patient_years | treatment_days | units_sold | prescriptions), value, calculation_method_note, source_document_id, confirmed_by
- **pv_study** — id, product_id, study_id, title, phase, status, population, planned_enrolment, actual_enrolment, start_date, completion_date, is_in_reporting_period (for DSUR §5 inventory)
- **pv_signal** — id, product_id, signal_reference, description, meddra_terms[], detection_source (disproportionality | case_review | literature | authority_request | trial | other), detection_date, status (new | ongoing | closed), priority, evaluation_summary, conclusion, action_taken, closure_date, linked_case_ids[], linked_report_instance_ids[]
- **pv_safety_concern** — id, product_id, concern_type (important_identified_risk | important_potential_risk | missing_information), title, meddra_terms[], first_added_report_id, status, rmp_part_reference (keeps RMP and PBRER §16.1 in sync)
- **pv_safety_action** — id, product_id, action_type (label_change | dhpc | suspension | withdrawal | restriction | protocol_amendment | clinical_hold | other), description, region, date, reason, source_document_id
- **pv_literature_ref** — id, product_id, citation, database, search_date, search_strategy_ref, relevance, linked_case_ids[]
- **pv_approval_status** — id, product_id, country, approval_date, indication, formulation, status (approved | withdrawn | not_approved | pending), source_document_id

**Documents / sections / output** — `pv_document`, `pv_chunk`, `pv_section`, `pv_section_draft`, `pv_citation`, `pv_export`, `pv_audit_log`: same shapes as the CMC module's equivalents. Reuse the shared services.

Enforce org + project-membership authorization on **every** endpoint. Add a **role dimension**: `pv_role` (writer | reviewer | qualified_person) gating who may confirm expectedness/causality and who may sign off.

---

## 5. Source and input types

**Structured case inputs**
- `e2b_r3_xml` — ICSR export (E2B(R3) or R2), preferred path
- `line_listing` — Excel/CSV export from the safety database (Argus / ArisG / Vault Safety / LifeSphere / other)
- `cioms_form` — CIOMS I PDFs
- `case_narrative_doc` — narrative text documents

**Document inputs (tagged like CMC)**

| doc_type | Display name | Feeds |
|---|---|---|
| `rsi_doc` | CCDS / IB / SmPC / USPI (Reference Safety Information) | §4 changes, expectedness basis |
| `previous_report` | Previous PBRER/DSUR/PADER (**baseline**) | delta generation, all carry-forward sections |
| `rmp_doc` | Current Risk Management Plan | safety concerns, risk minimisation |
| `study_report` | CSR / interim analysis / study synopsis | clinical trial findings sections |
| `study_registry` | Trial inventory / registry export | DSUR §5 |
| `exposure_data` | Sales, prescription, or exposure calculation source | §5 / §6 exposure |
| `literature` | Literature search output, articles, abstracts | literature section |
| `nonclinical` | Non-clinical study reports | non-clinical section |
| `authority_corr` | Regulatory authority correspondence, requests, assessments | actions taken, region-specific |
| `signal_doc` | Signal detection outputs, disproportionality runs, signal assessments | signal sections |
| `epi_data` | Epidemiology of indication / background incidence | RMP Part II SI |
| `other` | Other supporting document | — |

`previous_report` is dual-purpose: retrievable as **baseline text** for carry-forward sections and used for **continuity checks** (§11) — but tagged "prior report — verify currency before reuse."

Required/recommended checklist is computed from the selected report type, not fixed.

---

## 6. Ingestion pipeline

**Stage 1 — Parse.** E2B XML → map data elements to `pv_case` / `pv_case_event` / `pv_case_drug` (support R3, degrade gracefully for R2). Line listings → column-mapping UI with a saved mapping profile per source system, so the second cycle is one click. CIOMS PDFs and narrative docs → table/text extraction. Other documents → CMC-style parse and chunk.

**Stage 2 — De-identification (mandatory, blocking).**
Detect and mask before indexing, embedding, or any model call: patient names/initials, patient identifiers and record numbers, exact dates of birth, addresses and precise locations below country/region level, phone/email, reporter and investigator names, site names and identifiers, national ID numbers. Approach: pattern rules + structured-field mapping first; an NER pass second; **never a model call on un-masked text**. Store the masked text as the working copy; keep the original encrypted, access-restricted, excluded from indexing, and never included in an export. Ambiguous detections go to a **de-identification review queue** that blocks progression until resolved. Log every masking decision.

**Stage 3 — Normalisation and coding.**
Map verbatim terms to MedDRA (LLT → PT → HLT → HLGT → SOC) using the MedDRA version configured for this report instance. If a licensed MedDRA dictionary is loaded, use it; **if not, do not guess codes** — leave `meddra_pt` null, mark the event `coding_required`, and surface it in the review grid. Record the MedDRA version on every coded event. Compute **suggested** expectedness by matching coded PT against `pv_rsi_listed_term` for the pinned RSI version and write it to `suggested_by_system_json` — never to the confirmed field.

**Stage 4 — Duplicate detection.** Candidate duplicates by (product, country, patient attributes, event PTs, onset date, reporter) → duplicate review queue. Never auto-merge.

**Stage 5 — Embed & index** masked narrative and document chunks into the per-product namespace, with metadata filters (product, report_instance, doc_type, report_source, is_table).

---

## 7. UI flow (screens)

**S0 — Dashboard.** "Safety / Pharmacovigilance" service card → product list, plus a **submission calendar** showing upcoming report instances by DLP and region, with the informational-only disclaimer.

**S1 — Product safety profile.** `pv_product` fields, IBD/DIBD, indications, regions, MAH; approval status table; RSI version library with the current pinned version highlighted.

**S2 — Report instance setup.** Report type, sequence number, period start/end, **data lock point**, RSI version pin, MedDRA version, target regions, baseline (previous approved instance). Show a computed preview: "cases in interval: N · cumulative: M · new since last report: K" before the user commits.

**S3 — Data ingestion.** Upload E2B XML / line listings / CIOMS / documents. Column-mapping step for line listings with reusable profiles. Doc-type tagging for documents. Required/recommended checklist per report type.

**S4 — Processing & de-identification review.** Per-file status: Queued → Parsing → De-identifying → Coding → Indexing → Done/Failed. **De-identification review queue** front and centre: detected item, context snippet, proposed mask, Accept / Edit / Not an identifier. Progress gate: no generation until the queue is clear (override requires the qualified-person role + audit entry).

**S5 — Case review grid.** Tabbed, spreadsheet-style, virtualised:
- **Cases** — case id, source, country, receipt dates, serious flag, criteria, outcome, demographics, in-interval/cumulative flags.
- **Events** — verbatim, coded PT/SOC, seriousness, **expectedness (system suggestion vs confirmed)**, causality reporter/company, AESI flag. Suggestion chips read "Suggested: Unlisted — no matching PT in CCDS v3.2. Confirm." Confirm / Override / Bulk-confirm by PT.
- **Drugs** — suspect/concomitant, dose, route, dates, action taken, dechallenge/rechallenge.
- **Duplicates** — candidate pairs side by side, Merge / Keep both / Link.
- **Coding required** — events with no MedDRA code.
Header bar: "X of Y events confirmed" with a hard gate on generating data-dependent sections.

**S6 — Aggregation & exposure.** Exposure entry per context/region/measure with the calculation method note and source. Live preview of computed tabulations (§9) with drill-down from any cell to the contributing case list. Interval vs cumulative toggle.

**S7 — Generation workspace (three panes).**
- **Left:** section tree with status chips and a **delta badge** (Carried forward / Changed / New data / Needs rewrite).
- **Centre:** editor. **Tabulations and line listings are locked blocks** rendered from confirmed data — "Rendered from confirmed case data — edit in Case Review." Baseline text from the previous report loads as editable prose with changed portions highlighted. Versioned drafts; regenerate with instruction → new version.
- **Right, four tabs:** **Sources** (retrieved chunks, citation ↔ chunk highlighting) · **Data** (which cases/figures this section resolves to, drill-down to case list) · **Baseline** (previous report's text for this section, side-by-side diff) · **Issues** (QC flags).

**S8 — Signal management workspace.** Signal log (new / ongoing / closed), each with source, MedDRA terms, linked cases, evaluation, conclusion, action, and which report instances it appeared in. Disproportionality panel (§9) with the screening-only disclaimer. Signals flow into the report's signal sections automatically as a rendered table plus generated narrative per signal.

**S9 — QC dashboard.** §11 checks grouped Blocker / Warning / Info, each linking to the section, grid cell or case.

**S10 — Export.** Options: report type variant per region, appendices to include (line listings, tabulations, RSI, study inventory, signal log), citation handling, DRAFT watermark, tracked changes vs baseline report. Blocked until approval (and QPPV sign-off where configured). Export history with working downloads.

---

## 8. Generation engine — prose sections

Per section: resolve mapped doc_types and data scopes → build retrieval query from section code + title + guidance + product metadata + reporting interval → retrieve top-k (k ≈ [12–20]) filtered by product/report_instance/doc_type → assemble context including the baseline section text and the confirmed data summary → stream → persist draft, citations, audit.

**Embed this system prompt verbatim (braces are template variables):**

```
You are drafting section {section_code} "{section_title}" of a {deliverable_name} — a pharmacovigilance
periodic safety document prepared to {structure_basis} conventions for {target_regions}.

PRODUCT: {product_name} ({inn}), MAH {mah_name}. IBD {ibd}. DIBD {dibd}.
REPORTING INTERVAL: {period_start} to {period_end}. DATA LOCK POINT: {data_lock_point}.
REFERENCE SAFETY INFORMATION IN FORCE FOR THIS REPORT: {rsi_label} version {rsi_version}, effective
{rsi_effective_date}. MedDRA version {meddra_version}.

PRODUCT AND PERIOD METADATA:
{report_metadata_json}

CONFIRMED SAFETY DATA AVAILABLE TO THIS SECTION (read-only, already rendered as tables in the document):
{confirmed_data_summary_json}

BASELINE TEXT FROM THE PREVIOUS APPROVED REPORT FOR THIS SECTION (may be reused where still accurate;
verify against this interval's data before retaining any statement):
{baseline_section_text}

TEMPLATE GUIDANCE FOR THIS SECTION:
{section_guidance}

SOURCE EXTRACTS — the only permitted factual basis for narrative content:
{numbered_source_extracts}

RULES:
1. Use ONLY the source extracts, report metadata, baseline text and confirmed safety data above for
   product-specific facts. General pharmacovigilance and medical knowledge may shape structure and
   phrasing, never content.
2. DO NOT WRITE TABULATIONS OR LINE LISTINGS. Summary tabulations, line listings, exposure tables,
   the signal overview and the safety-concern table are inserted automatically from confirmed data.
   Where such a table belongs, output the single line: [TABLE: <table_key>] and continue.
3. DO NOT COUNT, TOTAL, SUBTRACT OR ESTIMATE CASES, EVENTS, RATES OR EXPOSURE. Every figure you state
   must appear verbatim in the confirmed safety data above; reproduce it exactly and follow it with its
   citation [S#, p.X] or [S#, Table Y].
4. DO NOT ASSIGN OR ALTER seriousness, seriousness criteria, expectedness/listedness, or causality.
   Report only what the confirmed data records. Do not describe an event as "listed", "unlisted",
   "related" or "unrelated" unless that determination is present in the confirmed data.
5. DO NOT CONCLUDE that a signal exists, is refuted, or is causally associated with the product, and do
   not state a change to the benefit-risk balance, unless that conclusion is explicitly stated in the
   sources. Where the section requires such an evaluation and no sourced conclusion exists, write
   [ASSESSMENT REQUIRED: <the specific judgment the qualified person must make>].
6. Distinguish INTERVAL data from CUMULATIVE data explicitly in every sentence where a figure appears.
   Never merge the two. Never carry an interval figure forward from the baseline text.
7. If information this section requires is absent, insert [DATA NEEDED: <exactly what is missing>].
   Never guess. Never omit silently.
8. NEVER include patient identifiers, reporter names, investigator names, site names, exact dates of
   birth, or any identifying detail. Refer to cases by case identifier only.
9. Baseline text is a starting point, not a source of current fact. Any statement carried forward must
   be consistent with this interval's confirmed data; where it is not, rewrite it and note the change.
10. Style: formal regulatory English, third person, past tense for events and actions in the interval,
    present tense for the current state of knowledge. No speculation, no promotional language.
11. Keep the exact section code and title given. Output the heading, then the content. No markdown
    decoration beyond headings. No commentary about being an AI.
```

**`icsr_narrative` uses a separate, tighter prompt:** a fixed narrative order (demographics → relevant medical history and concomitant medications → suspect product with dose, route and dates → event onset with dates and course → treatment given → outcome → dechallenge/rechallenge → reporter's and company's causality as recorded → relevant laboratory data → follow-up status), every clause traceable to a case field, no interpretation, no identifiers, and `[DATA NEEDED: …]` for absent E2B elements.

**Model/cost:** strong model for drafting and benefit-risk sections; cheap model for utility tasks (abbreviation extraction, QC parsing, delta summarisation). Config-driven model names, k, thresholds, token caps.

---

## 9. Computed tabulations and analytics (never LLM)

**Table builders** — query the confirmed case store and render through the fill engine. `[TABLE: key]` markers resolve at render time, so a case correction propagates everywhere without regeneration. Seed:
- `summary_tab_soc_pt` — SOC × PT × (serious/non-serious) × (listed/unlisted), interval and cumulative columns
- `summary_tab_trials` — cumulative SAEs from clinical trials by SOC/PT and treatment arm where unblinded
- `line_listing_sar` — serious adverse reactions in the interval (DSUR §7.2 format), de-identified
- `exposure_table` — clinical trial and marketing exposure by region and measure, with method notes
- `signal_overview` — new / ongoing / closed signals with status, source and action
- `safety_concern_table` — important identified risks, important potential risks, missing information
- `action_table` — actions taken for safety reasons in the interval, by region
- `study_inventory` — DSUR §5 trials ongoing and completed
- `approval_status_table` — worldwide marketing approval status
- `literature_table` — literature references and linked cases

**Disproportionality** (PRR, ROR, information component / EBGM if implemented) computed in Python, presented with counts and confidence intervals, and labelled in both UI and output: **"Screening statistic — indicates reporting frequency, not causality; not evidence of a causal association."** Do not auto-create signals from a threshold crossing; create a **candidate** requiring human triage.

**Interval/cumulative engine.** One query layer computes every figure with explicit DLP, interval and cumulative filters. Every rendered figure carries its scope in metadata so QC can verify prose against it. No figure is ever computed twice by two code paths.

---

## 10. Delta and baseline handling

On report-instance creation, copy the baseline report's section content into the new instance as `carried_forward` drafts. Classify each section:
- **Carried forward** — no new data in scope; text loads editable, badge shown.
- **Changed** — new cases, new actions, RSI change, or new signals in scope; regeneration offered with a summary of what changed.
- **New data** — sections whose tables changed but whose narrative may still hold.
- **Needs rewrite** — sections where the qualified person flags material change (benefit-risk, conclusions).

Provide a **"what changed this interval"** summary generated from structured deltas (case counts by SOC, new signals, closed signals, RSI changes, safety actions, new studies) — computed, not LLM-authored, with an LLM-written prose framing on top.

---

## 11. QC checks (mandatory)

**Blockers**
1. **De-identification queue not clear**, or an identifier pattern detected in any draft or appendix (PII leakage scan runs on every draft save and every export).
2. **Unconfirmed data** feeding an enabled section (expectedness, seriousness, causality, or exposure not confirmed).
3. **Count reconciliation:** line listing rows = summary tabulation totals = figures quoted in prose, for every scope.
4. **Interval/cumulative integrity:** cumulative ≥ interval; this report's cumulative ≥ previous report's cumulative; no case dated after the DLP inside interval or cumulative figures.
5. **RSI pin integrity:** every confirmed expectedness references the report's pinned RSI version; RSI change mid-cycle leaves stale determinations.
6. **MedDRA version consistency** across all tabulations in one report.
7. **Unresolved `[TABLE: key]`** or `[DATA NEEDED]` markers.
8. **`[ASSESSMENT REQUIRED]`** items unresolved — must be answered by a qualified-person-role user.
9. **Exposure denominator missing** for any rate stated anywhere in the report.

**Warnings**
10. **Duplicate candidates** unresolved.
11. **Coding required** events remaining.
12. **Missing narratives** for cases that conventionally require them (deaths, life-threatening, other cases of special interest) per configurable rules.
13. **Signal completeness:** every signal in the overview has a status; every closed signal has a conclusion and an action.
14. **Cross-document continuity:** safety concerns in the report match the current RMP; signals closed here reflected in the signal log; label changes reflected in the RSI library.
15. **Baseline drift:** carried-forward statements contradicted by this interval's confirmed data.
16. **Citation coverage** and **number-to-source verification** in prose.
17. **Region profile gaps:** required region-specific items missing for a selected region.

**Info**
18. Abbreviation builder; delta summary; case-volume trend versus previous intervals.

---

## 12. Export

Assemble approved sections in report order, resolving `[TABLE: …]` at render time, applying template styles: heading levels, TOC, page numbers, header with product + report type + interval + "CONFIDENTIAL", optional DRAFT watermark. Appendices assembled from the same builders. Region variants where the structure differs. Tracked changes versus the baseline report. PDF via DOCX conversion. Record in `pv_export` + audit log.

**Hard constraints:** the original un-masked case text is never exportable through this module. Do not generate E2B XML, CIOMS forms for submission, or any gateway payload — state in the UI that transmission belongs to the safety system of record.

---

## 13. API surface (match existing FastAPI conventions)

- `POST/GET /pv/products`, `GET/PATCH/DELETE /pv/products/{id}`
- `POST /pv/products/{id}/rsi-versions`, `GET /pv/rsi-versions/{id}/listed-terms`, `POST /pv/rsi-versions/{id}/pin`
- `POST /pv/products/{id}/reports`, `GET /pv/reports/{id}`, `GET /pv/reports/{id}/preview-scope`
- `POST /pv/reports/{id}/ingest` (multipart + input_type + mapping_profile), `GET /pv/reports/{id}/ingest-status`
- `GET /pv/reports/{id}/deid-queue`, `POST /pv/deid-items/{id}/resolve`
- `GET /pv/reports/{id}/cases`, `PATCH /pv/case-events/{id}/confirm`, `POST /pv/case-events/bulk-confirm`, `GET /pv/reports/{id}/duplicates`, `POST /pv/duplicates/{id}/resolve`
- `POST /pv/reports/{id}/exposure`, `PATCH /pv/exposure/{id}`
- `GET /pv/reports/{id}/tabulations/{table_key}`, `GET /pv/reports/{id}/tabulations/{table_key}/drilldown`
- `GET/POST /pv/products/{id}/signals`, `PATCH /pv/signals/{id}`, `POST /pv/reports/{id}/disproportionality`
- `GET/POST /pv/products/{id}/safety-concerns`
- `POST /pv/sections/{id}/generate`, `GET/PUT /pv/sections/{id}/draft`, `PATCH /pv/sections/{id}/status`, `GET /pv/sections/{id}/baseline-diff`
- `GET /pv/reports/{id}/qc`, `POST /pv/reports/{id}/signoff`, `POST /pv/reports/{id}/export`, `GET /pv/reports/{id}/audit`

---

## 14. Non-functional requirements

- Case stores of 100,000+ cases and 500,000+ events per product; tabulation queries and grid paging must stay responsive (index on product, DLP-relevant dates, PT, SOC, seriousness, source).
- Real background jobs with retry/backoff and partial-failure isolation; streaming generation.
- Role-based authorization: only the qualified-person role may confirm causality/expectedness overrides, clear the de-identification gate, or sign off.
- Encryption at rest; original un-masked text in a separate access-controlled store with its own audit trail.
- Tests: unit tests for the interval/cumulative query layer (highest priority), DLP filtering, expectedness matching against RSI, tabulation totals, de-identification patterns, duplicate detection, citation parsing and number-to-source matching. End-to-end: ingest → confirm → generate → QC → export.

---

## 15. Built-in section structures (seed data)

**`pbrer` (ICH E2C(R2))**
1 Introduction · 2 Worldwide Marketing Approval Status · 3 Actions Taken in the Reporting Interval for Safety Reasons · 4 Changes to Reference Safety Information · 5 Estimated Exposure and Use Patterns (5.1 Cumulative Subject Exposure in Clinical Trials · 5.2 Cumulative and Interval Patient Exposure from Marketing Experience) · 6 Data in Summary Tabulations (6.1 Reference Information · 6.2 Cumulative Summary Tabulations of Serious Adverse Events from Clinical Trials · 6.3 Cumulative and Interval Summary Tabulations from Post-Marketing Data Sources) · 7 Summaries of Significant Findings from Clinical Trials during the Reporting Interval (7.1 Completed Clinical Trials · 7.2 Ongoing Clinical Trials · 7.3 Long-term Follow-up · 7.4 Other Therapeutic Use · 7.5 New Safety Data Related to Fixed Combination Therapies) · 8 Findings from Non-interventional Studies · 9 Information from Other Clinical Trials and Sources · 10 Non-clinical Data · 11 Literature · 12 Other Periodic Reports · 13 Lack of Efficacy in Controlled Clinical Trials · 14 Late-Breaking Information · 15 Overview of Signals: New, Ongoing or Closed · 16 Signal and Risk Evaluation (16.1 Summary of Safety Concerns · 16.2 Signal Evaluation · 16.3 Evaluation of Risks and New Information · 16.4 Characterisation of Risks · 16.5 Effectiveness of Risk Minimisation) · 17 Benefit Evaluation (17.1 Important Baseline Efficacy and Effectiveness Information · 17.2 Newly Identified Information on Efficacy and Effectiveness · 17.3 Characterisation of Benefits) · 18 Integrated Benefit-Risk Analysis for Approved Indications (18.1 Benefit-Risk Context · 18.2 Benefit-Risk Analysis Evaluation) · 19 Conclusions and Actions · 20 Appendices

**`dsur` (ICH E2F)**
1 Introduction · 2 Worldwide Marketing Approval Status · 3 Actions Taken in the Reporting Period for Safety Reasons · 4 Changes to Reference Safety Information · 5 Inventory of Clinical Trials Ongoing and Completed during the Reporting Period · 6 Estimated Cumulative Exposure (6.1 Cumulative Subject Exposure in the Development Programme · 6.2 Patient Exposure from Marketing Experience) · 7 Data in Line Listings and Summary Tabulations (7.1 Reference Information · 7.2 Line Listings of Serious Adverse Reactions during the Reporting Period · 7.3 Cumulative Summary Tabulations of Serious Adverse Events) · 8 Significant Findings from Clinical Trials during the Reporting Period (8.1 Completed · 8.2 Ongoing · 8.3 Long-term Follow-up · 8.4 Other Therapeutic Use · 8.5 Combination Therapies) · 9 Safety Findings from Non-interventional Studies · 10 Other Clinical Trial and Study Safety Information · 11 Safety Findings from Marketing Experience · 12 Non-clinical Data · 13 Literature · 14 Other DSURs · 15 Lack of Efficacy · 16 Region-Specific Information · 17 Late-Breaking Information · 18 Overall Safety Assessment (18.1 Evaluation of the Risks · 18.2 Benefit-Risk Considerations) · 19 Summary of Important Risks · 20 Conclusions · Appendices

**`rmp` (EU GVP Module V)**
Part I Product Overview · Part II Safety Specification (SI Epidemiology of the Indication and Target Population · SII Non-clinical Part · SIII Clinical Trial Exposure · SIV Populations Not Studied in Clinical Trials · SV Post-authorisation Experience · SVI Additional EU Requirements · SVII Identified and Potential Risks · SVIII Summary of the Safety Concerns) · Part III Pharmacovigilance Plan · Part IV Plans for Post-authorisation Efficacy Studies · Part V Risk Minimisation Measures · Part VI Summary of the Risk Management Plan · Part VII Annexes

**`signal_eval` (GVP Module IX)**
Signal Identification and Source · Description of the Signal · Reference Safety Information Status · Case Series Review · Disproportionality and Database Findings · Clinical Trial Data · Literature Review · Biological Plausibility and Mechanism · Evaluation of Alternative Explanations and Confounding · Exposure and Estimated Reporting Rate · Assessment and Conclusion · Recommendation and Action Plan · References

**`pader` (21 CFR 314.80)** — Narrative Summary and Analysis · Index Line Listing of Reports · Copies of 15-Day Alert Reports Submitted in the Period · History of Actions Taken · Appendices

---

## 16. Acceptance criteria

1. Create product + report instance (`dsur`, defined interval and DLP, pinned IB version) → import E2B XML and a line listing → cases and events populate with source chips.
2. Ingest a narrative containing a patient name and a site name → both appear in the de-identification queue, are masked on accept, never appear in any chunk, embedding, model call, draft or export; the audit log records each masking decision.
3. An event's expectedness shows as a **suggestion with its basis**, is not counted anywhere until confirmed, and confirmation is blocked for a non-qualified-person role.
4. Add a case with a receipt date after the DLP → it appears in the case store but contributes to no interval or cumulative figure in any tabulation or draft.
5. Generate DSUR §7.3 → the draft contains `[TABLE: summary_tab_soc_pt]` and no LLM-typed counts; the rendered export shows the computed tabulation; clicking a cell drills to the contributing cases.
6. Change one event's confirmed seriousness in the grid → tabulation and every dependent figure update **without regenerating any section**; QC flags any prose figure now contradicting the table.
7. Generate the benefit-risk section with no sourced conclusion → draft contains `[ASSESSMENT REQUIRED: …]`, QC raises a blocker, export is blocked until a qualified-person-role user resolves it.
8. Create the next interval's report instance from the approved one → stable sections load as carried-forward baseline text with badges; data-driven sections are marked Changed; the delta summary lists new cases by SOC, new/closed signals, RSI changes and safety actions.
9. Export with tracked changes versus the baseline report → diff shows only genuinely changed sections; PII leakage scan passes; appendices render from the same builders as the body.
10. Delete the product → documents, chunks, vectors, case store, original un-masked store and drafts verifiably gone.

---

## 17. Build order — implement ONE milestone, then stop for my review

- **M1:** Data model + migrations + product/RSI CRUD + report-instance setup S1–S2 + dashboard card and calendar. Refactor shared services from CMC rather than duplicating.
- **M2:** Ingestion: E2B parser, line-listing column mapping, document upload, async pipeline, S3/S4 shell.
- **M3:** **De-identification pipeline + review queue.** Nothing downstream ships before this is solid.
- **M4:** MedDRA coding + expectedness suggestion engine + duplicate detection + case review grid S5, with roles enforced.
- **M5:** Interval/cumulative query layer + tabulation builders + exposure entry + S6, with drill-down.
- **M6:** Prose generation engine + workspace S7 (streaming, locked tables, baseline diff pane).
- **M7:** QC engine + PII leakage scan + S9 dashboard.
- **M8:** Export (report + appendices + tracked changes) + sign-off + audit surfacing.
- **M9:** Signal management S8 + disproportionality + remaining report types (`pbrer` variants, `pader`, `rmp`, `signal_eval`, `icsr_narrative`, `aco`, `lit_review`).

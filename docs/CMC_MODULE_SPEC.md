# Quality / CMC Module — Build Spec (received 2026-09-08)

The user-approved specification for the Quality/CMC generator. Built milestone by milestone;
progress lives in the roadmap memory. Deviations from this spec are listed at the top of the
commit that makes them.

> Guideline codes below (ICH M4Q, Q1A, Q1E, Q2, Q3x, Q6A, Q8-Q12, M7; EU GMP Annex 15;
> 21 CFR 211/Part 11) are **structural pointers only**. The regulatory affairs owner confirms the
> current revision of each before this module goes near a real submission. The product does not
> claim compliance.

## Why this is not "CSR for chemistry"

In a CSR, numbers are cited. In CMC, **numbers are the deliverable**. A specification limit, a
batch assay result, or a stability value that the model paraphrases, rounds, or invents is a
regulatory event, not a typo. Two flows that must never mix:

- **Flow A — prose sections**: RAG-grounded narrative, LLM-generated, cited (the CSR engine).
- **Flow B — data tables**: numeric content extracted into a normalised structured store,
  human-verified in a review grid, rendered into the document **deterministically by code**.

The LLM may *describe and interpret* a table. It may never *typeset* one.

## Non-negotiable design principles

1. **Multi-deliverable, not one document.** A document-type registry, never a hardcoded outline.
2. **No LLM-authored numbers.** Any value in a specification, batch analysis or stability table is
   read from the structured store and rendered deterministically. Prose referencing a value pulls
   it from the same store, so prose and table cannot disagree.
3. **Human verification of extracted data.** Every extracted numeric result carries an extraction
   confidence and `verified_by`. Nothing unverified reaches an export without an explicit override
   plus an audit entry.
4. **Grounded prose.** Product-specific facts come only from uploaded sources or entered metadata,
   each with an inline citation to document + page/table. Gaps output as `[DATA NEEDED: <what>]`.
5. **Human-in-the-loop.** Sections Draft -> In Review -> Approved; export requires approval. UI
   copy positions this as an AI-assisted drafting tool for CMC/regulatory writers, with a visible
   disclaimer.
6. **Lifecycle-aware.** Approved **baseline version** per section; a variation/change workflow that
   marks impacted sections and regenerates only those; tracked-changes export against baseline.
7. **Audit and data integrity.** Persist every generation, extraction, manual correction,
   verification, approval and export (actor, timestamp, before/after). Design toward ALCOA+ and
   21 CFR Part 11 direction: attributable, legible, contemporaneous, original, accurate,
   time-stamped, tamper-evident. Do not claim compliance; do not design it out.
8. **Confidentiality.** CMC sources contain trade secrets and CBI. Encrypt at rest, restrict to
   project members, support "referenced DMF - closed part, no data held" section states, full
   project purge, never used for model training.
9. **Truly async.** Ingestion, extraction and generation as real background jobs with status
   streaming; never synchronous behind a 202.
10. **Never rebuild DOCX from editor HTML.** Export assembles stored section content + rendered
    tables through template styles.

## Data model

**Project and structure**
- `cmc_project` - org_id, name, product_name, inn_or_ds_name, dosage_form, strengths[],
  route_of_administration, submission_type (IND/IMPD/NDA/ANDA/MAA/variation/other),
  target_regions[] (FDA/EMA/CDSCO/PMDA/HC/other), development_phase, baseline_version, status
- `cmc_site` - name, address, identifier (FEI/DUNS/other), activities[] (DS manufacture /
  DP manufacture / packaging / testing / release), gmp_evidence_document_id
- `cmc_deliverable` - doc_type_key, template_source (builtin | uploaded), storage_path, status
- `cmc_section` - section_code ("3.2.P.5.1"), title, sort_order, enabled,
  applicability (applicable | not_applicable | referenced_dmf), applicability_justification,
  guidance_text, status, baseline_draft_id
- `cmc_section_draft` - version, content, created_by (ai | user_id), model,
  generation_params_json (edits create versions; never overwrite)
- `cmc_citation` - marker, document_id, page, table_ref, chunk_id, cited_value

**Structured quality data (the core differentiator)**
- `cmc_material` - kind (drug_substance | drug_product | excipient | intermediate |
  packaging_component), name, grade, compendial_ref (USP/Ph.Eur./JP/BP + monograph), supplier,
  dmf_reference
- `cmc_batch` - material_id, batch_number, batch_size, batch_size_unit, manufacture_date, site_id,
  purpose (development/clinical/registration/process_validation/commercial/stability), scale,
  source_document_id
- `cmc_test` - material_id, test_name, method_id, method_type (compendial | in_house), unit,
  acceptance_criterion_text, limit_lower, limit_upper, limit_operator, spec_version_id,
  stage (release | shelf_life | in_process)
- `cmc_specification` - material_id, version, effective_date, source_document_id
- `cmc_result` - batch_id, test_id, storage_condition (25C/60RH, 30C/65RH, 40C/75RH, or null for
  release), timepoint_months (null for release), orientation (upright/inverted/horizontal),
  value_numeric, value_text, operator (=, <, >, ND, NMT, NLT), unit, source_document_id, page,
  table_ref, extraction_confidence, verified_by, verified_at
- `cmc_batch_formula` - deliverable_id, component_material_id, function, quantity_per_unit, unit,
  percent_ww, quantity_per_batch, reference_to_standard, source_document_id
- `cmc_change` - change_reference, description, change_type, impacted_section_ids[], status

**Ingestion / output**: `cmc_document`, `cmc_chunk`, `cmc_export`
(+ `cmc_document.doc_type`, `cmc_export.granularity` = ectd_leaves | combined | both).

Org + project-membership authorization on **every** endpoint.

## Output document types (registry — seeded)

| key | Deliverable | Structure basis |
|---|---|---|
| `ctd_32s` | CTD Module 3.2.S - Drug Substance | ICH M4Q |
| `ctd_32p` | CTD Module 3.2.P - Drug Product | ICH M4Q |
| `ctd_32ar` | CTD Module 3.2.A / 3.2.R - Appendices & Regional | ICH M4Q + region profile |
| `qos_23` | Quality Overall Summary (Module 2.3) | ICH M4Q - derived from approved 3.2.S/3.2.P |
| `apqr` | Annual Product Quality Review / PQR | EU GMP Ch.1, 21 CFR 211.180(e) |
| `method_val` | Analytical Method Validation Report | ICH Q2 |
| `process_val` | Process Validation Report | EU GMP Annex 15, FDA PV stages |
| `stability_report` | Stability Study Report | ICH Q1A, Q1E |

A project may hold several deliverables; they share one document set, one structured data store,
one audit trail.

## Source document types (tagging enum)

`spec_ds` / `spec_dp` / `spec_excipient` (specification) · `coa` (Certificate of Analysis) ·
`stability_protocol` · `stability_data` · `method_sop` · `method_val_report` · `bmr` (batch
manufacturing record) · `process_flow` · `pv_report` · `dev_report` · `characterisation` ·
`impurity_report` · `ccs` (container closure) · `ref_std` · `site_gmp` · `dmf` · `supplier_doc` ·
`deviation_capa` · `prior_dossier` (**baseline / style reference — never cite as current fact**) ·
`other`.

The required/recommended checklist shown in the UI is **driven by the selected deliverables**,
not fixed.

## Ingestion and structured extraction

**Stage 1 — parse & chunk** (shared engine): table-aware PDF, DOCX incl. tables, XLSX/CSV to
structured rows. Narrative chunks ~800-1,200 tokens with heading metadata. **Each table becomes
its own chunk**, prefixed with a header line (table id, title, batch/condition).

**Stage 2 — structured extraction (the important one).** A targeted pass over `coa`, `spec_*`,
`stability_data`, `bmr` populating `cmc_batch`, `cmc_test`, `cmc_specification`, `cmc_result`,
`cmc_batch_formula`:
- Extract **verbatim**: value string, operator, unit, exact source location. Store the raw string
  alongside the parsed numeric.
- Normalise units into a canonical unit per test but **retain the original**; never overwrite
  reported precision or significant figures.
- Preserve non-numeric results as-is: "Complies", "Conforms to reference", "ND", "NMT 0.1%",
  "Report result".
- Reconcile test names across documents against the specification's test list; unmatched names go
  to a **needs-mapping** queue, not silent discard.
- Assign `extraction_confidence`; anything below 0.9 is forced into the review grid.
- Deduplicate by (batch, test, condition, timepoint); conflicting values from different sources
  become a **conflict** for human resolution, never auto-merged.

**Stage 3 — embed & index** per project with metadata filters; cache by file hash.

**Extraction engine (user decision, 2026-09-08): deterministic first, AI fallback.** Table
structure is parsed by code and values copied verbatim at high confidence. The AI is used only to
map messy layouts to the right test/batch/timepoint — never to state a value. Anything the parser
cannot place goes to the review grid at low confidence.

## Screens

S0 dashboard card · S1 project setup + sites · S2 deliverable selection (built-in or uploaded
template; section tree with enable/disable and per-section applicability) · S3 tagged source
upload (+ optional material tag; checklist from selected deliverables; CBI banner) ·
S4 processing status (Queued -> Parsing -> Extracting -> Indexing -> Done/Failed, per-file retry,
partial-failure isolation) · **S5 data review grid** (Batches / Specifications / Release results /
Stability pivot / Batch formula; every cell shows value + confidence + source chip, click opens
the source snippet; Verify, Correct, Bulk-verify, Resolve conflict; "X of Y verified" hard gate) ·
S6 three-pane generation workspace (locked table blocks, "Insert data table", Sources / Data /
Issues tabs, streaming) · S7 QC dashboard (Blocker / Warning / Info) · S8 change & variation
workspace · S9 export.

## Generation engine — prose sections (Flow A)

Per section: resolve mapped doc_types -> build retrieval query from section_code + title +
guidance + product metadata + material -> retrieve top-k (12-20) filtered by
project/doc_type/material, boosting table chunks -> assemble context -> stream -> persist draft,
citations, audit.

### System prompt (verbatim; braces are template variables)

```
You are drafting section {section_code} "{section_title}" of a {deliverable_name} for a pharmaceutical
quality dossier, prepared to {structure_basis} conventions for submission to {target_regions}.

PRODUCT: {product_name} ({inn_or_ds_name}), {dosage_form}, {strengths}, {route_of_administration}.
Submission type: {submission_type}. Development phase: {development_phase}.

PRODUCT AND SITE METADATA:
{project_metadata_json}

VERIFIED QUALITY DATA AVAILABLE TO THIS SECTION (read-only, already rendered as tables in the document):
{verified_data_summary_json}

TEMPLATE GUIDANCE FOR THIS SECTION:
{section_guidance}

SOURCE EXTRACTS — the only permitted factual basis for narrative content:
{numbered_source_extracts}

RULES:
1. Use ONLY the source extracts, product metadata, and verified quality data above for product-specific
   facts. General regulatory and pharmaceutical knowledge may shape structure and phrasing, never content.
2. DO NOT WRITE DATA TABLES. Specification tables, batch analysis tables, stability tables and batch
   formulae are inserted automatically from verified data. Where such a table belongs, output the single
   line: [TABLE: <table_key>] and continue with the narrative.
3. Do not restate individual numeric results in prose unless the value appears in the verified quality
   data above; when you do, reproduce it exactly — same digits, same significant figures, same unit,
   same operator (NMT/NLT/ND/<//>) — and follow it with its citation [S#, p.X] or [S#, Table Y].
4. Never round, convert, average, extrapolate, or infer a value. Never state a trend, a shelf life, or a
   conformance conclusion that is not explicitly stated in the sources.
5. If information this section requires is absent, insert [DATA NEEDED: <exactly what is missing>].
   Never guess. Never omit silently.
6. Extracts marked "reference only" (previously approved dossiers) must never be cited as current fact.
7. Style: formal regulatory English, third person, no marketing language, no speculation. Use the present
   tense for descriptions of the manufacturing process, controls, and specifications as they currently
   stand; use the past tense for studies, validation exercises and batches already executed.
8. Refer to other sections by their CTD code (e.g. "see Section 3.2.P.5.1") only where the referenced
   section exists in this dossier.
9. Keep the exact section code and title given. Follow the subsection structure in the template guidance.
10. Output the heading, then the content. No markdown decoration beyond headings. No commentary about
    being an AI.
```

**Regeneration**: same pipeline, user instruction appended as an additional rule, saved as a new
version. **Model/cost**: strong model for drafting, cheap model for utility work. Model names, k,
thresholds and file limits live in config, not code.

## Table rendering and derived deliverables (Flow B)

Named table builders query the structured store and emit a styled table deterministically. Each
`[TABLE: key]` marker resolves at render time, so corrections in the Data Review grid propagate to
every deliverable **without regeneration**. Seed builders: `spec_table`, `batch_analyses`,
`stability_summary`, `stability_matrix`, `batch_formula`, `site_list`, `impurity_table`.

**Computed values are computed, not generated.** Shelf-life evaluation and trend statistics
(ICH Q1E-style regression, poolability, extrapolation) run in Python, are labelled with their
method and inputs, and are flagged **"computed — requires statistician review."** The LLM never
produces a statistical conclusion.

**QOS (Module 2.3)** generates only from **approved** 3.2.S/3.2.P sections, condensing without
introducing new facts, reusing the same rendered tables. Blocked while any feeding section is
unapproved.

## QC checks

**Blockers**: unverified data feeding an enabled section · specification conformance (OOS with
batch/test/condition/timepoint) · spec consistency across sections (limits, unit, method id
identical wherever a test appears) · method traceability (every method id has a procedure section
and a validation report or compendial reference) · batch formula arithmetic (components reconcile
to batch size, %w/w totals 100 within tolerance, per-unit x units-per-batch matches) ·
`[DATA NEEDED]` tracker · `[TABLE: key]` resolution.

**Warnings**: stability completeness matrix · out-of-trend detection (flag only) · citation
coverage · number-to-source verification · unit and significant-figure drift · batch number, site
and material name consistency · cross-reference validity · compendial claims flagged for human
verification (the system holds no monograph text) · region profile gaps.

**Info**: abbreviation builder · baseline diff summary.

## Export

Approved sections in CTD order, `[TABLE: ...]` resolved at render time, template styles, heading
levels per CTD numbering, TOC, page numbers, header with product name + "CONFIDENTIAL", optional
DRAFT watermark.

- **eCTD leaf granularity**: one file per CTD leaf with conventional naming
  (`32s1-gen-info.docx`, `32p5-contr-drug-prod.docx`) in a folder tree mirroring CTD, plus a
  manifest (section code -> filename). **Do not generate an eCTD backbone/XML** — that belongs to
  a publishing tool; the UI says so.
- **Combined document** option for internal review.
- **Tracked changes vs baseline** when a change record is active.
- PDF via DOCX conversion. Recorded in `cmc_export` + audit. Assembly works from stored content +
  rendered tables, **never** from editor HTML.

## Non-functional

500+ page development reports, stability bundles with 1,000+ tables; grids of 50+ batches x 30+
tests x 8 timepoints x 3 conditions must page and filter without freezing. Real background jobs
with retry/backoff, partial-failure isolation, streaming generation. Working downloads. Tests:
unit tests for unit normalisation, spec-limit comparison (operators and non-numeric results),
batch-formula arithmetic, table rendering, citation parsing, number-to-source matching; one
end-to-end happy path.

## Built-in section structures

**`ctd_32s` — Drug Substance**
S.1 General Information (S.1.1 Nomenclature, S.1.2 Structure, S.1.3 General Properties) ·
S.2 Manufacture (S.2.1 Manufacturer(s), S.2.2 Description of Manufacturing Process and Process
Controls, S.2.3 Control of Materials, S.2.4 Controls of Critical Steps and Intermediates,
S.2.5 Process Validation and/or Evaluation, S.2.6 Manufacturing Process Development) ·
S.3 Characterisation (S.3.1 Elucidation of Structure and other Characteristics, S.3.2 Impurities) ·
S.4 Control of Drug Substance (S.4.1 Specification, S.4.2 Analytical Procedures, S.4.3 Validation
of Analytical Procedures, S.4.4 Batch Analyses, S.4.5 Justification of Specification) ·
S.5 Reference Standards or Materials · S.6 Container Closure System ·
S.7 Stability (S.7.1 Stability Summary and Conclusions, S.7.2 Post-approval Stability Protocol and
Stability Commitment, S.7.3 Stability Data)

**`ctd_32p` — Drug Product**
P.1 Description and Composition · P.2 Pharmaceutical Development (P.2.1 Components — P.2.1.1 Drug
Substance, P.2.1.2 Excipients; P.2.2 Drug Product — P.2.2.1 Formulation Development,
P.2.2.2 Overages, P.2.2.3 Physicochemical and Biological Properties; P.2.3 Manufacturing Process
Development; P.2.4 Container Closure System; P.2.5 Microbiological Attributes;
P.2.6 Compatibility) · P.3 Manufacture (P.3.1 Manufacturer(s), P.3.2 Batch Formula,
P.3.3 Description of Manufacturing Process and Process Controls, P.3.4 Controls of Critical Steps
and Intermediates, P.3.5 Process Validation and/or Evaluation) · P.4 Control of Excipients
(P.4.1 Specifications, P.4.2 Analytical Procedures, P.4.3 Validation of Analytical Procedures,
P.4.4 Justification of Specifications, P.4.5 Excipients of Human or Animal Origin, P.4.6 Novel
Excipients) · P.5 Control of Drug Product (P.5.1 Specification(s), P.5.2 Analytical Procedures,
P.5.3 Validation of Analytical Procedures, P.5.4 Batch Analyses, P.5.5 Characterisation of
Impurities, P.5.6 Justification of Specification(s)) · P.6 Reference Standards or Materials ·
P.7 Container Closure System · P.8 Stability (P.8.1 Stability Summary and Conclusion,
P.8.2 Post-approval Stability Protocol and Stability Commitment, P.8.3 Stability Data)

**`ctd_32ar`** — A.1 Facilities and Equipment · A.2 Adventitious Agents Safety Evaluation ·
A.3 Novel Excipients · R Regional Information (region-specific item list per target region)

**`apqr`** — Scope and Period · Products and Batches Covered · Starting Materials and Packaging ·
In-Process Controls and Finished Product Results (with trends) · Out-of-Specification Results and
Investigations · Deviations and Non-conformances · CAPA Status · Change Controls · Stability
Results and Trends · Returns, Complaints and Recalls · Qualification and Validation Status ·
Technical Agreements Review · Regulatory Commitments and Variations · Conclusions and
Recommendations

**`method_val`** — Purpose and Scope · Method Summary · Materials, Equipment and Reference
Standards · Validation Parameters (Specificity, Linearity and Range, Accuracy, Precision —
Repeatability and Intermediate Precision, Detection and Quantitation Limits, Robustness, Solution
Stability, System Suitability) · Results and Discussion · Deviations · Conclusion

**`process_val`** — Purpose and Scope · Process Description and Flow · Critical Quality Attributes
and Critical Process Parameters · Sampling Plan and Acceptance Criteria · Equipment and Facility
Qualification Status · Batch Results per Validation Batch · Statistical Evaluation · Deviations and
Investigations · Continued Process Verification Plan · Conclusion

**`stability_report`** — Objective and Scope · Study Design (batches, conditions, orientations,
timepoints, pull schedule) · Test Methods and Acceptance Criteria · Results by Condition and
Timepoint · Trend Evaluation · Out-of-Specification / Out-of-Trend Investigations · Shelf-life /
Retest Period Evaluation · Post-approval Stability Commitment · Conclusion

## Acceptance criteria

1. Create project -> select `ctd_32p` -> upload spec, three CoAs, stability data, BMR (tagged) ->
   all reach Done, Data Review grid populated with batches, tests and results carrying source chips.
2. Correct one extracted stability value; audit records original -> corrected -> actor, and the
   rendered `stability_summary` in P.8.3 updates **without regenerating the section**.
3. Generate P.5.1: draft contains `[TABLE: spec_table]` and no LLM-typed specification numbers;
   the export contains the table built from verified data.
4. A result exceeding its acceptance criterion -> OOS blocker naming batch, test, condition,
   timepoint; export blocked until override.
5. Change a limit so S.4.1 and S.4.4 disagree -> spec-consistency blocker.
6. Remove the method validation report, generate P.5.3 -> `[DATA NEEDED: ...]`, QC lists it.
7. Approve all 3.2.P sections -> freeze baseline -> generate `qos_23` reusing the same tables,
   introducing no fact absent from the approved sections.
8. Change record affecting P.3.3 -> regenerate -> export with tracked changes shows only P.3.3.
9. Export with eCTD leaf granularity -> correct folder tree, per-leaf filenames, manifest, styles,
   TOC, header.
10. Delete the project -> files, chunks, vectors and structured data verifiably gone.

## Build order

M1 data model + migrations + project/site CRUD + S1-S2 with built-in `ctd_32s`/`ctd_32p`,
refactoring shared services rather than duplicating · M2 uploads + tagging + async parse/chunk/
index + S3/S4 · M3 structured extraction + Data Review grid S5 · M4 table renderer +
`[TABLE: key]` resolution + preview endpoint · M5 prose generation + workspace S6 · M6 QC engine +
S7 · M7 export (eCTD leaves + combined + tracked changes) + audit surfacing · M8 QOS derivation,
change/variation workspace S8, remaining deliverable types, custom template parsing.

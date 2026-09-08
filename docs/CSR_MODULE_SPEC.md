# CSR Generation Module — Build Spec (received 2026-09-08)

The user-approved specification for the Clinical Study Report generator.
Built milestone-by-milestone (M1–M6), one per review cycle. Progress lives in
the roadmap memory; deviations from this spec are listed at the top of each
milestone's commit message.

## Non-negotiable design principles
1. Section-wise generation only — never the full CSR in one LLM call; ICH E3 order.
2. Grounded output — facts only from uploaded sources or study metadata; every number
   carries an inline citation [S#, p.X]/[S#, Table Y]; gaps become
   [DATA NEEDED: ...] — never invented, never silently skipped.
3. Human-in-the-loop — Draft -> In Review -> Approved; export requires approval;
   positioned as an AI-assisted drafting tool for medical writers, with disclaimer.
4. Project isolation — per-project document set and vector namespace; retrieval
   never crosses projects.
5. Auditability — persist every generation event (section, model, prompt version,
   retrieved chunk ids, user, timestamp) and every edit/approval/export; 21 CFR
   Part 11 direction (attributable, time-stamped, tamper-evident), no compliance claim.
6. Privacy — patient-level data possible; restrict to project members, upload warning
   recommending de-identified sources, full purge on delete (files+chunks+vectors+drafts),
   never used for training.
7. Truly async — ingestion and generation as real background jobs with status
   polling/streaming; never synchronous behind a 202.
8. Never rebuild DOCX from editor HTML — structured section content + template styles.

## Data model (adapted names)
csr_projects (extension of a portal Project; study identity from the studies book),
csr_documents, csr_templates, csr_sections, csr_section_drafts (every version kept;
edits are versions), csr_citations, csr_chunks (pgvector), csr_audit (or shared audit),
csr_exports. Org+project authorization on every endpoint.

## Source doc types
protocol* , sap*, tlf* (required); narrative, ib, icf (recommended);
crf, randomization, prior_csr (style reference ONLY — never citable as fact), other.

## Section->source default mapping (editable JSON)
2: protocol,sap,tlf · 5: protocol,icf · 6: protocol · 7: protocol,ib · 8: protocol ·
9.1–9.6: protocol (9.4.6 +randomization) · 9.7: sap · 9.8: protocol,sap ·
10: tlf,protocol · 11: sap,tlf · 12: tlf,narrative · 13: protocol,sap,tlf + approved
drafts of 11 & 12 · 14: tlf.

## Screens
S0 dashboard entry · S1 study details · S2 template (builtin ICH E3 | uploaded DOCX
with parsed heading tree + enable/disable, no reordering) · S3 tagged multi-upload with
required-type checklist + patient-data banner · S4 per-file processing status with retry ·
S5 three-pane workspace (section tree | streaming editor with versions+status | Sources
and Issues tabs, citation<->chunk cross-highlight) · S6 QC + export panel.

## Ingestion
PDF text (PyMuPDF), tables table-aware (TLFs never flattened to prose), RTF (SAS TLF)
converted then parsed, DOCX incl. tables, XLSX/CSV structured. Narrative chunks
800–1200 tok w/ overlap + {doc_type,page,heading}; TLF: ONE chunk per table with header
line (table number, title, population). Embed+index per-project pgvector with metadata
filters; cache by file_hash. Granular status updates.

## Generation engine
Per section: resolve mapped doc_types, retrieval query from number+title+guidance+
metadata, top-k 12–20 project+type-filtered (boost matching TLF table ranges),
context = metadata JSON + guidance + numbered extracts [S1..]; drafting model streams;
persist draft + citations + audit. Regenerate = same pipeline + user instruction as an
extra rule, ALWAYS a new version. Strong model drafts; cheap model for utility tasks.
Config (not code): model names, k, token budgets, file limits. On overflow keep table
chunks + highest-similarity narrative chunks.

### System prompt (verbatim; braces are variables)
You are drafting Section {section_number} "{section_title}" of a Clinical Study Report (ICH E3) for:
Study {study_id} — {compound_name}, Phase {phase}, {indication}. Sponsor: {sponsor}.

STUDY METADATA:
{study_metadata_json}

TEMPLATE GUIDANCE FOR THIS SECTION:
{section_guidance}

SOURCE EXTRACTS — the only permitted factual basis:
{numbered_source_extracts}

RULES:
1. Use ONLY the source extracts and study metadata above for study-specific facts. General regulatory-writing knowledge may shape style, never content.
2. Every number, percentage, dose, count, p-value, confidence interval, or date must be immediately followed by its citation: [S#, p.X] or [S#, Table Y].
3. If information this section requires is not present in the sources, insert [DATA NEEDED: <exactly what is missing>]. Never guess. Never omit silently.
4. Extracts marked "style reference only" must never be cited as fact.
5. Style: formal regulatory English; past tense for study conduct and results; third person; no marketing language; no speculation.
6. Keep the exact heading number and title given. Follow the subsection structure in the template guidance.
7. Prefer the SAP's wording for statistical methods and the protocol's wording for design elements. Refer to statistical outputs as "Table/Listing/Figure <id>" only when that id appears in the sources; otherwise write [DATA NEEDED: table reference].
8. Output the section heading, then the prose. No markdown decoration beyond headings. No commentary about being an AI.

## QC (mandatory)
1 citation coverage for every numeric token · 2 number-to-source verification
(normalized) · 3 cross-section N consistency (screened/enrolled/randomized/treated/
completed/discontinued) · 4 [DATA NEEDED] tracker blocking export (override =
confirm + audit) · 5 abbreviation builder feeding CSR Section 4.

## Export
Enabled+approved sections in E3 order -> DOCX from stored structured content + template
styles (TOC, heading levels, page numbers, study-ID+CONFIDENTIAL header, optional DRAFT
watermark; citation tags keep/strip/appendix) -> PDF via LibreOffice. Export gated on
approval (override audited). Never from editor HTML.

## API
POST/GET /csr/projects · GET/DELETE /csr/projects/{id} (full purge) ·
POST .../template · GET .../sections · POST .../documents (+PATCH doc_type, DELETE) ·
POST .../process · GET .../processing-status · POST /csr/sections/{id}/generate ·
GET/PUT /csr/sections/{id}/draft · PATCH /csr/sections/{id}/status ·
GET .../qc · POST .../export · GET exports · GET .../audit

## Non-functional
300+ page protocols, 500+ page / 1000+ table TLF bundles; retries/backoff;
partial-failure isolation; working downloads; unit tests for chunking, citation
parsing, QC number-matching; one e2e happy path.

## Acceptance criteria
(1) protocol+SAP+TLF indexed · (2) 10.1 cites disposition tables, marker<->chunk
highlight · (3) no SAP -> 9.7 has [DATA NEEDED], export blocked until override ·
(4) edit->new version, approve all, DOCX with styles/TOC/header, audit complete ·
(5) altered number -> mismatch flag · (6) delete -> files/chunks/vectors verifiably gone.

## Milestones
M1 data model + migrations + project CRUD + wizard S1–S2 + builtin ICH E3 ·
M2 uploads/tagging/async ingestion + S3/S4 · M3 generation engine + workspace S5 ·
M4 QC engine + Issues/S6 · M5 export + audit surfacing · M6 custom template parsing +
mapping editor + polish.

## ICH E3 built-in structure
1 Title Page · 2 Synopsis · 3 ToC · 4 Abbreviations · 5 Ethics (5.1 IEC/IRB, 5.2
Ethical Conduct, 5.3 Patient Information and Consent) · 6 Investigators/Admin
Structure · 7 Introduction · 8 Objectives · 9 Investigational Plan (9.1 Overall
Design; 9.2 Discussion of Design; 9.3 Population: 9.3.1 Inclusion / 9.3.2 Exclusion /
9.3.3 Removal; 9.4 Treatments: 9.4.1 Administered / 9.4.2 Identity / 9.4.3 Assignment /
9.4.4 Dose Selection / 9.4.5 Dose Timing / 9.4.6 Blinding / 9.4.7 Prior+Concomitant /
9.4.8 Compliance; 9.5 Efficacy and Safety Variables; 9.6 Data Quality Assurance;
9.7 Statistical Methods and Sample Size; 9.8 Changes in Conduct or Planned Analyses) ·
10 Study Patients (10.1 Disposition, 10.2 Protocol Deviations) · 11 Efficacy (11.1
Data Sets; 11.2 Demographics/Baseline; 11.3 Compliance; 11.4 Results: 11.4.1 Analysis /
11.4.2 Statistical Issues / 11.4.3 Individual Response / 11.4.4 Dose–Response /
11.4.5 Interactions / 11.4.6 By-Patient Displays / 11.4.7 Conclusions) · 12 Safety
(12.1 Exposure; 12.2 AEs: 12.2.1 Summary / 12.2.2 Display / 12.2.3 Analysis /
12.2.4 Listings; 12.3 Deaths, SAEs, Significant AEs; 12.4 Laboratory; 12.5 Vitals;
12.6 Safety Conclusions) · 13 Discussion and Overall Conclusions · 14 Tables/Figures
Not in Text · 15 References · 16 Appendices

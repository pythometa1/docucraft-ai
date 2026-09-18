"""Section structures for the four deliverables that are not CTD modules.

Seed data in the same shape as `app.cmc.ctd`: tuples of (section_code, title,
guidance, is_container), where a container is a heading whose prose lives in
its children and only a leaf ever holds a draft. The guidance is written for
two readers at once -- the person deciding which sections apply to this
product, and the drafting prompt, which is handed it verbatim. It says what
the section must contain, never how to phrase it.

They live here rather than in `ctd.py` because their numbering is not ICH
M4Q's. A reviewer hunting 3.2.P.5.1 must find exactly that code, so the CTD
file has to stay a transcription of the guideline; an annual review numbered
1..14 by house convention would look like one if it sat beside it. The APQR
in particular carries plain integers because EU GMP Chapter 1 and
21 CFR 211.180(e) give it no numbering at all, and a dotted code invented
here would assert a dossier location the document does not have.

`TABLE_SECTIONS` names the sections whose numbers are rendered from the
verified structured store rather than written: the section still keeps its
guidance, because the drafter still has to say what the table is of, but the
figures come from `cmc_results` and its neighbours and reach the page as
their stored strings. The restraint runs the other way too, and is the
reason several obviously tabular sections have no builder here: only
`registry.STRUCTURED_TYPES` are read into that store, so validation
parameters, deviation logs and CAPA registers hold no verified numbers. A
builder pointed at them would render an empty or invented table, where prose
with citations reports honestly what the source document says.

`SOURCE_MAPS` is section-code prefix -> the doc types that section may
retrieve from, longest-prefix-wins as applied by `registry.source_types_for`.
A section with no entry reads every citable type, which is the honest default
for a conclusion that draws on the whole study. `prior_dossier` never appears:
a previously approved dossier is a style reference and evidence for nothing.

A caller seeding one of these trees through `ctd.seed_sections` gets its
table keys from `ctd.DATA_SECTIONS`, which holds CTD codes only, so it must
pair the tree with its entry here (as `registry.table_key_for` does) or every
one of these sections seeds with no table.
"""

# ------------------------------------------------------------------ APQR

#: (section_code, title, guidance, is_container)
APQR = (
    ("1", "Scope and Period",
     "The product, strengths and presentations this review covers, the review period "
     "with its start and end dates, the review it follows, and the standard the review "
     "is performed against.", False),
    ("2", "Products, Sites and Batches Covered",
     "Each manufacturing, packaging and testing site involved during the period with its "
     "role, and the batches manufactured, released, rejected and reworked, with the "
     "batch numbers and the total for each category.", False),
    ("3", "Starting Materials and Packaging",
     "The starting materials and packaging components used, their approved suppliers and "
     "any change of supplier, the results of incoming testing, and any supplier quality "
     "issue raised during the period.", False),
    ("4", "In-Process Controls and Finished Product Results",
     "In-process control results and finished product release results for the period, per "
     "test, with the trend across batches and any test showing a shift or a drift against "
     "the previous period.", False),
    ("5", "Out-of-Specification Results and Investigations",
     "Every out-of-specification and out-of-trend result in the period: the batch, test, "
     "result and acceptance criterion, the investigation conclusion and assigned root "
     "cause, and the disposition of the affected batch.", False),
    ("6", "Deviations and Non-conformances",
     "The deviations, non-conformances and quality incidents recorded in the period, "
     "classified by criticality, with their investigation outcomes and any recurrence of "
     "an issue seen in an earlier period.", False),
    ("7", "CAPA Status",
     "Corrective and preventive actions raised, closed and still open at the end of the "
     "period, their effectiveness checks, and the reason and revised date for any action "
     "past its due date.", False),
    ("8", "Change Controls",
     "Changes to the product, process, equipment, materials, specifications and analytical "
     "procedures approved in the period, whether each required notification to or approval "
     "by an authority, and its effect on validated and qualified status.", False),
    ("9", "Stability Results and Trends",
     "The stability studies running or completed in the period, their results by storage "
     "condition and timepoint, the trends observed, and whether the approved shelf life "
     "and storage statement remain supported.", False),
    ("10", "Returns, Complaints and Recalls",
     "Product returns, quality complaints and recalls in the period: number and category, "
     "investigation outcome, and any trend pointing to a product, process or packaging "
     "cause.", False),
    ("11", "Qualification and Validation Status",
     "The qualification status of premises, utilities and equipment, and the validation "
     "status of the manufacturing process, cleaning procedures and analytical methods, "
     "including anything due for requalification or revalidation.", False),
    ("12", "Technical Agreements Review",
     "The technical and quality agreements in place with contract manufacturers, contract "
     "laboratories and suppliers: whether each is current for the activities actually "
     "performed, and any that require revision.", False),
    ("13", "Regulatory Commitments and Variations",
     "Variations submitted, approved and pending in the period, the post-approval "
     "commitments made to each authority, and the status of each against its due date.", False),
    ("14", "Conclusions and Recommendations",
     "The conclusion on the consistency of the process and the quality of the product, "
     "whether the specifications, process and controls remain adequate, and the actions "
     "recommended with their owners and target dates.", False),
)

# ------------------------------------------------- analytical method validation

METHOD_VALIDATION = (
    ("1", "Purpose and Scope",
     "The analytical procedure validated, with its identifier and version, the material or "
     "product and the test it is used for, and the validation characteristics required for "
     "that type of procedure under ICH Q2.", False),
    ("2", "Method Summary",
     "A summary of the procedure: its principle, the instrumentation and detection used, "
     "the chromatographic or other separation conditions, standard and sample preparation, "
     "and the tests and acceptance criteria it supports.", False),
    ("3", "Materials, Equipment and Reference Standards",
     "The samples, placebos, reagents, columns and instruments used, and the reference "
     "standards with their source, lot number, assigned potency and expiry or requalification "
     "date.", False),

    ("4", "Validation Parameters", "", True),
    ("4.1", "Specificity",
     "Evidence that the procedure measures the analyte free of interference from the "
     "placebo, the known impurities, the degradation products and the diluent, including "
     "peak purity and forced degradation results where the procedure is stability "
     "indicating.", False),
    ("4.2", "Linearity and Range",
     "The concentration levels prepared, the regression obtained with its correlation "
     "coefficient, slope, intercept and residuals, and the range over which the procedure "
     "is demonstrated to be linear, accurate and precise.", False),
    ("4.3", "Accuracy",
     "Recovery at each level tested, the number of determinations at each, the mean "
     "recovery and its variability, against the acceptance criteria set before the study.", False),
    ("4.4", "Precision", "", True),
    ("4.4.1", "Repeatability",
     "Precision under the same operating conditions over a short interval: the "
     "determinations made, at what concentrations, and the relative standard deviation "
     "obtained.", False),
    ("4.4.2", "Intermediate Precision",
     "Precision across the variations expected within the laboratory -- different days, "
     "analysts, instruments and columns -- with the variability contributed by each and "
     "the combined result.", False),
    ("4.5", "Detection and Quantitation Limits",
     "The detection and quantitation limits, how each was determined (signal-to-noise, the "
     "standard deviation of the response and the slope, or visual evaluation), and the "
     "confirmation of the quantitation limit by accuracy and precision at that level.", False),
    ("4.6", "Robustness",
     "The deliberate variations made to procedure parameters, the effect of each on the "
     "result and on system suitability, and the parameters that consequently require a "
     "stated control in the procedure.", False),
    ("4.7", "Solution Stability",
     "The stability of standard, sample and mobile phase solutions over the storage "
     "conditions and intervals tested, and the hold times the procedure states as a "
     "result.", False),
    ("4.8", "System Suitability",
     "The system suitability tests and their acceptance criteria, and the evidence that "
     "they discriminate a system fit for use from one that is not.", False),

    ("5", "Results and Discussion",
     "The result obtained for each validation characteristic against its acceptance "
     "criterion, in the order the parameters are presented, with discussion of anything "
     "that did not meet its criterion at the first attempt.", False),
    ("6", "Deviations",
     "Deviations from the validation protocol, their assessment, and their effect on the "
     "validity of the results. Where there were none, say so explicitly.", False),
    ("7", "Conclusion",
     "Whether the procedure is validated for its intended use, the range and matrix over "
     "which the conclusion holds, and any restriction or condition placed on its use.", False),
)

# ------------------------------------------------------- process validation

PROCESS_VALIDATION = (
    ("1", "Purpose and Scope",
     "The product, strengths and batch sizes validated, the sites and lines involved, the "
     "validation approach taken (traditional, continuous process verification or hybrid), "
     "and the approved protocol this report executes.", False),

    ("2", "Process Description", "", True),
    ("2.1", "Manufacturing Sites and Facilities",
     "Each site, area and line used for the validation batches, with its address, its role "
     "in manufacture, packaging and testing, and its GMP status.", False),
    ("2.2", "Batch Formula and Batch Size",
     "The batch formula at the validated batch size: every component, its quantity per "
     "batch, its function and its quality standard, including components removed during "
     "processing.", False),
    ("2.3", "Manufacturing Process and Process Flow",
     "The process as executed for the validation batches: the sequence of unit operations, "
     "the equipment used at each, the operating conditions and set points, and the "
     "in-process controls, given as a flow diagram and a narrative.", False),

    ("3", "Critical Quality Attributes and Critical Process Parameters",
     "The critical quality attributes with their targets, the critical process parameters "
     "with their proven acceptable ranges, the link from each parameter to the attribute it "
     "controls, and the risk assessment the selection came from.", False),
    ("4", "Sampling Plan and Acceptance Criteria",
     "Where, when and how many samples were taken at each stage, the tests performed on "
     "them, and the acceptance criteria the validation was judged against, which were fixed "
     "before the batches were manufactured.", False),
    ("5", "Equipment and Facility Qualification Status",
     "The qualification status of the equipment, utilities and facilities used: the IQ, OQ "
     "and PQ documents and their approval dates, and confirmation that the calibration of "
     "critical instruments was current for the duration of the batches.", False),
    ("6", "Batch Results per Validation Batch",
     "The results for each validation batch: batch number, size, date and site of "
     "manufacture, in-process and finished product results against their acceptance "
     "criteria, and the yield at each stage.", False),
    ("7", "Statistical Evaluation",
     "The statistical evaluation of the results: the method applied, the inputs it was "
     "applied to, the variability within and between batches, and the process capability "
     "where one was calculated, stated so that the conclusion can be re-derived rather than "
     "taken on trust.", False),
    ("8", "Deviations and Investigations",
     "Deviations that occurred during the validation batches, their investigation and their "
     "effect on the conclusion of the study. Where there were none, say so explicitly.", False),
    ("9", "Continued Process Verification Plan",
     "The ongoing monitoring after validation: the attributes and parameters trended, the "
     "frequency and sample size, the control limits used, and the criteria that trigger "
     "investigation or revalidation.", False),
    ("10", "Conclusion",
     "Whether the process is validated for the stated batch size, sites and equipment, the "
     "conditions the conclusion depends on, and any action outstanding before routine "
     "manufacture.", False),
)

# ------------------------------------------------------- stability study report

STABILITY_REPORT = (
    ("1", "Objective and Scope",
     "The purpose of the study, the material or product with its strengths and "
     "presentations, the packaging on study, and the ICH storage conditions and climatic "
     "zones the study is designed against.", False),

    ("2", "Study Design", "", True),
    ("2.1", "Batches on Study",
     "The batches placed on stability: batch number, size, date and site of manufacture, "
     "the drug substance batch used, and how each relates to the commercial process and "
     "formulation.", False),
    ("2.2", "Storage Conditions, Orientations and Pull Schedule",
     "The storage conditions with their tolerances, the orientations stored, the timepoints "
     "scheduled at each condition, and any bracketing or matrixing design applied with its "
     "justification.", False),
    ("2.3", "Container Closure System and Packaging Configurations",
     "Each packaging configuration on study: the primary container and closure with their "
     "materials of construction, the fill count or fill volume, and any secondary packaging "
     "that contributes protection.", False),

    ("3", "Test Methods and Acceptance Criteria",
     "The tests performed at each timepoint, the analytical procedures used with their "
     "identifiers, which of them are stability indicating, and the acceptance criteria "
     "applied through shelf life where these differ from the release criteria.", False),
    ("4", "Results by Condition and Timepoint",
     "The results obtained, by batch, storage condition, orientation and timepoint, in the "
     "units and to the significant figures the analytical report gives them.", False),
    ("5", "Trend Evaluation",
     "The change over time in each attribute that changes: its direction and size at each "
     "condition, the comparison between batches, and any statistical analysis performed "
     "under ICH Q1E, with the method and its inputs.", False),
    ("6", "Out-of-Specification and Out-of-Trend Investigations",
     "Every result outside its acceptance criterion or outside the trend of its batch: the "
     "batch, condition, orientation and timepoint, the investigation and its conclusion, "
     "and the effect on the study. Where there were none, say so explicitly.", False),
    ("7", "Shelf-life or Retest Period Evaluation",
     "The shelf life or retest period proposed and the storage statement it carries, the "
     "data and analysis supporting it, and any extrapolation beyond the observed data with "
     "its extent and its justification under ICH Q1E.", False),
    ("8", "Post-approval Stability Protocol and Commitment",
     "The protocol for continuing stability: the batches to be placed on study each year, "
     "the conditions, timepoints and tests, and the commitment to complete the studies begun "
     "and to begin studies on production batches.", False),
    ("9", "Conclusion",
     "Whether the data support the proposed shelf life, storage conditions and any in-use "
     "period, the conditions the conclusion rests on, and any action arising from the "
     "study.", False),
)

#: Registry key -> tree, for the deliverables `registry.DELIVERABLES` carries
#: with an empty tree.
TREES = {
    "apqr": APQR,
    "method_val": METHOD_VALIDATION,
    "process_val": PROCESS_VALIDATION,
    "stability_report": STABILITY_REPORT,
}

#: Registry key -> section code -> the builder that renders that section's
#: table. The drafting prompt for such a section is told to emit
#: `[TABLE: <key>]` and to write no numbers of its own, so the figures reach
#: the page as the strings the source printed. Only sections whose numbers
#: actually exist in the structured store appear: the rest are prose with
#: citations, which is the truthful rendering of a number nobody verified.
TABLE_SECTIONS = {
    "apqr": {
        "2": "site_list",
        "3": "composition_table",
        "4": "batch_analyses",
        "9": "stability_summary",
    },
    "method_val": {
        # The validation parameters have no builder on purpose. A method
        # validation report is not a structured source, so its recoveries and
        # RSDs are cited prose; what is verified data here is the set of tests
        # and acceptance criteria the procedure exists to serve.
        "2": "spec_table",
    },
    "process_val": {
        "2.1": "site_list",
        "2.2": "batch_formula",
        "4": "spec_table",
        "6": "batch_analyses",
    },
    "stability_report": {
        # "Batches on Study" gets no builder. What it must state -- batch size,
        # manufacture date, site, the drug substance batch used -- is provenance
        # that no builder renders, and `batch_analyses` would put a grid of
        # release results under that heading and then refuse to export the whole
        # report on a study whose batches carry stability results and no release
        # row. The initial results belong to section 4, where the study reports
        # them by timepoint.
        "2.2": "stability_matrix",
        "3": "spec_table",
        "4": "stability_summary",
    },
}

#: Registry key -> section code prefix -> doc types that section may retrieve
#: from. Longest-prefix-wins is applied by `registry.source_types_for`, so a
#: parent entry covers its children unless a child overrides it, and a section
#: with no entry anywhere above it retrieves from every citable type.
SOURCE_MAPS = {
    "apqr": {
        "1": ["site_gmp", "spec_dp"],
        "2": ["site_gmp", "bmr", "coa"],
        "3": ["spec_excipient", "supplier_doc", "dmf", "coa"],
        "4": ["coa", "spec_dp", "bmr"],
        "5": ["coa", "deviation_capa", "spec_dp"],
        "6": ["deviation_capa", "bmr"],
        "7": ["deviation_capa"],
        "8": ["deviation_capa", "dev_report", "other"],
        "9": ["stability_data", "stability_protocol"],
        "10": ["deviation_capa", "other"],
        "11": ["pv_report", "method_val_report", "site_gmp"],
        "12": ["supplier_doc", "site_gmp", "other"],
        "13": ["other", "site_gmp"],
    },
    "method_val": {
        "1": ["method_val_report", "method_sop"],
        "2": ["method_sop", "spec_ds", "spec_dp", "method_val_report"],
        "3": ["method_sop", "ref_std", "supplier_doc", "method_val_report"],
        "4": ["method_val_report"],
        "5": ["method_val_report"],
        "6": ["method_val_report", "deviation_capa"],
        "7": ["method_val_report"],
    },
    "process_val": {
        "1": ["pv_report", "dev_report"],
        "2": ["bmr", "process_flow", "pv_report"],
        "2.1": ["site_gmp", "bmr"],
        "2.2": ["bmr"],
        "3": ["dev_report", "pv_report", "spec_dp"],
        "4": ["pv_report", "spec_dp", "bmr"],
        "5": ["site_gmp", "pv_report"],
        "6": ["coa", "bmr", "pv_report"],
        "7": ["pv_report", "coa"],
        "8": ["deviation_capa", "pv_report"],
        "9": ["pv_report", "coa", "spec_dp"],
        "10": ["pv_report"],
    },
    "stability_report": {
        "1": ["stability_protocol", "stability_data"],
        "2": ["stability_protocol", "bmr"],
        "2.1": ["bmr", "coa", "stability_protocol"],
        "2.2": ["stability_protocol", "stability_data"],
        "2.3": ["ccs", "stability_protocol"],
        "3": ["spec_dp", "spec_ds", "method_sop", "stability_protocol"],
        "4": ["stability_data"],
        "5": ["stability_data"],
        "6": ["stability_data", "deviation_capa"],
        "7": ["stability_data", "stability_protocol"],
        "8": ["stability_protocol"],
        "9": ["stability_data", "stability_protocol"],
    },
}

"""The built-in section structures, from §15 of the module spec.

Data, not code. A structure is a tuple of `(code, title)` pairs in document
order; depth is read from the code, and a section with children is a container
that holds no text of its own. Codes are the ones the guideline uses, because
a reviewer reads "PBRER 16.2" and has to be able to find it in the tree
without translating.

Three lookups sit beside the trees and are the interesting part:

`TABLE_KEYS` says which sections carry a computed table. That mapping is what
makes §2's second principle enforceable rather than aspirational -- the LLM
never writes a tabulation because the section that would contain one declares
its table here, the drafting prompt is told to emit `[TABLE: key]` and nothing
else, and the export refuses a data section whose marker has gone missing.

`SOURCE_TYPES` says which uploaded document types a section may retrieve from.
A non-clinical section that could reach a sales report is a section that can
cite one.

`GUIDANCE` says what the section is for, in the words the drafting prompt will
carry. Where §15 gives no specific instruction the title is the guidance; where
the spec's own rules bear on a section -- benefit-risk needs
`[ASSESSMENT REQUIRED]` rather than a conclusion, interval and cumulative
figures must never merge -- the guidance says so at the point of use, because a
rule stated only in the system prompt is a rule competing with eighteen other
sentences for the model's attention.
"""

# --------------------------------------------------------------------- trees

#: ICH E2C(R2) / GVP Module VII.
PBRER = (
    ("1", "Introduction"),
    ("2", "Worldwide Marketing Approval Status"),
    ("3", "Actions Taken in the Reporting Interval for Safety Reasons"),
    ("4", "Changes to Reference Safety Information"),
    ("5", "Estimated Exposure and Use Patterns"),
    ("5.1", "Cumulative Subject Exposure in Clinical Trials"),
    ("5.2", "Cumulative and Interval Patient Exposure from Marketing Experience"),
    ("6", "Data in Summary Tabulations"),
    ("6.1", "Reference Information"),
    ("6.2", "Cumulative Summary Tabulations of Serious Adverse Events from Clinical Trials"),
    ("6.3", "Cumulative and Interval Summary Tabulations from Post-Marketing Data Sources"),
    ("7", "Summaries of Significant Findings from Clinical Trials during the Reporting Interval"),
    ("7.1", "Completed Clinical Trials"),
    ("7.2", "Ongoing Clinical Trials"),
    ("7.3", "Long-term Follow-up"),
    ("7.4", "Other Therapeutic Use"),
    ("7.5", "New Safety Data Related to Fixed Combination Therapies"),
    ("8", "Findings from Non-interventional Studies"),
    ("9", "Information from Other Clinical Trials and Sources"),
    ("10", "Non-clinical Data"),
    ("11", "Literature"),
    ("12", "Other Periodic Reports"),
    ("13", "Lack of Efficacy in Controlled Clinical Trials"),
    ("14", "Late-Breaking Information"),
    ("15", "Overview of Signals: New, Ongoing or Closed"),
    ("16", "Signal and Risk Evaluation"),
    ("16.1", "Summary of Safety Concerns"),
    ("16.2", "Signal Evaluation"),
    ("16.3", "Evaluation of Risks and New Information"),
    ("16.4", "Characterisation of Risks"),
    ("16.5", "Effectiveness of Risk Minimisation"),
    ("17", "Benefit Evaluation"),
    ("17.1", "Important Baseline Efficacy and Effectiveness Information"),
    ("17.2", "Newly Identified Information on Efficacy and Effectiveness"),
    ("17.3", "Characterisation of Benefits"),
    ("18", "Integrated Benefit-Risk Analysis for Approved Indications"),
    ("18.1", "Benefit-Risk Context"),
    ("18.2", "Benefit-Risk Analysis Evaluation"),
    ("19", "Conclusions and Actions"),
    ("20", "Appendices"),
)

#: ICH E2F.
DSUR = (
    ("1", "Introduction"),
    ("2", "Worldwide Marketing Approval Status"),
    ("3", "Actions Taken in the Reporting Period for Safety Reasons"),
    ("4", "Changes to Reference Safety Information"),
    ("5", "Inventory of Clinical Trials Ongoing and Completed during the Reporting Period"),
    ("6", "Estimated Cumulative Exposure"),
    ("6.1", "Cumulative Subject Exposure in the Development Programme"),
    ("6.2", "Patient Exposure from Marketing Experience"),
    ("7", "Data in Line Listings and Summary Tabulations"),
    ("7.1", "Reference Information"),
    ("7.2", "Line Listings of Serious Adverse Reactions during the Reporting Period"),
    ("7.3", "Cumulative Summary Tabulations of Serious Adverse Events"),
    ("8", "Significant Findings from Clinical Trials during the Reporting Period"),
    ("8.1", "Completed Clinical Trials"),
    ("8.2", "Ongoing Clinical Trials"),
    ("8.3", "Long-term Follow-up"),
    ("8.4", "Other Therapeutic Use"),
    ("8.5", "Combination Therapies"),
    ("9", "Safety Findings from Non-interventional Studies"),
    ("10", "Other Clinical Trial and Study Safety Information"),
    ("11", "Safety Findings from Marketing Experience"),
    ("12", "Non-clinical Data"),
    ("13", "Literature"),
    ("14", "Other DSURs"),
    ("15", "Lack of Efficacy"),
    ("16", "Region-Specific Information"),
    ("17", "Late-Breaking Information"),
    ("18", "Overall Safety Assessment"),
    ("18.1", "Evaluation of the Risks"),
    ("18.2", "Benefit-Risk Considerations"),
    ("19", "Summary of Important Risks"),
    ("20", "Conclusions"),
    ("21", "Appendices"),
)

#: EU GVP Module V. Roman parts, so the codes are the ones the template uses.
RMP = (
    ("I", "Product Overview"),
    ("II", "Safety Specification"),
    ("II.SI", "Epidemiology of the Indication and Target Population"),
    ("II.SII", "Non-clinical Part of the Safety Specification"),
    ("II.SIII", "Clinical Trial Exposure"),
    ("II.SIV", "Populations Not Studied in Clinical Trials"),
    ("II.SV", "Post-authorisation Experience"),
    ("II.SVI", "Additional EU Requirements for the Safety Specification"),
    ("II.SVII", "Identified and Potential Risks"),
    ("II.SVIII", "Summary of the Safety Concerns"),
    ("III", "Pharmacovigilance Plan"),
    ("IV", "Plans for Post-authorisation Efficacy Studies"),
    ("V", "Risk Minimisation Measures"),
    ("VI", "Summary of the Risk Management Plan"),
    ("VII", "Annexes"),
)

#: GVP Module IX. Flat: a signal evaluation is an argument in order, not a
#: hierarchy.
SIGNAL_EVAL = (
    ("1", "Signal Identification and Source"),
    ("2", "Description of the Signal"),
    ("3", "Reference Safety Information Status"),
    ("4", "Case Series Review"),
    ("5", "Disproportionality and Database Findings"),
    ("6", "Clinical Trial Data"),
    ("7", "Literature Review"),
    ("8", "Biological Plausibility and Mechanism"),
    ("9", "Evaluation of Alternative Explanations and Confounding"),
    ("10", "Exposure and Estimated Reporting Rate"),
    ("11", "Assessment and Conclusion"),
    ("12", "Recommendation and Action Plan"),
    ("13", "References"),
)

#: 21 CFR 314.80 / 600.80.
PADER = (
    ("1", "Narrative Summary and Analysis"),
    ("2", "Index Line Listing of Reports"),
    ("3", "Copies of 15-Day Alert Reports Submitted in the Period"),
    ("4", "History of Actions Taken"),
    ("5", "Appendices"),
)

#: ICH E2B(R3) data elements / CIOMS I. One narrative, in a fixed order, and
#: the order IS the structure -- §8 gives it as a sequence a reader can check
#: clause by clause against the case.
ICSR_NARRATIVE = (
    ("1", "Patient Demographics"),
    ("2", "Relevant Medical History and Concomitant Medications"),
    ("3", "Suspect Product: Dose, Route and Dates"),
    ("4", "Event Onset, Dates and Course"),
    ("5", "Treatment Given"),
    ("6", "Outcome"),
    ("7", "Dechallenge and Rechallenge"),
    ("8", "Causality as Recorded by Reporter and Company"),
    ("9", "Relevant Laboratory Data"),
    ("10", "Follow-up Status"),
)

#: EU renewal guidance.
ACO = (
    ("1", "Introduction"),
    ("2", "Clinical Efficacy Update"),
    ("3", "Clinical Safety Update"),
    ("4", "Benefit-Risk Assessment"),
    ("5", "Conclusions on the Benefit-Risk Balance"),
)

#: GVP Module VI.
LIT_REVIEW = (
    ("1", "Scope and Objectives"),
    ("2", "Databases and Search Strategy"),
    ("3", "Search Results"),
    ("4", "Articles Assessed as Relevant"),
    ("5", "Cases Identified and Reported"),
    ("6", "Conclusions"),
)

TREES = {
    "pbrer": PBRER,
    "dsur": DSUR,
    "rmp": RMP,
    "signal_eval": SIGNAL_EVAL,
    "pader": PADER,
    "icsr_narrative": ICSR_NARRATIVE,
    "aco": ACO,
    "lit_review": LIT_REVIEW,
}


# ------------------------------------------------------- the computed tables

#: Which section carries which computed table. A section named here is a DATA
#: section: the model writes prose around the table and never the table, and
#: the export blocks if the marker is not in the approved text.
TABLE_KEYS = {
    "pbrer": {
        "2": "approval_status_table",
        "3": "action_table",
        "5.1": "exposure_table",
        "5.2": "exposure_table",
        "6.2": "summary_tab_trials",
        "6.3": "summary_tab_soc_pt",
        "11": "literature_table",
        "15": "signal_overview",
        "16.1": "safety_concern_table",
    },
    "dsur": {
        "2": "approval_status_table",
        "3": "action_table",
        "5": "study_inventory",
        "6.1": "exposure_table",
        "6.2": "exposure_table",
        "7.2": "line_listing_sar",
        "7.3": "summary_tab_soc_pt",
        "13": "literature_table",
        "19": "safety_concern_table",
    },
    "pader": {
        "2": "line_listing_sar",
        "4": "action_table",
    },
    "rmp": {
        "II.SIII": "exposure_table",
        "II.SVIII": "safety_concern_table",
    },
    "signal_eval": {
        "5": "summary_tab_soc_pt",
        "10": "exposure_table",
    },
    "lit_review": {
        "4": "literature_table",
    },
    "icsr_narrative": {},
    "aco": {},
}


# ------------------------------------------------------------ source mapping

#: What every section may draw on when nothing more specific is stated. The
#: previous report is in the default because a periodic report is written
#: against its predecessor everywhere, not only in the sections that quote it.
DEFAULT_SOURCES = ("previous_report", "rsi_doc", "other")

#: Section code -> the doc types retrieval may use, over and above the default.
SOURCE_TYPES = {
    "pbrer": {
        "2": ("authority_corr",),
        "3": ("authority_corr", "rsi_doc"),
        "4": ("rsi_doc",),
        "5.1": ("exposure_data", "study_report"),
        "5.2": ("exposure_data",),
        "7": ("study_report",),
        "7.1": ("study_report",),
        "7.2": ("study_report",),
        "7.3": ("study_report",),
        "7.4": ("study_report",),
        "7.5": ("study_report",),
        "8": ("study_report",),
        "10": ("nonclinical",),
        "11": ("literature",),
        "15": ("signal_doc",),
        "16": ("signal_doc", "rmp_doc"),
        "16.1": ("rmp_doc",),
        "16.2": ("signal_doc",),
        "16.5": ("rmp_doc",),
        "17": ("study_report",),
        "18": ("study_report", "rmp_doc"),
    },
    "dsur": {
        "2": ("authority_corr",),
        "3": ("authority_corr", "rsi_doc"),
        "4": ("rsi_doc",),
        "5": ("study_registry", "study_report"),
        "6.1": ("exposure_data", "study_report"),
        "6.2": ("exposure_data",),
        "8": ("study_report",),
        "8.1": ("study_report",),
        "8.2": ("study_report",),
        "8.3": ("study_report",),
        "8.4": ("study_report",),
        "8.5": ("study_report",),
        "9": ("study_report",),
        "12": ("nonclinical",),
        "13": ("literature",),
        "16": ("authority_corr",),
        "19": ("rmp_doc",),
    },
    "rmp": {
        "II.SI": ("epi_data",),
        "II.SII": ("nonclinical",),
        "II.SIII": ("exposure_data", "study_report"),
        "II.SIV": ("study_report",),
        "II.SV": ("exposure_data",),
        "II.SVII": ("signal_doc", "rmp_doc"),
        "II.SVIII": ("rmp_doc",),
        "III": ("rmp_doc",),
        "V": ("rmp_doc",),
    },
    "signal_eval": {
        "3": ("rsi_doc",),
        "5": ("signal_doc",),
        "6": ("study_report",),
        "7": ("literature",),
        "10": ("exposure_data",),
    },
    "pader": {"4": ("authority_corr",)},
    "lit_review": {"2": ("literature",), "3": ("literature",), "4": ("literature",)},
    "icsr_narrative": {},
    "aco": {"2": ("study_report",), "3": ("study_report",)},
}


# ----------------------------------------------------------------- guidance

#: The sentence the drafting prompt carries for this section. Only sections
#: whose instruction is not obvious from the title appear here; the rest get
#: their title. What earns an entry is a rule that would otherwise be broken
#: plausibly -- writing a count, merging interval with cumulative, concluding a
#: benefit-risk balance nobody stated.
_INTERVAL_AND_CUMULATIVE = (
    "State interval and cumulative figures separately and label each. They are "
    "different numbers about the same product and must never be merged or "
    "carried forward from the baseline text."
)
_NO_CONCLUSION = (
    "Do not conclude that the benefit-risk balance has changed, that a signal "
    "is confirmed, or that a risk is causally associated, unless a source "
    "states that conclusion. Where the section requires such a judgment and no "
    "sourced conclusion exists, write [ASSESSMENT REQUIRED: ...]."
)
_TABLE_ONLY = (
    "The tabulation itself is inserted from confirmed case data. Write the "
    "surrounding prose only; where the table belongs, emit its [TABLE: ...] "
    "marker on a line of its own and continue."
)

GUIDANCE = {
    "pbrer": {
        "1": "State the reporting interval, the product, and what this report covers. "
             "Name the international birth date and the data lock point.",
        "5": _INTERVAL_AND_CUMULATIVE,
        "5.1": "Cumulative subject exposure in clinical trials, with the method by "
               "which it was estimated. " + _TABLE_ONLY,
        "5.2": "Interval and cumulative patient exposure from marketing experience. "
               + _INTERVAL_AND_CUMULATIVE + " " + _TABLE_ONLY,
        "6.1": "State the MedDRA version and the reference safety information version "
               "against which the tabulations that follow were prepared.",
        "6.2": _TABLE_ONLY,
        "6.3": _INTERVAL_AND_CUMULATIVE + " " + _TABLE_ONLY,
        "13": "Report only what the sources state about lack of efficacy in controlled "
              "trials. Absence of evidence is not a finding of efficacy.",
        "15": "Describe the signals in the overview table. Do not add, close or "
              "re-classify a signal. " + _TABLE_ONLY,
        "16.2": "Summarise each signal evaluation as the sources record it. " + _NO_CONCLUSION,
        "16.3": _NO_CONCLUSION,
        "16.4": "Characterise each risk in terms of frequency, seriousness, "
                "reversibility and affected population, as recorded. " + _NO_CONCLUSION,
        "16.5": "Report the effectiveness of risk minimisation measures only where a "
                "source measures it.",
        "17": _NO_CONCLUSION,
        "18": "The integrated benefit-risk analysis. " + _NO_CONCLUSION,
        "18.1": "Set out the context: the condition, the alternatives, and the "
                "population. " + _NO_CONCLUSION,
        "18.2": _NO_CONCLUSION,
        "19": "State the conclusions the sources support and the actions proposed. "
              + _NO_CONCLUSION,
    },
    "dsur": {
        "1": "State the reporting period, the development programme, and the "
             "development international birth date this report counts from.",
        "5": "The inventory of trials ongoing and completed in the period. " + _TABLE_ONLY,
        "6": _INTERVAL_AND_CUMULATIVE,
        "6.1": "Cumulative subject exposure in the development programme. " + _TABLE_ONLY,
        "6.2": "Patient exposure from marketing experience, where the product is "
               "marketed. " + _TABLE_ONLY,
        "7.1": "State the MedDRA version and the reference safety information version "
               "the line listings and tabulations were prepared against.",
        "7.2": "The line listing of serious adverse reactions in the period, "
               "de-identified. Refer to cases by case identifier only. " + _TABLE_ONLY,
        "7.3": _INTERVAL_AND_CUMULATIVE + " " + _TABLE_ONLY,
        "18": "The overall safety assessment. " + _NO_CONCLUSION,
        "18.1": _NO_CONCLUSION,
        "18.2": _NO_CONCLUSION,
        "19": "Summarise the important risks as the safety concern table records them. "
              + _TABLE_ONLY,
        "20": "State the conclusions the sources support. " + _NO_CONCLUSION,
    },
    "signal_eval": {
        "3": "State the reference safety information version in force and whether the "
             "signal's terms are listed in it. Do not decide listedness here; report "
             "the determination the confirmed data records.",
        "5": "Present the disproportionality figures with their counts. Label them as "
             "screening statistics: they indicate reporting frequency, not causality, "
             "and are not evidence of a causal association. " + _TABLE_ONLY,
        "10": "State the exposure denominator and its method before any reporting "
              "rate. A rate without its denominator is not a rate. " + _TABLE_ONLY,
        "11": _NO_CONCLUSION,
        "12": _NO_CONCLUSION,
    },
    "rmp": {
        "II.SIII": "Clinical trial exposure by the measures the sources state. " + _TABLE_ONLY,
        "II.SVIII": "The summary of safety concerns, from the safety concern "
                    "register. " + _TABLE_ONLY,
    },
    "pader": {
        "1": "Narrative summary and analysis of the interval's reports. " + _NO_CONCLUSION,
        "2": "The index line listing. " + _TABLE_ONLY,
    },
    "icsr_narrative": {},
    "aco": {"4": _NO_CONCLUSION, "5": _NO_CONCLUSION},
    "lit_review": {"4": _TABLE_ONLY},
}


# ------------------------------------------------------------------ builders

def _level(code: str) -> int:
    """Depth from the code. "16.2" is level 2; "II.SVIII" is level 2."""
    return code.count(".") + 1


def _is_container(code: str, codes) -> bool:
    """A section with children holds no text of its own."""
    prefix = code + "."
    return any(other.startswith(prefix) for other in codes)


# ------------------------------------------------------ regional appendices

#: Where a report's structure differs by region, the difference is a regional
#: appendix seeded only for the regions the report is prepared for. A PBRER is
#: one document under ICH E2C(R2); the EU adds GVP Module VII's regional
#: appendix, and the US accepts a PBRER in place of a PADER with its own. A
#: regional copy of the export carries the base report and that region's
#: appendix only.
REGIONAL_APPENDICES = {
    "pbrer": {
        "EU": (
            ("EU", "EU Regional Appendix"),
            ("EU.1", "Current Proposed Product Information"),
            ("EU.2", "Proposed Additional Pharmacovigilance and Risk Minimisation "
                     "Activities"),
            ("EU.3", "Summary of Ongoing Safety Concerns"),
            ("EU.4", "Reporting of Results from Post-authorisation Safety Studies"),
            ("EU.5", "Effectiveness of Risk Minimisation"),
        ),
        "US": (
            ("US", "US Regional Appendix"),
            ("US.1", "Index Line Listing of 15-Day Alert Reports"),
            ("US.2", "Actions Taken Since the Last Report"),
            ("US.3", "Other Information Required under 21 CFR 314.80"),
        ),
    },
}

REGIONAL_TABLE_KEYS = {
    "pbrer": {"EU.3": "safety_concern_table", "US.1": "line_listing_sar",
              "US.2": "action_table"},
}

REGIONAL_SOURCE_TYPES = {
    "pbrer": {"EU.1": ("rsi_doc",), "EU.2": ("rmp_doc",), "EU.3": ("rmp_doc",),
              "EU.4": ("study_report",), "EU.5": ("rmp_doc",)},
}


def region_of(section_code: str) -> str | None:
    """The region a section belongs to, or None for the base report."""
    head = (section_code or "").split(".")[0]
    for appendices in REGIONAL_APPENDICES.values():
        if head in appendices:
            return head
    return None


def regional_codes(doc_type_key: str, region: str) -> list[str]:
    return [code for code, _t in REGIONAL_APPENDICES.get(doc_type_key, {}).get(region, ())]


def seed_sections(doc_type_key: str, regions=()) -> list[dict]:
    """Every section of one report type, in document order, with the regional
    appendix of each region the report is prepared for.

    Returned as plain dicts so the caller can hand them straight to the model
    layer without this module importing it -- `app.safety.trees` is leaf data
    and stays that way.
    """
    tree = TREES.get(doc_type_key)
    if tree is None:
        raise KeyError(f"unknown report type {doc_type_key!r}")
    appendices = REGIONAL_APPENDICES.get(doc_type_key, {})
    tree = tuple(tree) + tuple(
        entry for region in sorted(set(regions or ()) & set(appendices))
        for entry in appendices[region])
    codes = [code for code, _title in tree]
    tables = {**TABLE_KEYS.get(doc_type_key, {}),
              **REGIONAL_TABLE_KEYS.get(doc_type_key, {})}
    sources = {**SOURCE_TYPES.get(doc_type_key, {}),
               **REGIONAL_SOURCE_TYPES.get(doc_type_key, {})}
    guidance = GUIDANCE.get(doc_type_key, {})

    sections = []
    for order, (code, title) in enumerate(tree):
        container = _is_container(code, codes)
        sections.append({
            "section_code": code,
            "title": title,
            "sort_order": order,
            "level": _level(code),
            "is_container": container,
            "guidance_text": guidance.get(code) or title,
            # A container carries no table: the table belongs to whichever
            # child prints it, and a marker on a heading would render above
            # the sections it summarises.
            "table_key": None if container else tables.get(code),
            "source_types": sorted(set(DEFAULT_SOURCES) | set(sources.get(code, ()))),
        })
    return sections


def table_key_for(doc_type_key: str, section_code: str) -> str | None:
    return (TABLE_KEYS.get(doc_type_key, {}).get(section_code)
            or REGIONAL_TABLE_KEYS.get(doc_type_key, {}).get(section_code))

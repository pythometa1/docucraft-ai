"""What this module can produce, and what each thing needs.

The registry is data a deployment can edit, not a chain of branches somebody
has to find. Each entry declares the built-in section structure, the sources it
expects, and -- the entry that matters most -- **which birth date its
cumulative figures count from**.

That last one is why the anchor lives here rather than in the query layer. A
PBRER counts cumulatively from the international birth date, the day the
product was first approved anywhere. A DSUR counts from the development
international birth date, the day the first trial was authorised. They are
usually years apart. A query layer that picked one would silently change every
cumulative figure in the other document, and nothing in the output would look
wrong: the totals would simply be totals of a different window. So the report
type states its anchor, `app.safety.scope` reads it, and the two facts stay
attached to each other.
"""

from app.safety import trees

#: The two anchors a cumulative window can start from.
IBD = "ibd"
DIBD = "dibd"

#: Every uploaded document's type, with what it is for. `previous_report` is
#: dual-purpose -- baseline text for carry-forward sections, and the thing
#: continuity checks compare against -- and is labelled so a reader knows to
#: check its currency before reusing a sentence from it.
DOC_TYPES = {
    "rsi_doc": "CCDS / IB / SmPC / USPI (Reference Safety Information)",
    "previous_report": "Previous PBRER / DSUR / PADER (baseline — verify currency before reuse)",
    "rmp_doc": "Current Risk Management Plan",
    "study_report": "CSR / interim analysis / study synopsis",
    "study_registry": "Trial inventory / registry export",
    "exposure_data": "Sales, prescription or exposure calculation source",
    "literature": "Literature search output, articles, abstracts",
    "nonclinical": "Non-clinical study reports",
    "authority_corr": "Regulatory authority correspondence, requests, assessments",
    "signal_doc": "Signal detection outputs, disproportionality runs, assessments",
    "epi_data": "Epidemiology of the indication / background incidence",
    "other": "Other supporting document",
}

#: The structured case inputs, which are parsed rather than chunked.
INPUT_TYPES = {
    "e2b_r3_xml": "ICSR export (E2B(R3) or R2)",
    "line_listing": "Line listing from the safety database (Excel/CSV)",
    "cioms_form": "CIOMS I form (PDF)",
    "case_narrative_doc": "Case narrative document",
    "document": "Supporting document",
}

#: The roles that gate pharmacovigilance judgment, weakest first. Ordering is
#: the point: `require_pv_role` compares positions, so a qualified person can
#: do a writer's work and not the other way round.
PV_ROLES = ("writer", "reviewer", "qualified_person")

#: The regions a report may target. Free text would let two reports spell one
#: authority two ways and then disagree about which regions were covered.
REGIONS = ("EU", "US", "UK", "JP", "CA", "CH", "AU", "IN", "CN", "BR", "ROW")

RSI_TYPES = ("ccds", "ib", "smpc", "uspi", "other")


DELIVERABLES = {
    "pbrer": {
        "name": "Periodic Benefit-Risk Evaluation Report (EU PSUR)",
        "structure_basis": "ICH E2C(R2) · GVP Module VII",
        "cumulative_anchor": IBD,
        "periodic": True,
        "required_sources": ("previous_report", "rsi_doc", "exposure_data"),
        "recommended_sources": ("signal_doc", "rmp_doc", "literature",
                                "study_report", "authority_corr"),
    },
    "dsur": {
        "name": "Development Safety Update Report",
        "structure_basis": "ICH E2F",
        # The development birth date, not the approval one. A DSUR that counted
        # from first approval would omit every trial subject exposed before it.
        "cumulative_anchor": DIBD,
        "periodic": True,
        "required_sources": ("previous_report", "rsi_doc", "study_registry",
                             "exposure_data"),
        "recommended_sources": ("study_report", "literature", "nonclinical",
                                "authority_corr"),
    },
    "pader": {
        "name": "Periodic Adverse Drug Experience Report (US)",
        "structure_basis": "21 CFR 314.80 / 600.80",
        "cumulative_anchor": IBD,
        "periodic": True,
        "required_sources": ("previous_report", "rsi_doc"),
        "recommended_sources": ("authority_corr", "exposure_data"),
    },
    "rmp": {
        "name": "Risk Management Plan (EU)",
        "structure_basis": "GVP Module V",
        "cumulative_anchor": IBD,
        # An RMP is a living document rather than an interval's report: it has
        # a version, not a period. The scope layer still bounds it by the data
        # lock point, which is what "as at" means for a document like this.
        "periodic": False,
        "required_sources": ("rmp_doc", "rsi_doc"),
        "recommended_sources": ("epi_data", "study_report", "nonclinical",
                                "exposure_data", "signal_doc"),
    },
    "signal_eval": {
        "name": "Signal Evaluation / Assessment Report",
        "structure_basis": "GVP Module IX",
        "cumulative_anchor": IBD,
        "periodic": False,
        "required_sources": ("signal_doc", "rsi_doc"),
        "recommended_sources": ("literature", "study_report", "exposure_data",
                                "epi_data"),
    },
    "icsr_narrative": {
        "name": "ICSR case narrative",
        "structure_basis": "ICH E2B(R3) data elements · CIOMS I",
        "cumulative_anchor": IBD,
        "periodic": False,
        "required_sources": (),
        "recommended_sources": ("rsi_doc",),
    },
    "aco": {
        "name": "Addendum to Clinical Overview (renewal)",
        "structure_basis": "EU renewal guidance",
        "cumulative_anchor": IBD,
        "periodic": True,
        "required_sources": ("previous_report", "rsi_doc"),
        "recommended_sources": ("study_report", "literature", "rmp_doc"),
    },
    "lit_review": {
        "name": "Safety literature monitoring summary",
        "structure_basis": "GVP Module VI",
        "cumulative_anchor": IBD,
        "periodic": True,
        "required_sources": ("literature",),
        "recommended_sources": ("rsi_doc", "previous_report"),
    },
}


def deliverable(doc_type_key: str) -> dict:
    entry = DELIVERABLES.get(doc_type_key)
    if entry is None:
        raise KeyError(f"unknown report type {doc_type_key!r}")
    return entry


def cumulative_anchor(doc_type_key: str) -> str:
    """`ibd` or `dibd`: the column a cumulative window starts at."""
    return deliverable(doc_type_key)["cumulative_anchor"]


def is_periodic(doc_type_key: str) -> bool:
    return bool(deliverable(doc_type_key)["periodic"])


def source_types_for(doc_type_key: str, section_code: str) -> list[str]:
    """The doc types one section may retrieve from."""
    extra = trees.SOURCE_TYPES.get(doc_type_key, {}).get(section_code, ())
    return sorted(set(trees.DEFAULT_SOURCES) | set(extra))


def requirements(doc_type_keys) -> dict:
    """The upload checklist for a set of report types.

    Computed from what was chosen rather than fixed, so a product doing only
    literature monitoring is not told it is missing a trial registry.
    """
    required: set = set()
    recommended: set = set()
    for key in doc_type_keys:
        entry = DELIVERABLES.get(key)
        if not entry:
            continue
        required |= set(entry["required_sources"])
        recommended |= set(entry["recommended_sources"])
    return {
        "required": sorted(required),
        "recommended": sorted(recommended - required),
        "labels": dict(DOC_TYPES),
    }


def catalogue() -> list[dict]:
    """The registry as the report-type picker needs it."""
    return [
        {
            "key": key,
            "name": entry["name"],
            "structure_basis": entry["structure_basis"],
            "cumulative_anchor": entry["cumulative_anchor"],
            "periodic": entry["periodic"],
            "section_count": len(trees.TREES[key]),
            "required_sources": list(entry["required_sources"]),
            "recommended_sources": list(entry["recommended_sources"]),
        }
        for key, entry in DELIVERABLES.items()
    ]

"""Which deliverables this module can produce, and what each one needs.

A registry, not a hardcoded outline. Quality/CMC is not one document: a
dossier module, an annual review and a method validation report share a
product, a data store and an audit trail while having nothing else in common.
Adding the ninth deliverable is an entry here, not a branch somewhere else.

Each entry declares:
  tree            the section structure (`app.cmc.ctd`), or () while unbuilt
  structure_basis what the sections are numbered against, quoted into the prompt
  required        source doc types the deliverable cannot be written without
  recommended     types that materially improve it
  source_map      section code prefix -> the doc types that section may read

`source_map` is longest-prefix-wins, exactly as the clinical module's is: a key
for "P.5" covers P.5.4 unless P.5.4 says otherwise. A section with no entry
reads every citable type, which is the honest default -- an empty filter
retrieves widely rather than retrieving nothing.
"""

from app.cmc import ctd

#: Every source document type the module accepts. `prior_dossier` is
#: retrievable for style and for baseline diffing and citable as fact for
#: nothing, which is why it never appears in a `source_map` value.
DOC_TYPES = (
    "spec_ds", "spec_dp", "spec_excipient", "coa", "stability_protocol",
    "stability_data", "method_sop", "method_val_report", "bmr", "process_flow",
    "pv_report", "dev_report", "characterisation", "impurity_report", "ccs",
    "ref_std", "site_gmp", "dmf", "supplier_doc", "deviation_capa",
    "prior_dossier", "other",
)

STYLE_REFERENCE_TYPE = "prior_dossier"

#: The types the structured extractor reads numbers out of. Everything else is
#: prose evidence only -- a development report may discuss a limit, but the
#: limit that reaches a specification table comes from the specification.
STRUCTURED_TYPES = ("coa", "spec_ds", "spec_dp", "spec_excipient",
                    "stability_data", "bmr")

_32S_SOURCES = {
    "S.1": ["characterisation", "dev_report", "dmf"],
    "S.2": ["process_flow", "bmr", "dev_report", "site_gmp", "dmf"],
    "S.2.1": ["site_gmp", "dmf"],
    "S.2.5": ["pv_report"],
    "S.3": ["characterisation", "impurity_report"],
    "S.3.2": ["impurity_report", "spec_ds"],
    "S.4": ["spec_ds", "method_sop", "coa"],
    "S.4.1": ["spec_ds"],
    "S.4.2": ["method_sop"],
    "S.4.3": ["method_val_report"],
    "S.4.4": ["coa", "spec_ds"],
    "S.4.5": ["spec_ds", "coa", "stability_data", "dev_report"],
    "S.5": ["ref_std"],
    "S.6": ["ccs"],
    "S.7": ["stability_data", "stability_protocol"],
}

_32P_SOURCES = {
    "P.1": ["spec_dp", "dev_report", "bmr"],
    "P.2": ["dev_report"],
    "P.2.4": ["ccs", "dev_report"],
    "P.3": ["bmr", "process_flow", "site_gmp"],
    "P.3.1": ["site_gmp"],
    "P.3.2": ["bmr"],
    "P.3.5": ["pv_report"],
    "P.4": ["spec_excipient", "supplier_doc"],
    "P.4.5": ["supplier_doc"],
    "P.5": ["spec_dp", "method_sop", "coa"],
    "P.5.1": ["spec_dp"],
    "P.5.2": ["method_sop"],
    "P.5.3": ["method_val_report"],
    "P.5.4": ["coa", "spec_dp"],
    "P.5.5": ["impurity_report", "spec_dp"],
    "P.5.6": ["spec_dp", "coa", "stability_data", "dev_report"],
    "P.6": ["ref_std"],
    "P.7": ["ccs"],
    "P.8": ["stability_data", "stability_protocol"],
}

DELIVERABLES = {
    "ctd_32s": {
        "name": "CTD Module 3.2.S - Drug Substance",
        "structure_basis": "ICH M4Q",
        "tree": ctd.DRUG_SUBSTANCE,
        "required": ("spec_ds", "coa"),
        "recommended": ("stability_data", "method_sop", "characterisation", "impurity_report"),
        "source_map": _32S_SOURCES,
    },
    "ctd_32p": {
        "name": "CTD Module 3.2.P - Drug Product",
        "structure_basis": "ICH M4Q",
        "tree": ctd.DRUG_PRODUCT,
        "required": ("spec_dp", "coa", "bmr"),
        "recommended": ("stability_data", "method_sop", "dev_report", "ccs"),
        "source_map": _32P_SOURCES,
    },
    "ctd_32ar": {
        "name": "CTD Module 3.2.A / 3.2.R - Appendices and Regional Information",
        "structure_basis": "ICH M4Q and the regional profile",
        "tree": ctd.APPENDICES_REGIONAL,
        "required": ("site_gmp",),
        "recommended": ("supplier_doc", "dev_report"),
        "source_map": {"A.1": ["site_gmp"], "A.2": ["supplier_doc"],
                       "A.3": ["dev_report"], "R.1": ["site_gmp", "method_val_report"]},
    },
    # Declared so the picker can show what is coming and the registry stays the
    # single list, but refused at selection time rather than seeded empty: a
    # deliverable with no sections is a deliverable that looks built and is not.
    "qos_23": {"name": "Quality Overall Summary (Module 2.3)",
               "structure_basis": "ICH M4Q", "tree": (), "required": (),
               "recommended": (), "source_map": {}, "unbuilt": "M8"},
    "apqr": {"name": "Annual Product Quality Review",
             "structure_basis": "EU GMP Chapter 1 and 21 CFR 211.180(e)", "tree": (),
             "required": (), "recommended": (), "source_map": {}, "unbuilt": "M8"},
    "method_val": {"name": "Analytical Method Validation Report",
                   "structure_basis": "ICH Q2", "tree": (), "required": (),
                   "recommended": (), "source_map": {}, "unbuilt": "M8"},
    "process_val": {"name": "Process Validation Report",
                    "structure_basis": "EU GMP Annex 15", "tree": (), "required": (),
                    "recommended": (), "source_map": {}, "unbuilt": "M8"},
    "stability_report": {"name": "Stability Study Report",
                         "structure_basis": "ICH Q1A and Q1E", "tree": (), "required": (),
                         "recommended": (), "source_map": {}, "unbuilt": "M8"},
}

#: Display order in the picker.
DELIVERABLE_ORDER = ("ctd_32s", "ctd_32p", "ctd_32ar", "qos_23", "apqr",
                     "method_val", "process_val", "stability_report")


class UnknownDeliverable(ValueError):
    """A deliverable key nobody ships."""


def deliverable(key: str) -> dict:
    entry = DELIVERABLES.get((key or "").strip())
    if entry is None:
        raise UnknownDeliverable(
            f"there is no deliverable called {key!r}; the ones that exist are "
            f"{', '.join(DELIVERABLE_ORDER)}")
    return entry


def source_types_for(deliverable_key: str, section_code: str) -> list:
    """The doc types a section may retrieve from: its own entry, else the
    nearest prefix entry, else everything citable.

    Longest-prefix-wins over the dotted code, the same rule the clinical
    module applies to ICH E3 numbers -- so a deliverable declares "P.5" once
    and overrides only the subsections that genuinely read something else.
    """
    source_map = deliverable(deliverable_key).get("source_map") or {}
    parts = (section_code or "").split(".")
    for depth in range(len(parts), 0, -1):
        key = ".".join(parts[:depth])
        if key in source_map:
            return list(source_map[key])
    return []


def requirements(deliverable_keys) -> tuple:
    """The required and recommended source types for a set of deliverables.

    Computed per project rather than fixed, because the checklist a person is
    shown has to be the checklist for the dossier they are actually writing:
    a 3.2.P project needs a batch record, a 3.2.S project does not.
    """
    required: list = []
    recommended: list = []
    for key in deliverable_keys or ():
        entry = DELIVERABLES.get(key)
        if entry is None:
            continue
        for doc_type in entry.get("required") or ():
            if doc_type not in required:
                required.append(doc_type)
        for doc_type in entry.get("recommended") or ():
            if doc_type not in recommended:
                recommended.append(doc_type)
    # A type that is required by one deliverable is not merely recommended by
    # another: the stricter claim wins, or the checklist would tell somebody a
    # missing specification is optional.
    recommended = [d for d in recommended if d not in required]
    return tuple(required), tuple(recommended)

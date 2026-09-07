"""What the clinical vertical declares to the registry (`app.verticals`).

A leaf module on purpose: nothing from `app.*` is imported here, because the
registry that imports this file is itself imported by the compiler and the
routers. The derivations, the router and the models live beside this file;
this file is only the declarations.

Service keys are per document type (`clinical_csr`, `clinical_icf`, ...)
rather than one `clinical`, because the authoring mechanism maps one service
key to one prompt pack and one fallback kit -- and a study report, a consent
form and an amendment are three different documents, not three flavours of
one.
"""

from pathlib import Path

KIT_DIR = Path(__file__).parent / "kits"
KIT_IDS = ("clinical", "clinical_icf", "clinical_protocol")

#: The document types this vertical generates: request key -> taxonomy label.
#: The request key doubles as the numbering key, so each type numbers its own
#: sequence (CSR-0001 and PA-0001 never collide, and never share a counter).
DOC_TYPES = {
    "csr": "Clinical Study Report",
    "protocol_amendment": "Protocol Amendment",
    "icf": "Informed Consent",
    "investigator_brochure": "Investigator Brochure",
}

PROMPT_PACKS = {
    "clinical_csr": """This template is a CLINICAL STUDY REPORT. It must contain, in a sensible order:
  - a title heading, then the study's identity: <Study Title>, <Protocol Number>,
    <Sponsor Name>, <Phase>, and <Indication>
  - document control: <Document Number>, <Report Date>, and <Version Label>
  - a brief synopsis or background section grounded in the description
  - ONE subject-disposition table: a static header row, then the repeat row with
    placeholders such as <Site Name>, <Subjects Enrolled>, <Subjects Completed>,
    <Subjects Withdrawn> (line_items_key: disposition_rows), every count typed number
  - totals after the table as their own placeholders -- <Total Subjects Enrolled>,
    <Total Subjects Completed>, <Total Subjects Withdrawn> -- never a reuse of the
    row's own column tokens
  - narrative results sections, each with its own placeholder: <Efficacy Summary>,
    <Safety Summary>, and <Conclusions>
  - a signatures section: <Investigator Name> and <Report Date>.
Dates are typed date. Formal regulatory-register English unless the description
says otherwise.""",
    "clinical_protocol_amendment": """This template is a PROTOCOL AMENDMENT. It must contain, in a sensible order:
  - a title heading, then the study's identity: <Study Title>, <Protocol Number>,
    and <Sponsor Name>
  - document control: <Document Number> (the amendment's own number),
    <Document Date>, and <Version Label>
  - the reason for the change: <Amendment Rationale>
  - ONE table of changes: a static header row, then the repeat row with
    placeholders <Section Reference>, <Previous Text>, <Revised Text>
    (line_items_key: amendment_items)
  - a short implementation note, and approval signatures: <Investigator Name>
    and <Document Date>.""",
    "clinical_icf": """This template is an INFORMED CONSENT FORM for a clinical study. It must
contain, in a sensible order:
  - a title heading, then the study's identity: <Study Title>, <Protocol Number>,
    <Sponsor Name>, and <Investigator Name>
  - document control: <Document Number>, <Document Date>, and <Version Label>
  - plain-language sections the participant reads: the purpose
    (<Purpose Description>), what taking part involves (<Procedures Description>),
    the risks (<Risks Description>), the benefits (<Benefits Description>), and
    static prose on confidentiality and the right to withdraw at any time
  - ONE visit-schedule table when the description implies scheduled visits: a
    static header row, then the repeat row with placeholders such as
    <Visit Name>, <Visit Week>, <Visit Procedures> (line_items_key: visits),
    the week typed number
  - a consent statement, then signature and date lines for the participant
    (<Participant Name>, <Document Date>) and the person obtaining consent
    (<Investigator Name>).
Written TO the participant, in plain non-technical language.""",
    "clinical_ib": """This template is an INVESTIGATOR BROCHURE summary. It must contain, in a
sensible order:
  - a title heading, then the identity of the product and programme:
    <Study Title>, <Protocol Number>, <Sponsor Name>, <Phase>, and <Indication>
  - document control: <Document Number>, <Document Date>, and <Version Label>
  - narrative sections grounded in the description, each with its own
    placeholder where the content varies per issue: an introduction, the
    product description, <Nonclinical Summary>, <Clinical Summary>, and
    <Investigator Guidance>
  - no repeating table is required (line_items_key: "")
  - a closing line naming <Sponsor Name> and <Document Date>.""",
}

FALLBACK_KITS = {
    "clinical_csr": "clinical",
    "clinical_protocol_amendment": "clinical_protocol",
    "clinical_icf": "clinical_icf",
    # No IB kit of its own: the fallback only fires when no model is
    # configured, the reason is recorded in provenance, and an investigator
    # brochure is a narrative document whose primary path is authoring. A
    # fourth kit is one YAML and one tuple entry away if it earns its place.
    "clinical_ib": "clinical",
}

NUMBER_PREFIXES = {
    "csr": "CSR-",
    "protocol_amendment": "PA-",
    "icf": "ICF-",
    "investigator_brochure": "IB-",
}

"""Reading an ICSR export without inventing anything that was not in it.

E2B is two quite different formats wearing one name. R2 is a flat DTD document
-- `<ichicsr><safetyreport><patient><reaction>` -- and R3 is HL7 v3, where the
same facts live inside nested `observation` and `substanceAdministration` acts
addressed by OIDs. A parser written for one reads nothing at all from the
other, and a safety database will export whichever its vendor implemented.

So this module does not model either schema. It looks for each fact in the
places both formats put it, takes the first that answers, and records what it
could not find. That is deliberately less clever than a schema-driven reader
and it fails in the right direction: a field that is absent stays absent and
appears in the review grid, rather than being inferred from a neighbouring
element that happened to parse.

Three rules hold throughout.

**Nothing is guessed.** A seriousness flag that is not in the file does not
become False, it stays unset -- because "not serious" is a regulatory
determination and "not stated" is an absence. Same for expectedness and
causality, which this module does not touch at all: they are human-owned
columns and an importer that filled them would be making determinations and
attributing them to whoever imported the file.

**Every date keeps what it actually said.** E2B dates are `YYYYMMDD`,
`YYYYMM` or `YYYY`, and a partial date is a real answer -- the reporter knew
the month and not the day. A parser that read `202603` as the 1st of March
would be inventing a day, so a partial date is stored as the earliest day it
could mean and the precision it came with is recorded beside it.

**The XML is untrusted.** These files arrive from outside. The parser resolves
no entities, fetches no networks, and refuses a document that declares a DTD
with entity definitions -- the billion-laughs shape -- rather than expanding it.
"""

from dataclasses import dataclass, field
from datetime import date

from lxml import etree

#: How a value was obtained, mirroring the CMC extractor's confidence idea:
#: a value read from the element it belongs in is not the same as one read
#: from a fallback path.
CONFIDENCE_LABELLED = 1.0
CONFIDENCE_FALLBACK = 0.6


class E2bUnreadable(Exception):
    """The file is not an ICSR this parser can read."""


@dataclass
class ParsedDrug:
    drug_name: str | None = None
    role: str | None = None
    is_company_product: bool = False
    dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None
    route: str | None = None
    indication: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    action_taken: str | None = None
    dechallenge: str | None = None
    rechallenge: str | None = None


@dataclass
class ParsedEvent:
    verbatim_term: str | None = None
    meddra_pt: str | None = None
    meddra_llt: str | None = None
    meddra_soc: str | None = None
    meddra_version: str | None = None
    onset_date: date | None = None
    outcome: str | None = None
    #: Only ever True when the file says so. Never inferred from the case.
    is_serious: bool | None = None


@dataclass
class ParsedCase:
    worldwide_case_id: str | None = None
    local_case_ids: list = field(default_factory=list)
    case_version: int | None = None
    report_source: str | None = None
    study_id: str | None = None
    country_of_occurrence: str | None = None
    primary_reporter_qualification: str | None = None
    initial_receipt_date: date | None = None
    latest_receipt_date: date | None = None
    is_medically_confirmed: bool | None = None
    is_serious: bool | None = None
    seriousness_criteria: list = field(default_factory=list)
    case_outcome: str | None = None
    patient_age: float | None = None
    patient_age_group: str | None = None
    patient_sex: str | None = None
    is_pregnancy_case: bool | None = None
    events: list = field(default_factory=list)
    drugs: list = field(default_factory=list)
    #: The narrative, un-masked. Goes to `pv_case_originals` and nowhere else
    #: until the de-identification pass has run over it.
    narrative: str | None = None
    #: Field name -> why it is not here. Shown in the grid rather than being a
    #: silence somebody has to notice.
    unmapped: dict = field(default_factory=dict)
    #: Dates whose source string was partial, and how partial.
    date_precision: dict = field(default_factory=dict)


# ------------------------------------------------------------------ the XML

def _parser() -> etree.XMLParser:
    """A parser that will not fetch, expand or recurse on somebody's behalf."""
    return etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False,
        load_dtd=False, dtd_validation=False, recover=False)


def _read(data: bytes):
    if not data or not data.strip():
        raise E2bUnreadable("the file is empty")
    try:
        root = etree.fromstring(data, _parser())
    except etree.XMLSyntaxError as exc:
        raise E2bUnreadable(f"the file is not well-formed XML: {exc}") from exc
    # An entity-declaring DTD is refused rather than expanded: an ICSR has no
    # legitimate reason to define entities, and the shapes that do are attacks.
    doctype = getattr(root.getroottree(), "docinfo", None)
    if doctype is not None and (doctype.internalDTD is not None
                                and list(doctype.internalDTD.iterentities())):
        raise E2bUnreadable(
            "the file declares XML entities, which this parser does not expand")
    return root


def _localname(element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _first(node, *names):
    """The first descendant whose local name is one of `names`.

    Local names, so a namespaced R3 document and a bare R2 one are searched the
    same way -- the namespace is the difference between the formats, and it is
    exactly the difference this module is trying not to care about.
    """
    wanted = {n.lower() for n in names}
    for element in node.iter():
        if _localname(element) in wanted:
            return element
    return None


def _all(node, *names):
    wanted = {n.lower() for n in names}
    return [e for e in node.iter() if _localname(e) in wanted]


def _text(node, *names) -> str | None:
    element = _first(node, *names) if names else node
    if element is None:
        return None
    value = (element.text or "").strip()
    if not value:
        # R3 puts values in attributes as often as in text.
        for attribute in ("value", "extension", "displayName", "code"):
            attr = element.get(attribute)
            if attr and attr.strip():
                return attr.strip()
        return None
    return value


# ---------------------------------------------------------------- the values

def parse_date(raw: str | None) -> tuple[date | None, str | None]:
    """An E2B date, and how precise it actually was.

    `YYYYMMDD`, `YYYYMM` and `YYYY` are all legal, and a partial one is a real
    answer rather than a defective one: the reporter knew the month and not the
    day. The earliest day it could mean is stored so that the value can be
    compared, and the precision is returned so the grid can show `2026-03` as
    a month rather than as the first of March -- a day nobody reported.
    """
    text = "".join(ch for ch in (raw or "") if ch.isdigit())
    try:
        if len(text) >= 8:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8])), "day"
        if len(text) >= 6:
            return date(int(text[:4]), int(text[4:6]), 1), "month"
        if len(text) == 4:
            return date(int(text), 1, 1), "year"
    except ValueError:
        return None, None
    return None, None


#: E2B code lists. Only the ones whose meaning is fixed by the standard are
#: here; anything else is carried through as the source wrote it.
_SEX = {"1": "male", "2": "female", "0": "unknown", "9": "unknown"}
_DRUG_ROLE = {"1": "suspect", "2": "concomitant", "3": "interacting"}
_OUTCOME = {"1": "recovered", "2": "recovering", "3": "not_recovered",
            "4": "recovered_with_sequelae", "5": "fatal", "6": "unknown"}
_ACTION = {"1": "drug_withdrawn", "2": "dose_reduced", "3": "dose_increased",
           "4": "dose_not_changed", "5": "unknown", "6": "not_applicable"}
_CHALLENGE = {"1": "positive", "2": "negative", "3": "not_applicable",
              "4": "unknown"}
_QUALIFICATION = {"1": "physician", "2": "pharmacist", "3": "other_health_professional",
                  "4": "lawyer", "5": "consumer"}
#: `reporttype` in R2. 2 is the one that means a trial.
_REPORT_SOURCE = {"1": "spontaneous", "2": "clinical_trial",
                  "3": "other", "4": "literature"}

#: The seriousness criteria, by the element that carries each one. A flag is
#: only recorded when the file says "1"; absent means absent.
_SERIOUSNESS = (
    ("seriousnessdeath", "death"),
    ("seriousnesslifethreatening", "life_threatening"),
    ("seriousnesshospitalization", "hospitalisation"),
    ("seriousnessdisabling", "disability"),
    ("seriousnesscongenitalanomali", "congenital_anomaly"),
    ("seriousnesscongenitalanomaly", "congenital_anomaly"),
    ("seriousnessother", "other_medically_important"),
)


def _flag(node, *names) -> bool | None:
    """True, False, or None for "the file did not say".

    The three-way answer is the point. A seriousness flag that defaults to
    False turns an absent statement into a negative determination, and a case
    that nobody assessed becomes a case somebody assessed as not serious.
    """
    value = _text(node, *names)
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "y"):
        return True
    if lowered in ("2", "0", "false", "no", "n"):
        return False
    return None


def _decode(value: str | None, table: dict) -> str | None:
    if value is None:
        return None
    return table.get(value.strip(), value.strip())


# ------------------------------------------------------------- the case itself

def _age_in_years(raw: str | None, unit: str | None) -> float | None:
    """E2B reports an age with its unit: 801 years, 802 months, 803 weeks,
    804 days, 805 hours. Stored in years so two cases can be compared."""
    try:
        value = float((raw or "").strip())
    except ValueError:
        return None
    factor = {"801": 1.0, "802": 1 / 12, "803": 1 / 52.18, "804": 1 / 365.25,
              "805": 1 / 8766.0, "year": 1.0, "years": 1.0,
              "month": 1 / 12, "months": 1 / 12,
              "day": 1 / 365.25, "days": 1 / 365.25}.get(
        (unit or "801").strip().lower(), 1.0)
    return round(value * factor, 4)


def _parse_events(report) -> list:
    events = []
    for node in _all(report, "reaction", "reactionmeddra"):
        parsed = ParsedEvent(
            verbatim_term=_text(node, "primarysourcereaction",
                                "reactionmeddrallt", "value"),
            meddra_llt=_text(node, "reactionmeddrallt"),
            meddra_pt=_text(node, "reactionmeddrapt"),
            meddra_version=_text(node, "reactionmeddraversionpt",
                                 "reactionmeddraversionllt"),
            outcome=_decode(_text(node, "reactionoutcome"), _OUTCOME),
        )
        onset, precision = parse_date(_text(node, "reactionstartdate"))
        parsed.onset_date = onset
        if precision and precision != "day":
            parsed.__dict__.setdefault("_precision", precision)
        if parsed.verbatim_term or parsed.meddra_pt:
            events.append(parsed)
    return events


def _parse_drugs(report, *, product_names) -> list:
    drugs = []
    wanted = {n.strip().lower() for n in product_names if n and n.strip()}
    for node in _all(report, "drug"):
        name = _text(node, "medicinalproduct", "activesubstancename")
        parsed = ParsedDrug(
            drug_name=name,
            role=_decode(_text(node, "drugcharacterization"), _DRUG_ROLE),
            dose=_text(node, "drugdosagetext", "drugstructuredosagenumb"),
            dose_unit=_text(node, "drugstructuredosageunit"),
            frequency=_text(node, "drugintervaldosagedefinition"),
            route=_text(node, "drugadministrationroute"),
            indication=_text(node, "drugindication", "drugindicationmeddrapt"),
            action_taken=_decode(_text(node, "actiondrug"), _ACTION),
            dechallenge=_decode(_text(node, "drugrecurreadministration"), _CHALLENGE),
            rechallenge=_decode(_text(node, "drugrechallenge"), _CHALLENGE),
        )
        parsed.start_date, _p = parse_date(_text(node, "drugstartdate"))
        parsed.end_date, _p = parse_date(_text(node, "drugenddate"))
        # Whether this is the company's own product is decided by matching the
        # names the caller supplied, never by the file: an ICSR names a drug,
        # it does not know whose portfolio it is in.
        parsed.is_company_product = bool(
            name and wanted and any(w in name.strip().lower() for w in wanted))
        if name:
            drugs.append(parsed)
    return drugs


def _parse_report(report, *, product_names) -> ParsedCase:
    case = ParsedCase()
    case.worldwide_case_id = _text(report, "safetyreportid", "companynumb")
    local = _text(report, "companynumb")
    if local and local != case.worldwide_case_id:
        case.local_case_ids = [local]
    version = _text(report, "safetyreportversion")
    if version and version.strip().isdigit():
        case.case_version = int(version.strip())

    case.country_of_occurrence = _text(report, "occurcountry", "primarysourcecountry")
    case.report_source = _decode(_text(report, "reporttype"), _REPORT_SOURCE)
    case.study_id = _text(report, "studyname", "studyregistrationnumber")
    if case.study_id and case.report_source is None:
        case.report_source = "clinical_trial"

    source = _first(report, "primarysource")
    if source is not None:
        case.primary_reporter_qualification = _decode(
            _text(source, "qualification"), _QUALIFICATION)
        case.is_medically_confirmed = _flag(source, "medicalconfirm")

    initial, precision = parse_date(_text(report, "receivedate"))
    case.initial_receipt_date = initial
    if precision and precision != "day":
        case.date_precision["initial_receipt_date"] = precision
    latest, precision = parse_date(_text(report, "receiptdate"))
    case.latest_receipt_date = latest or initial
    if precision and precision != "day":
        case.date_precision["latest_receipt_date"] = precision

    case.is_serious = _flag(report, "serious")
    for element, label in _SERIOUSNESS:
        if _flag(report, element):
            if label not in case.seriousness_criteria:
                case.seriousness_criteria.append(label)
    # A case with a criterion but no seriousness flag IS serious: the criterion
    # is the statement. The reverse is not true and is not inferred.
    if case.seriousness_criteria and case.is_serious is None:
        case.is_serious = True

    patient = _first(report, "patient")
    if patient is not None:
        case.patient_age = _age_in_years(
            _text(patient, "patientonsetage"), _text(patient, "patientonsetageunit"))
        case.patient_age_group = _text(patient, "patientagegroup")
        case.patient_sex = _decode(_text(patient, "patientsex"), _SEX)
        case.case_outcome = _decode(_text(patient, "patientdeath"), _OUTCOME)
        summary = _first(patient, "summary")
        if summary is not None:
            case.narrative = _text(summary, "narrativeincludeclinical")
        case.is_pregnancy_case = bool(_first(patient, "parentidentification")) or None

    case.events = _parse_events(report)
    case.drugs = _parse_drugs(report, product_names=product_names)

    # What is missing is recorded, because an absent field in a safety database
    # export is usually a mapping problem rather than an empty fact, and a
    # silence is the one thing a reviewer cannot see.
    for name, value in (("worldwide_case_id", case.worldwide_case_id),
                        ("latest_receipt_date", case.latest_receipt_date),
                        ("country_of_occurrence", case.country_of_occurrence),
                        ("report_source", case.report_source)):
        if not value:
            case.unmapped[name] = "not present in the file"
    if not case.events:
        case.unmapped["events"] = "no reaction element carried a term"
    if case.is_serious is None:
        case.unmapped["is_serious"] = (
            "the file states no seriousness; it is a determination, not a default")
    return case


def parse_icsr(data: bytes, *, product_names=()) -> list[ParsedCase]:
    """Every case in one ICSR file.

    A single export routinely carries many `safetyreport` elements, and R3
    wraps each in its own act. Both are walked the same way: find the report
    containers, read each one, and if there are none treat the document itself
    as a single report -- which is what a CIOMS-style single-case XML looks
    like.
    """
    root = _read(data)
    reports = _all(root, "safetyreport", "investigationevent", "icsr")
    # `_all` walks descendants including nested matches; keep only the
    # outermost, or a nested element would be read twice.
    outermost = [r for r in reports
                 if not any(other is not r and other in r.iterancestors()
                            for other in reports)]
    if not outermost:
        outermost = [root]
    cases = [_parse_report(report, product_names=product_names)
             for report in outermost]
    real = [c for c in cases if c.worldwide_case_id or c.events or c.drugs]
    if not real:
        raise E2bUnreadable(
            "no safety report in this file carried a case identifier, a reaction "
            "or a drug; it may be an E2B acknowledgement rather than an ICSR")
    return real

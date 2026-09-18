"""Reading a line listing out of whatever the safety database exported.

Argus, ArisG, Vault Safety and LifeSphere all export the same facts under
different column headings, so the mapping between a spreadsheet's columns and
this store's fields is per-source-system rather than universal. `suggest` makes
the first cycle quick and `PvMappingProfile` makes every cycle after it one
click -- which is the whole point, because a mapping redone by hand each
quarter is a mapping that is slightly different each quarter.

Two things about the shape.

**A line listing is event-level, not case-level.** One case with three
reactions is three rows carrying the same case identifier, and reading each row
as a case would treble the count of everything. Rows are therefore grouped by
case identifier and the events accumulated -- and a row with no identifier is
refused rather than being given one, because a case that cannot be tied to its
siblings is a case that will be counted separately from them.

**An ambiguous date is refused.** `03/04/2026` is the third of April in half
the world and the fourth of March in the other, and either reading is a
receipt date that decides which reporting interval a case falls into. Guessing
wrong moves a case between reports. So a numeric date whose first two
components are both twelve or less is not parsed at all unless the mapping
profile states the order -- the same refusal `app.cmc.values` makes for a bare
comma decimal, for the same reason.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from app.safety.e2b import ParsedCase, ParsedDrug, ParsedEvent

#: The fields a column can be mapped to. Deliberately a small set: a line
#: listing is a summary, and anything it does not carry belongs in the ICSR.
FIELDS = {
    "worldwide_case_id": "Case identifier (required)",
    "local_case_id": "Local case identifier",
    "case_version": "Case version",
    "initial_receipt_date": "Initial receipt date",
    "latest_receipt_date": "Latest receipt date",
    "country_of_occurrence": "Country of occurrence",
    "report_source": "Report source",
    "study_id": "Study identifier",
    "primary_reporter_qualification": "Reporter qualification",
    "is_serious": "Serious (yes/no)",
    "seriousness_criteria": "Seriousness criteria",
    "case_outcome": "Case outcome",
    "patient_age": "Patient age",
    "patient_sex": "Patient sex",
    "verbatim_term": "Reported term (verbatim)",
    "meddra_pt": "MedDRA preferred term",
    "meddra_llt": "MedDRA lowest level term",
    "meddra_soc": "MedDRA system organ class",
    "meddra_version": "MedDRA version",
    "event_onset_date": "Event onset date",
    "event_outcome": "Event outcome",
    "drug_name": "Suspect drug",
    "drug_role": "Drug role",
    "drug_indication": "Drug indication",
    "narrative": "Narrative",
}

#: The one field without which a row cannot be filed.
REQUIRED_FIELD = "worldwide_case_id"

#: Header fragments that identify a column, most specific first. Order is
#: load-bearing: "case id" and "local case id" both contain "case id", so the
#: longer pattern has to be tried first or every local id becomes the
#: worldwide one.
_SIGNALS = (
    ("local_case_id", ("local case", "local id", "local number", "local no")),
    ("worldwide_case_id", ("worldwide case", "world wide case", "case number",
                           "case id", "case no", "caseid", "case reference",
                           "aer number", "icsr number", "report id",
                           "safety report id")),
    ("case_version", ("case version", "version number", "follow-up number")),
    ("initial_receipt_date", ("initial receipt", "initial received", "first received",
                              "date received", "receipt date", "received date")),
    ("latest_receipt_date", ("latest receipt", "most recent receipt", "last received",
                             "latest received", "follow-up receipt")),
    ("country_of_occurrence", ("country of occurrence", "occurrence country",
                               "country", "occurcountry")),
    ("report_source", ("report source", "report type", "source of report",
                       "case type")),
    ("study_id", ("study id", "study number", "protocol number", "study name",
                  "trial id")),
    ("primary_reporter_qualification", ("reporter qualification", "reporter type",
                                        "qualification", "reporter")),
    ("is_serious", ("serious", "seriousness flag", "serious case")),
    ("seriousness_criteria", ("seriousness criteria", "seriousness criterion",
                              "criteria for seriousness", "serious criteria")),
    ("case_outcome", ("case outcome", "patient outcome")),
    ("patient_age", ("patient age", "age at onset", "age")),
    ("patient_sex", ("patient sex", "sex", "gender")),
    ("verbatim_term", ("verbatim", "reported term", "reporter term",
                       "adverse event as reported", "event as reported")),
    ("meddra_pt", ("preferred term", "meddra pt", " pt", "pt name", "pt ")),
    ("meddra_llt", ("lowest level term", "meddra llt", "llt")),
    ("meddra_soc", ("system organ class", "meddra soc", "soc")),
    ("meddra_version", ("meddra version", "meddra ver")),
    ("event_onset_date", ("onset date", "event onset", "date of onset",
                          "reaction start")),
    ("event_outcome", ("event outcome", "reaction outcome", "outcome of event")),
    ("drug_name", ("suspect drug", "medicinal product", "drug name", "product name",
                   "suspect product")),
    ("drug_role", ("drug role", "drug characterisation", "drug characterization",
                   "role")),
    ("drug_indication", ("indication",)),
    ("narrative", ("narrative", "case narrative", "clinical course")),
)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

_ISO = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$")
_NAMED = re.compile(r"^\s*(\d{1,2})[-/ ]([A-Za-z]{3,})[-/ ](\d{2,4})\s*$")
_NUMERIC = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\s*$")
_COMPACT = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})\s*$")

#: What the profile may say about how its source writes dates.
DATE_ORDER_KEY = "_date_order"
DAY_FIRST, MONTH_FIRST = "day_first", "month_first"


class AmbiguousDate(ValueError):
    """A numeric date that could be read two ways, with no order declared."""


@dataclass
class MappingSuggestion:
    column: str
    field: str | None
    confidence: float


@dataclass
class ReadResult:
    cases: list = field(default_factory=list)
    #: Row number -> why it was not used. Rows are never silently dropped.
    skipped: dict = field(default_factory=dict)
    #: Column headers the mapping did not claim.
    unmapped_columns: list = field(default_factory=list)
    rows_read: int = 0


def _fold(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def suggest(headers) -> list[MappingSuggestion]:
    """A first guess at what each column is.

    A guess, and labelled as one: the mapping screen shows it for confirmation
    rather than applying it. A column mapped wrongly puts one field's values in
    another field's column, and every figure downstream is then right about the
    wrong thing.
    """
    taken: set = set()
    out = []
    for header in headers:
        folded = _fold(header)
        chosen, score = None, 0.0
        if folded:
            for name, fragments in _SIGNALS:
                if name in taken:
                    continue
                for fragment in fragments:
                    piece = _fold(fragment)
                    if folded == piece:
                        chosen, score = name, 1.0
                        break
                    if piece and piece in folded:
                        chosen, score = name, 0.7
                        break
                if chosen:
                    break
        if chosen:
            taken.add(chosen)
        out.append(MappingSuggestion(column=header, field=chosen, confidence=score))
    return out


def parse_date(raw, *, order: str | None = None) -> date | None:
    """A date from a spreadsheet cell, or a refusal.

    Raises `AmbiguousDate` for a numeric date that could be read two ways when
    the profile has not said which. That is the whole discipline of this
    function: `03/04/2026` decides which reporting interval a case falls into,
    and a guess that is wrong by a month moves a case between two reports that
    are both then wrong.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None

    match = _ISO.match(text) or _COMPACT.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _safe(year, month, day)

    match = _NAMED.match(text)
    if match:
        day, name, year = match.groups()
        month = _MONTHS.get(name[:3].lower())
        if month:
            year_value = int(year)
            if year_value < 100:
                year_value += 2000 if year_value < 70 else 1900
            return _safe(year_value, month, int(day))
        return None

    match = _NUMERIC.match(text)
    if match:
        first, second, year = (int(g) for g in match.groups())
        if first > 12 and second <= 12:
            return _safe(year, second, first)      # unambiguously day first
        if second > 12 and first <= 12:
            return _safe(year, first, second)      # unambiguously month first
        if first <= 12 and second <= 12:
            if order == DAY_FIRST:
                return _safe(year, second, first)
            if order == MONTH_FIRST:
                return _safe(year, first, second)
            raise AmbiguousDate(
                f"{text!r} could be {first:02d}/{second:02d} or "
                f"{second:02d}/{first:02d}; the mapping profile does not say which "
                "order this source writes dates in")
        return None
    return None


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


_TRUE = {"1", "y", "yes", "true", "serious", "s"}
_FALSE = {"0", "n", "no", "false", "non-serious", "nonserious", "not serious"}


def parse_flag(raw) -> bool | None:
    """True, False, or "the cell did not say". Never a default."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _split(raw) -> list:
    if raw is None:
        return []
    return [piece.strip() for piece in re.split(r"[;,|/]", str(raw)) if piece.strip()]


def read_rows(headers, rows, mapping: dict, *, product_names=()) -> ReadResult:
    """Rows into cases, grouped by case identifier.

    `mapping` is column header -> field name, plus the optional
    `_date_order` entry. Anything the mapping does not claim is reported as
    unmapped rather than ignored: a column nobody mapped may be the one holding
    the seriousness criteria.
    """
    order = mapping.get(DATE_ORDER_KEY)
    by_column = {column: field_name for column, field_name in mapping.items()
                 if column != DATE_ORDER_KEY and field_name in FIELDS}
    index = {header: position for position, header in enumerate(headers)}
    result = ReadResult(rows_read=len(rows))
    result.unmapped_columns = [h for h in headers if h and h not in by_column]

    grouped: dict = {}
    for number, row in enumerate(rows, start=2):   # header is row 1
        def cell(name):
            column = next((c for c, f in by_column.items() if f == name), None)
            if column is None:
                return None
            position = index.get(column)
            if position is None or position >= len(row):
                return None
            value = row[position]
            return value if value not in ("", None) else None

        case_id = cell("worldwide_case_id")
        if not case_id:
            result.skipped[number] = (
                "no case identifier, so this row cannot be joined to the rest of "
                "its case")
            continue
        case_id = str(case_id).strip()

        try:
            dates = {
                "initial_receipt_date": parse_date(cell("initial_receipt_date"),
                                                   order=order),
                "latest_receipt_date": parse_date(cell("latest_receipt_date"),
                                                  order=order),
                "event_onset_date": parse_date(cell("event_onset_date"), order=order),
            }
        except AmbiguousDate as exc:
            result.skipped[number] = str(exc)
            continue

        case = grouped.get(case_id)
        if case is None:
            case = ParsedCase(worldwide_case_id=case_id)
            grouped[case_id] = case
            local = cell("local_case_id")
            if local:
                case.local_case_ids = [str(local).strip()]
            version = cell("case_version")
            if version is not None and str(version).strip().isdigit():
                case.case_version = int(str(version).strip())
            case.initial_receipt_date = dates["initial_receipt_date"]
            case.latest_receipt_date = (dates["latest_receipt_date"]
                                        or dates["initial_receipt_date"])
            country = cell("country_of_occurrence")
            case.country_of_occurrence = str(country).strip() if country else None
            source = cell("report_source")
            case.report_source = _fold(str(source)).replace(" ", "_") if source else None
            study = cell("study_id")
            case.study_id = str(study).strip() if study else None
            qualification = cell("primary_reporter_qualification")
            case.primary_reporter_qualification = (
                str(qualification).strip() if qualification else None)
            case.is_serious = parse_flag(cell("is_serious"))
            case.seriousness_criteria = [
                _fold(c).replace(" ", "_") for c in _split(cell("seriousness_criteria"))]
            if case.seriousness_criteria and case.is_serious is None:
                case.is_serious = True
            outcome = cell("case_outcome")
            case.case_outcome = str(outcome).strip() if outcome else None
            age = cell("patient_age")
            try:
                case.patient_age = float(str(age).strip()) if age is not None else None
            except ValueError:
                case.patient_age = None
            sex = cell("patient_sex")
            case.patient_sex = _fold(str(sex)) or None if sex else None
            narrative = cell("narrative")
            case.narrative = str(narrative).strip() if narrative else None
            if case.latest_receipt_date is None:
                case.unmapped["latest_receipt_date"] = (
                    "no receipt date, so this case falls in no reporting interval")

        verbatim = cell("verbatim_term")
        pt = cell("meddra_pt")
        if verbatim or pt:
            case.events.append(ParsedEvent(
                verbatim_term=str(verbatim).strip() if verbatim else None,
                meddra_pt=str(pt).strip() if pt else None,
                meddra_llt=(str(cell("meddra_llt")).strip()
                            if cell("meddra_llt") else None),
                meddra_soc=(str(cell("meddra_soc")).strip()
                            if cell("meddra_soc") else None),
                meddra_version=(str(cell("meddra_version")).strip()
                                if cell("meddra_version") else None),
                onset_date=dates["event_onset_date"],
                outcome=(str(cell("event_outcome")).strip()
                         if cell("event_outcome") else None),
            ))

        drug = cell("drug_name")
        if drug:
            name = str(drug).strip()
            wanted = {n.strip().lower() for n in product_names if n and n.strip()}
            if not any(d.drug_name == name for d in case.drugs):
                case.drugs.append(ParsedDrug(
                    drug_name=name,
                    role=(_fold(str(cell("drug_role"))) or None
                          if cell("drug_role") else "suspect"),
                    indication=(str(cell("drug_indication")).strip()
                                if cell("drug_indication") else None),
                    is_company_product=bool(
                        wanted and any(w in name.lower() for w in wanted)),
                ))

    for case in grouped.values():
        if not case.events:
            case.unmapped["events"] = "no row for this case carried a term"
    result.cases = list(grouped.values())
    return result


def validate_mapping(mapping: dict) -> list[str]:
    """What is wrong with a mapping, before it is used on anything."""
    problems = []
    fields = [f for column, f in mapping.items() if column != DATE_ORDER_KEY]
    if REQUIRED_FIELD not in fields:
        problems.append(
            f"no column is mapped to {FIELDS[REQUIRED_FIELD]}; without it rows "
            "cannot be grouped into cases")
    for name in fields:
        if name not in FIELDS:
            problems.append(f"{name!r} is not a field this store holds")
    duplicated = {f for f in fields if fields.count(f) > 1}
    if duplicated:
        problems.append(
            "two columns are mapped to the same field: " + ", ".join(sorted(duplicated)))
    order = mapping.get(DATE_ORDER_KEY)
    if order is not None and order not in (DAY_FIRST, MONTH_FIRST):
        problems.append(
            f"{DATE_ORDER_KEY} must be {DAY_FIRST!r} or {MONTH_FIRST!r}")
    return problems

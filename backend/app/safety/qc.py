"""§11: everything that stands between a periodic safety report and export.

Two design decisions come from defects the CMC module shipped with.

**This is the only gate.** CMC had a QC dashboard and a separate export gate,
each checking what the other did not, and a dossier could pass both while
failing either -- the export never called QC. Here, `run_qc` IS the export
gate: section approval and qualified-person sign-off are findings in this list,
so the screen that says "exportable" and the endpoint that exports are asking
one question.

**One broken check does not silence the rest.** In CMC a single builder
exception took every QC check down with it. Each check here runs on its own,
and a check that raises becomes a BLOCKER naming itself -- failing closed,
because a check that could not run has not passed.

The checks are grouped as §11 groups them. Blockers stop export; warnings and
information are shown and recorded but do not. Every finding says what is
wrong and where, because "QC failed" is not something anybody can act on.
"""

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.docgen.markers import parse_assessments, parse_data_needed, table_markers
from app.safety import deident
from app.safety import expectedness as exp
from app.safety import scope as scope_mod

BLOCKER = "blocker"
WARNING = "warning"
INFO = "info"


#: Blockers a qualified person may accept, with a reason, rather than fix.
#: Only the checks that read prose with a pattern, or compare against a figure
#: that may legitimately have moved: "a published series of 12 patients" is a
#: count the case store did not produce and is still correct. Nothing about
#: identifiers, unconfirmed data, approval or the lock point is on this list --
#: those are not judgments, they are states, and a state is fixed, not accepted.
ACCEPTABLE = frozenset({
    "PROSE_FIGURE_UNSOURCED", "RATE_WITHOUT_DENOMINATOR", "CUMULATIVE_DECREASED",
})


@dataclass
class PvFinding:
    code: str
    severity: str
    message: str
    section_code: str | None = None
    #: Ids and evidence, so the dashboard can link to the section, the grid
    #: cell or the case. JSON-safe: this crosses the wire.
    detail: dict = field(default_factory=dict)
    #: Stable for as long as the finding says the same thing. An acceptance is
    #: recorded against this, so when the figure in the message changes the
    #: finding is new and has to be accepted again.
    key: str = ""

    def __post_init__(self):
        if not self.key:
            self.key = hashlib.sha1(
                f"{self.code}|{self.section_code or ''}|{self.message}".encode()
            ).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {"key": self.key, "code": self.code, "severity": self.severity,
                "message": self.message, "section_code": self.section_code,
                "detail": dict(self.detail),
                "acceptable": self.severity == BLOCKER and self.code in ACCEPTABLE}


@dataclass
class _Context:
    """Everything a check reads, loaded once."""

    db: object
    report: object
    product: object
    scope: scope_mod.Scope
    sections: list
    drafts: dict          # section id -> latest draft
    tables: dict          # table key -> Tabulation, or the exception it raised


# ------------------------------------------------------------ the prose checks

#: A count in prose: a number followed by the noun it counts. The noun list is
#: deliberately the report's own vocabulary -- "2 hours after the second dose"
#: is not a count, and a check that flagged it would teach people to ignore
#: this one.
_COUNT_RE = re.compile(
    r"(?<![\w.,/-])(?P<n>\d{1,3}(?:,\d{3})+|\d+)\s+(?:new\s+|serious\s+|non-serious\s+|"
    r"fatal\s+|spontaneous\s+|unlisted\s+|listed\s+|medically\s+confirmed\s+)*"
    r"(?P<noun>cases?|events?|reports?|reactions?|patients?|subjects?|deaths?|ICSRs?)\b",
    re.IGNORECASE)

#: A rate, stated any of the ways a report states one.
_RATE_RE = re.compile(
    r"\b(?:reporting\s+rate|incidence(?:\s+rate)?|rate\s+of|per\s+[\d,]+\s+"
    r"(?:patient[- ]years?|patients|subjects|prescriptions|units)|"
    r"per\s+(?:100|1,?000|10,?000|100,?000|1,?000,?000))\b", re.IGNORECASE)

_CITATION_RE = re.compile(r"\[S\d+[^\]]*\]")


#: A numbered heading: "4 Case Series Review", "7.3 Cumulative ...", "II.SVIII
#: Summary of ...". Rule 11 makes every draft open with one, and "4 Case" is a
#: section number followed by a title, not a count of four cases.
_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVX]+(?:\.[A-Z]+)*)\s+[A-Z]")


def _prose(content: str) -> str:
    """The draft without its headings, table lines and citation markers -- the
    words a reader reads, which are the words a figure can be quoted in."""
    lines = [line for line in (content or "").splitlines()
             if not re.match(r"^\s*\[TABLE:", line) and not _HEADING_RE.match(line)]
    return _CITATION_RE.sub("", "\n".join(lines))


def _allowed_figures(ctx) -> set:
    """Every count the report is entitled to quote: the scope's case counts and
    every total every table computed. A figure in prose that is not one of
    these was typed, and §2's second principle says the model never counts."""
    allowed: set = set()
    counts = scope_mod.preview(ctx.db, ctx.scope)
    for key in ("interval_cases", "interval_events", "cumulative_cases",
                "cumulative_events", "new_since_baseline"):
        if counts.get(key) is not None:
            allowed.add(int(counts[key]))
    for built in ctx.tables.values():
        if isinstance(built, Exception):
            continue
        for value in built.totals.values():
            if isinstance(value, int):
                allowed.add(value)
        for row in built.rows:
            for cell in row:
                if re.fullmatch(r"\d+", str(cell)):
                    allowed.add(int(cell))
    return allowed


# ------------------------------------------------------------------ blockers

def _deid(ctx) -> list:
    """§11.1: the queue is clear, and nothing that reaches the document holds
    an identifier."""
    from app.models import PvChunk, PvDeidItem, PvDocument

    out = []
    db, product_id = ctx.db, ctx.product.id
    pending = db.scalar(select(func.count(PvDeidItem.id)).where(
        PvDeidItem.pv_product_id == product_id, PvDeidItem.status == "pending")) or 0
    waiting = db.scalar(select(func.count(PvDocument.id)).where(
        PvDocument.pv_product_id == product_id,
        PvDocument.processing_status == "awaiting_deid")) or 0
    if pending or waiting:
        out.append(PvFinding(
            "DEID_QUEUE_OPEN", BLOCKER,
            f"{pending} de-identification detection(s) are unanswered and {waiting} "
            "source(s) are held behind them.",
            detail={"pending": pending, "documents_waiting": waiting}))
    confirmed = deident.confirmed_identifiers(db, product_id)
    for section in ctx.sections:
        draft = ctx.drafts.get(section.id)
        if draft is None:
            continue
        hits = [(h.identifier_type, h.text) for h in deident.scan(draft.content)]
        hits += [("confirmed identifier", value)
                 for value in deident.confirmed_in(draft.content, confirmed)]
        if hits:
            out.append(PvFinding(
                "PII_IN_DRAFT", BLOCKER,
                f"{section.section_code} contains "
                f"{len({text.lower() for _, text in hits})} likely identifier(s): "
                + ", ".join(sorted({kind.replace('_', ' ') for kind, _ in hits})),
                section.section_code,
                {"section_id": section.id,
                 "found": list(dict.fromkeys(text for _, text in hits))[:10]}))
    leaked_chunks = 0
    for chunk in db.scalars(select(PvChunk).where(
            PvChunk.pv_product_id == product_id)).all():
        if deident.scan(chunk.content or ""):
            leaked_chunks += 1
    if leaked_chunks:
        out.append(PvFinding(
            "PII_IN_INDEX", BLOCKER,
            f"{leaked_chunks} indexed chunk(s) contain a likely identifier. They are "
            "what drafting retrieves from.", detail={"chunks": leaked_chunks}))
    return out


def _unconfirmed(ctx) -> list:
    """§11.2: nothing unconfirmed feeds an enabled data section."""
    from app.models import PvCase, PvCaseEvent, PvExposure

    out = []
    data_sections = [s for s in ctx.sections if s.enabled and s.table_key]
    if not data_sections:
        return out
    windows = [scope_mod.interval(ctx.scope)]
    if ctx.scope.has_cumulative:
        windows.append(scope_mod.cumulative(ctx.scope))
    from sqlalchemy import or_

    unconfirmed = ctx.db.scalar(select(func.count(PvCaseEvent.id)).where(
        PvCaseEvent.confirmed_by.is_(None),
        PvCaseEvent.case_id.in_(select(PvCase.id).where(or_(*windows))))) or 0
    case_tables = {"summary_tab_soc_pt", "summary_tab_trials", "line_listing_sar"}
    affected = [s.section_code for s in data_sections if s.table_key in case_tables]
    if unconfirmed and affected:
        out.append(PvFinding(
            "UNCONFIRMED_DATA", BLOCKER,
            f"{unconfirmed} event(s) in this report's windows have no confirmed "
            f"determination, and feed section(s) {', '.join(affected)}.",
            detail={"unconfirmed_events": unconfirmed, "sections": affected}))
    if any(s.table_key == "exposure_table" for s in data_sections):
        open_exposure = ctx.db.scalar(select(func.count(PvExposure.id)).where(
            PvExposure.report_instance_id == ctx.report.id,
            PvExposure.confirmed_by.is_(None))) or 0
        if open_exposure:
            out.append(PvFinding(
                "UNCONFIRMED_EXPOSURE", BLOCKER,
                f"{open_exposure} exposure figure(s) are not confirmed by a reviewer.",
                detail={"unconfirmed_exposure": open_exposure}))
    return out


def _reconciliation(ctx) -> list:
    """§11.3: the listing, the summary and the prose agree.

    The summary's interval serious total must equal the line listing's reactions
    plus the serious events assessed unrelated plus those nobody assessed --
    the three ways a serious event can be accounted for. And every count quoted
    in prose must be one the store produced.
    """
    from app.safety import tabulations as tab

    out = []
    summary = ctx.tables.get("summary_tab_soc_pt")
    listing = ctx.tables.get("line_listing_sar")
    if isinstance(summary, tab.Tabulation) and isinstance(listing, tab.Tabulation):
        serious = sum(summary.totals.get(f"interval_serious_{b}", 0)
                      for b in ("unlisted", "listed", "unconfirmed"))
        accounted = (listing.totals.get("interval_sar_rows", 0)
                     + listing.totals.get("interval_serious_unassessed", 0)
                     + _serious_unrelated(ctx))
        if serious != accounted:
            out.append(PvFinding(
                "COUNT_MISMATCH", BLOCKER,
                f"The summary tabulation counts {serious} serious event(s) in the "
                f"interval, and the line listing accounts for {accounted}.",
                detail={"summary_serious": serious, "listing_accounted": accounted}))

    allowed = _allowed_figures(ctx)
    for section in ctx.sections:
        draft = ctx.drafts.get(section.id)
        if draft is None:
            continue
        unsourced = []
        for match in _COUNT_RE.finditer(_prose(draft.content)):
            value = int(match.group("n").replace(",", ""))
            if value not in allowed:
                unsourced.append(match.group(0).strip())
        if unsourced:
            out.append(PvFinding(
                "PROSE_FIGURE_UNSOURCED", BLOCKER,
                f"{section.section_code} quotes {len(unsourced)} count(s) the case "
                f"store did not produce: {'; '.join(unsourced[:5])}.",
                section.section_code,
                {"section_id": section.id, "figures": unsourced[:20]}))
    return out


def _serious_unrelated(ctx) -> int:
    from app.models import PvCase, PvCaseEvent
    from app.safety import tabulations as tab

    cases = {c.id: c for c in ctx.db.scalars(select(PvCase).where(
        scope_mod.interval(ctx.scope))).all()}
    if not cases:
        return 0
    count = 0
    for event in ctx.db.scalars(select(PvCaseEvent).where(
            PvCaseEvent.case_id.in_(list(cases)))).all():
        if tab.is_serious(event, cases[event.case_id]) and tab.is_related(event) is False:
            count += 1
    return count


def _windows(ctx) -> list:
    """§11.4: cumulative is at least interval, at least the last report's
    cumulative, and nothing after the lock is inside either."""
    from app.models import PvCase, PvReportInstance

    out = []
    counts = scope_mod.preview(ctx.db, ctx.scope)
    if counts.get("cumulative_cases") is not None \
            and counts["cumulative_cases"] < counts["interval_cases"]:
        out.append(PvFinding(
            "CUMULATIVE_BELOW_INTERVAL", BLOCKER,
            f"Cumulative cases ({counts['cumulative_cases']}) are fewer than interval "
            f"cases ({counts['interval_cases']}). The interval starts before the "
            f"{ctx.scope.anchor.upper()} it is meant to count from.",
            detail=counts))
    if ctx.report.baseline_report_id and counts.get("cumulative_cases") is not None:
        baseline = ctx.db.get(PvReportInstance, ctx.report.baseline_report_id)
        stated = (baseline.figures_at_signoff or {}) if baseline is not None else {}
        # Compared against what the previous report STATED, frozen at its
        # sign-off. A recomputation of its window over today's store can never
        # exceed this report's own count, so it would pass every time.
        if stated.get("cumulative_cases") is not None:
            if counts["cumulative_cases"] < stated["cumulative_cases"]:
                out.append(PvFinding(
                    "CUMULATIVE_DECREASED", BLOCKER,
                    f"This report's cumulative count ({counts['cumulative_cases']}) is "
                    f"below the {stated['cumulative_cases']} the previous report "
                    "stated. Cases were removed, or updated after this report's lock "
                    "and so excluded from it -- either way a person has to look.",
                    detail={"this": counts["cumulative_cases"],
                            "previous": stated["cumulative_cases"]}))
        elif baseline is not None:
            out.append(PvFinding(
                "BASELINE_FIGURES_UNRECORDED", INFO,
                "The previous report has no figures recorded at sign-off, so whether "
                "the cumulative count fell between the two cannot be checked."))
    # Structural, and checked anyway: the table builders filter through the
    # scope layer, and this confirms no cell holds a case the lock excludes.
    late = set(c.id for c in ctx.db.scalars(select(PvCase).where(
        scope_mod.after_lock(ctx.scope))).all())
    if late:
        for key, built in ctx.tables.items():
            if isinstance(built, Exception):
                continue
            leaked = {c for cell in built.cells.values() for c in cell["cases"]} & late
            if leaked:
                out.append(PvFinding(
                    "CASE_AFTER_LOCK_COUNTED", BLOCKER,
                    f"{key} counts {len(leaked)} case(s) received after the data lock "
                    "point.", detail={"table": key, "cases": sorted(leaked)}))
    return out


def _rsi(ctx) -> list:
    """§11.5: every confirmed expectedness is about the version this report
    pins."""
    out = []
    stale = exp.stale_determinations(ctx.db, ctx.report)
    if stale:
        out.append(PvFinding(
            "STALE_EXPECTEDNESS", BLOCKER,
            f"{len(stale)} expectedness determination(s) were made against a different "
            "reference safety information version than this report pins.",
            detail={"events": [e.id for e in stale][:50]}))
    if not ctx.report.rsi_version_id:
        out.append(PvFinding(
            "NO_RSI_PINNED", BLOCKER,
            "This report pins no reference safety information, so no expectedness in "
            "it has anything to be expected against."))
    return out


def _meddra(ctx) -> list:
    """§11.6: one MedDRA version across the report's events."""
    from app.models import PvCase, PvCaseEvent

    versions = {v for (v,) in ctx.db.execute(
        select(PvCaseEvent.meddra_version).where(
            PvCaseEvent.meddra_version.is_not(None),
            PvCaseEvent.case_id.in_(select(PvCase.id).where(
                scope_mod.interval(ctx.scope))))).all()}
    expected = ctx.report.meddra_version
    out = []
    if len(versions) > 1 or (expected and versions and versions != {expected}):
        out.append(PvFinding(
            "MEDDRA_VERSION_MIXED", BLOCKER,
            f"Events in this report are coded to MedDRA {', '.join(sorted(versions))}"
            + (f"; the report declares {expected}" if expected else "")
            + ". Two versions under one set of totals is two SOC hierarchies.",
            detail={"versions": sorted(versions), "declared": expected}))
    return out


def _markers(ctx) -> list:
    """§11.7 and §11.8: every table marker resolves, every gap is filled, every
    judgment is made."""
    from app.safety import tabulations as tab

    out = []
    for section in ctx.sections:
        if not section.enabled or section.is_container:
            continue
        draft = ctx.drafts.get(section.id)
        if draft is None:
            continue
        for key in table_markers(draft.content):
            if key not in tab.BUILDERS:
                out.append(PvFinding(
                    "TABLE_UNKNOWN", BLOCKER,
                    f"{section.section_code} asks for [TABLE: {key}], which no builder "
                    "produces.", section.section_code, {"table_key": key}))
                continue
            built = ctx.tables.get(key)
            if built is None:
                try:
                    built = tab.render(ctx.db, report=ctx.report, product=ctx.product,
                                       table_key=key)
                except tab.TableUnavailable as exc:
                    built = exc
                ctx.tables[key] = built
            if isinstance(built, Exception):
                out.append(PvFinding(
                    "TABLE_UNRESOLVED", BLOCKER,
                    f"{section.section_code}'s [TABLE: {key}] cannot be built: {built}",
                    section.section_code, {"table_key": key}))
        if section.table_key and section.table_key not in table_markers(draft.content):
            out.append(PvFinding(
                "TABLE_MISSING", BLOCKER,
                f"{section.section_code} is a data section whose table is "
                f"[TABLE: {section.table_key}], and its text does not contain it.",
                section.section_code, {"table_key": section.table_key}))
        for gap in parse_data_needed(draft.content):
            out.append(PvFinding(
                "DATA_NEEDED", BLOCKER,
                f"{section.section_code}: [DATA NEEDED: {gap or 'unspecified'}]",
                section.section_code, {"section_id": section.id}))
        for question in parse_assessments(draft.content):
            out.append(PvFinding(
                "ASSESSMENT_REQUIRED", BLOCKER,
                f"{section.section_code}: a qualified person must answer "
                f"[ASSESSMENT REQUIRED: {question or 'unspecified'}]",
                section.section_code, {"section_id": section.id}))
    return out


def _denominator(ctx) -> list:
    """§11.9: a rate needs its exposure."""
    from app.models import PvExposure

    confirmed = ctx.db.scalar(select(func.count(PvExposure.id)).where(
        PvExposure.report_instance_id == ctx.report.id,
        PvExposure.confirmed_by.is_not(None))) or 0
    if confirmed:
        return []
    out = []
    for section in ctx.sections:
        draft = ctx.drafts.get(section.id)
        if draft is not None and _RATE_RE.search(_prose(draft.content)):
            out.append(PvFinding(
                "RATE_WITHOUT_DENOMINATOR", BLOCKER,
                f"{section.section_code} states a rate, and this report has no "
                "confirmed exposure to divide by.", section.section_code))
    return out


def _approval(ctx) -> list:
    """Approval and sign-off, as findings -- so the dashboard's "exportable" and
    the export endpoint's gate are one question."""
    out = []
    for section in ctx.sections:
        if not section.enabled or section.is_container:
            continue
        if ctx.drafts.get(section.id) is None:
            out.append(PvFinding("SECTION_EMPTY", BLOCKER,
                                 f"{section.section_code} {section.title} has no text.",
                                 section.section_code, {"section_id": section.id}))
        elif section.status != "approved":
            out.append(PvFinding(
                "SECTION_NOT_APPROVED", BLOCKER,
                f"{section.section_code} {section.title} is "
                f"{section.status.replace('_', ' ')}, not approved.",
                section.section_code, {"section_id": section.id}))
    if not ctx.report.qppv_signoff_by:
        out.append(PvFinding(
            "NOT_SIGNED_OFF", BLOCKER,
            "The report has not been signed off by a qualified person."))
    elif ctx.report.figures_at_signoff is not None:
        # The export resolves every table from the store as it is at export
        # time. If a confirmation, a correction or a late case moved a figure
        # after the qualified person signed, the document would print numbers
        # nobody signed for.
        stated = ctx.report.figures_at_signoff
        now_figures = figures(ctx)
        changed = sorted(k for k in now_figures if k != "tables"
                         and stated.get(k) != now_figures[k])
        for key, totals in now_figures["tables"].items():
            if (stated.get("tables") or {}).get(key) != totals:
                changed.append(key)
        if changed:
            out.append(PvFinding(
                "FIGURES_CHANGED_SINCE_SIGNOFF", BLOCKER,
                "Figures have changed since the qualified person signed off ("
                + ", ".join(changed) + "). The report is signed off again before "
                "it is exported.", detail={"changed": changed}))
    return out


def figures(ctx) -> dict:
    """What the report states, as numbers: the scope's counts and every
    total of every table the report prints.

    Frozen at sign-off, compared at export, and read by the next report's
    CUMULATIVE_DECREASED check. One function, so the three agree on what "the
    figures" are.
    """
    counts = scope_mod.preview(ctx.db, ctx.scope)
    out = {key: counts.get(key) for key in (
        "interval_cases", "interval_events", "cumulative_cases", "cumulative_events")}
    keys = {"summary_tab_soc_pt", "line_listing_sar"} | {
        s.table_key for s in ctx.sections if s.enabled and s.table_key}
    tables = {}
    for key in sorted(keys):
        built = ctx.tables.get(key)
        if built is None or isinstance(built, Exception):
            continue
        tables[key] = {name: value for name, value in sorted(built.totals.items())
                       if isinstance(value, (int, float, str)) or value is None}
    out["tables"] = tables
    return out


def figures_for(db, *, report, product) -> dict:
    return figures(_context(db, report, product))


# ------------------------------------------------------------------ warnings

def _duplicates(ctx) -> list:
    from app.models import PvDuplicateCandidate

    pending = ctx.db.scalar(select(func.count(PvDuplicateCandidate.id)).where(
        PvDuplicateCandidate.pv_product_id == ctx.product.id,
        PvDuplicateCandidate.status == "pending")) or 0
    return [PvFinding("DUPLICATES_UNRESOLVED", WARNING,
                      f"{pending} candidate duplicate pair(s) are unresolved.",
                      detail={"pending": pending})] if pending else []


def _coding(ctx) -> list:
    from app.models import PvCase, PvCaseEvent

    uncoded = ctx.db.scalar(select(func.count(PvCaseEvent.id)).where(
        PvCaseEvent.coding_required.is_(True),
        PvCaseEvent.case_id.in_(select(PvCase.id).where(
            scope_mod.interval(ctx.scope))))) or 0
    return [PvFinding("CODING_REQUIRED", WARNING,
                      f"{uncoded} event(s) in the interval carry no MedDRA code and are "
                      "grouped as uncoded in every tabulation.",
                      detail={"uncoded": uncoded})] if uncoded else []


#: Cases that conventionally need a narrative, by seriousness criterion.
NARRATIVE_CRITERIA = frozenset({"death", "life_threatening"})


def _narratives(ctx) -> list:
    from app.models import PvCase, PvCaseEvent, PvCaseNarrative

    cases = ctx.db.scalars(select(PvCase).where(scope_mod.interval(ctx.scope))).all()
    aesi_cases = {e.case_id for e in ctx.db.scalars(select(PvCaseEvent).where(
        PvCaseEvent.is_aesi.is_(True),
        PvCaseEvent.case_id.in_([c.id for c in cases] or [""]))).all()}
    need = [c for c in cases
            if NARRATIVE_CRITERIA & set(c.seriousness_criteria or [])
            or c.id in aesi_cases]
    written = {n.case_id for n in ctx.db.scalars(select(PvCaseNarrative).where(
        PvCaseNarrative.case_id.in_([c.id for c in need] or [""]),
        PvCaseNarrative.generated_text.is_not(None))).all()}
    missing = [c for c in need if c.id not in written]
    return [PvFinding(
        "NARRATIVE_MISSING", WARNING,
        f"{len(missing)} fatal, life-threatening or special-interest case(s) in the "
        "interval have no narrative.",
        detail={"cases": [c.worldwide_case_id or c.id for c in missing][:50]})] \
        if missing else []


def _signals(ctx) -> list:
    from app.models import PvSignal

    out = []
    for signal in ctx.db.scalars(select(PvSignal).where(
            PvSignal.pv_product_id == ctx.product.id)).all():
        label = signal.signal_reference or signal.id
        if not signal.status:
            out.append(PvFinding("SIGNAL_NO_STATUS", WARNING,
                                 f"Signal {label} has no status."))
        elif signal.status == "closed" and not (
                (signal.conclusion or "").strip() and (signal.action_taken or "").strip()):
            out.append(PvFinding(
                "SIGNAL_CLOSED_INCOMPLETE", WARNING,
                f"Signal {label} is closed with no recorded conclusion or action.",
                detail={"signal_id": signal.id}))
    return out


def _continuity(ctx) -> list:
    """§11.14: a label change in the interval should be reflected in the RSI
    library -- an RSI version effective on or after it."""
    from app.models import PvRsiVersion, PvSafetyAction

    out = []
    versions = ctx.db.scalars(select(PvRsiVersion).where(
        PvRsiVersion.pv_product_id == ctx.product.id)).all()
    for action in ctx.db.scalars(select(PvSafetyAction).where(
            PvSafetyAction.pv_product_id == ctx.product.id,
            PvSafetyAction.action_type == "label_change")).all():
        if not action.action_date or not (
                ctx.scope.period_start <= action.action_date <= ctx.scope.period_end):
            continue
        if not any(v.effective_date and v.effective_date >= action.action_date
                   for v in versions):
            out.append(PvFinding(
                "LABEL_CHANGE_NOT_IN_RSI", WARNING,
                f"A label change on {action.action_date} has no reference safety "
                "information version effective on or after it.",
                detail={"action_id": action.id}))
    return out


def _baseline_drift(ctx) -> list:
    """§11.15, the part a machine can see: carried-forward text that quotes a
    count. Rule 6 says an interval figure is never carried forward, and a
    section carried word for word from last interval that says "12 cases" is
    saying last interval's number."""
    out = []
    for section in ctx.sections:
        if section.delta_status != "carried_forward":
            continue
        draft = ctx.drafts.get(section.id)
        if draft is None or draft.origin != "carried_forward":
            continue
        figures = [m.group(0).strip() for m in _COUNT_RE.finditer(_prose(draft.content))]
        if figures:
            out.append(PvFinding(
                "BASELINE_FIGURE_CARRIED", WARNING,
                f"{section.section_code} was carried forward and still quotes "
                f"{', '.join(figures[:3])} from the previous interval.",
                section.section_code, {"section_id": section.id}))
    return out


def _citations(ctx) -> list:
    out = []
    for section in ctx.sections:
        draft = ctx.drafts.get(section.id)
        if draft is None or draft.origin != "model" or section.is_container:
            continue
        if not _CITATION_RE.search(draft.content or "") and len(
                _prose(draft.content).split()) > 40:
            out.append(PvFinding(
                "NO_CITATIONS", WARNING,
                f"{section.section_code} was drafted by the model and cites no source.",
                section.section_code, {"section_id": section.id}))
    return out


def _regions(ctx) -> list:
    from app.models import PvDueDate

    recorded = {d.region for d in ctx.db.scalars(select(PvDueDate).where(
        PvDueDate.report_instance_id == ctx.report.id)).all()}
    missing = [r for r in (ctx.report.regions or []) if r not in recorded]
    out = []
    if missing:
        out.append(PvFinding(
            "REGION_NO_DUE_DATE", WARNING,
            f"No due date is recorded for {', '.join(missing)}. The dates are "
            "informational, and still worth writing down.",
            detail={"regions": missing}))
    for section in ctx.sections:
        if "region" in section.title.lower() and section.enabled \
                and not section.is_container and ctx.report.regions \
                and ctx.drafts.get(section.id) is None:
            out.append(PvFinding(
                "REGION_SECTION_EMPTY", WARNING,
                f"{section.section_code} {section.title} is empty and the report "
                f"targets {', '.join(ctx.report.regions)}.", section.section_code))
    return out


# ---------------------------------------------------------------------- info

def _info(ctx) -> list:
    from app.csr.qc import collect_abbreviations
    from app.models import PvReportInstance

    out = []
    texts = [d.content for d in ctx.drafts.values() if d is not None]
    undefined = [a for a in collect_abbreviations(texts) if not a.get("expansion")]
    if undefined:
        out.append(PvFinding(
            "ABBREVIATIONS_UNDEFINED", INFO,
            f"{len(undefined)} abbreviation(s) are used and never spelled out: "
            + ", ".join(a["abbreviation"] for a in undefined[:10]),
            detail={"abbreviations": [a["abbreviation"] for a in undefined]}))
    counts = scope_mod.preview(ctx.db, ctx.scope)
    if ctx.report.baseline_report_id:
        baseline = ctx.db.get(PvReportInstance, ctx.report.baseline_report_id)
        if baseline is not None:
            before = scope_mod.preview(ctx.db, scope_mod.scope_for(ctx.product, baseline))
            out.append(PvFinding(
                "CASE_VOLUME_TREND", INFO,
                f"{counts['interval_cases']} case(s) this interval against "
                f"{before['interval_cases']} in the previous one.",
                detail={"this": counts["interval_cases"],
                        "previous": before["interval_cases"]}))
    return out


BLOCKER_CHECKS = (_deid, _unconfirmed, _reconciliation, _windows, _rsi, _meddra,
                  _markers, _denominator, _approval)
WARNING_CHECKS = (_duplicates, _coding, _narratives, _signals, _continuity,
                  _baseline_drift, _citations, _regions)
INFO_CHECKS = (_info,)


def _context(db, report, product) -> _Context:
    from app.models import PvSection, PvSectionDraft
    from app.safety import tabulations as tab

    sections = db.scalars(select(PvSection).where(
        PvSection.report_instance_id == report.id).order_by(PvSection.sort_order)).all()
    drafts = {}
    for section in sections:
        drafts[section.id] = db.scalar(select(PvSectionDraft).where(
            PvSectionDraft.pv_section_id == section.id
        ).order_by(PvSectionDraft.version.desc()))
    tables = {}
    for key in ("summary_tab_soc_pt", "line_listing_sar"):
        try:
            tables[key] = tab.render(db, report=report, product=product, table_key=key)
        except tab.TableUnavailable as exc:
            tables[key] = exc
    for section in sections:
        if section.table_key and section.table_key not in tables:
            try:
                tables[section.table_key] = tab.render(
                    db, report=report, product=product, table_key=section.table_key)
            except tab.TableUnavailable as exc:
                tables[section.table_key] = exc
    return _Context(db=db, report=report, product=product,
                    scope=scope_mod.scope_for(product, report), sections=sections,
                    drafts=drafts, tables=tables)


def run_qc(db, *, report, product) -> list:
    """Every §11 check, blockers first.

    A check that raises becomes a blocker naming itself. A check that could not
    run has not passed, and letting its failure take the other checks down with
    it -- as one builder exception did in the CMC module -- would report fewer
    problems precisely when something is most wrong.
    """
    ctx = _context(db, report, product)
    findings: list = []
    for check in BLOCKER_CHECKS + WARNING_CHECKS + INFO_CHECKS:
        try:
            findings.extend(check(ctx))
        except Exception as exc:  # noqa: BLE001 - fail closed, and keep going
            findings.append(PvFinding(
                "QC_CHECK_FAILED", BLOCKER,
                f"The {check.__name__.strip('_').replace('_', ' ')} check could not "
                f"run: {exc}", detail={"check": check.__name__}))
    # An accepted finding stays on the list, as a warning that says who
    # accepted it and why -- a blocker that vanished would leave the record
    # agreeing with the person who waved it through.
    accepted = report.accepted_findings or {}
    for finding in findings:
        record = accepted.get(finding.key)
        if record and finding.severity == BLOCKER and finding.code in ACCEPTABLE:
            finding.severity = WARNING
            finding.detail = {**finding.detail, "accepted_by": record.get("by"),
                              "accepted_at": record.get("at"),
                              "accepted_reason": record.get("reason")}
    order = {BLOCKER: 0, WARNING: 1, INFO: 2}
    return sorted(findings, key=lambda f: order[f.severity])


def exportable(findings) -> bool:
    return not any(f.severity == BLOCKER for f in findings)

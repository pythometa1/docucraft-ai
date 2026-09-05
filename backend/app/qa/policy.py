"""Which QA checks stop a document, and which only annotate it.

The architecture record (§17) lists the per-generation gates and, next to each,
the action on failure -- "Block document", "Block or route to exception
workflow". §6 then puts `qa_policy: { "blocking": [...], "warning": [...] }`
into the manifest envelope, which says the mapping is a property of the
approved contract rather than of the renderer.

Before this module it was neither. Every gate was hard-coded blocking inside
`fill_template`, so the only way an organisation could accept a check it did
not want -- a heuristic overflow estimate on a template whose tables genuinely
overhang, a branch invariant on a template that legitimately shows two
variants -- was to stop running the gate for everybody by editing the renderer.
That is how a QA suite loses the checks that matter: someone turns one off
globally to unblock a batch on a Friday.

Two rules keep the declarative version honest:

  * Silence is not permission. A registered check the policy does not mention
    keeps its default severity; omitting a check never downgrades it.
  * A name the registry does not know is an error, not a no-op. A typo in
    `qa_policy` must fail at resolution, because the alternative is a manifest
    that reads as though a check was configured while the check never ran.

There is deliberately no way to switch a check off. §17 offers "block" or
"route to exception workflow"; the second one is a warning that a human still
sees. A check nobody looks at is not in the policy vocabulary.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

BLOCKING = "blocking"
WARNING = "warning"

SEVERITIES = (BLOCKING, WARNING)

# ---- check names ----
# One per row of the §17 per-generation QA table that is computable for DOCX
# today. The strings are the manifest's vocabulary, so they are stable: renaming
# one silently invalidates every locked manifest that referenced it.
PLACEHOLDER_REMAINS = "placeholder_remains"
UNRESOLVED_MERGEFIELD = "unresolved_mergefield"
CONTROL_TOKEN_REMAINS = "control_token_remains"
INSTRUCTION_TEXT_REMAINS = "instruction_text_remains"
REQUIRED_VALUE_MISSING = "required_value_missing"
BRANCH_SELECTION = "branch_selection"
STATIC_REGION_CHANGED = "static_region_changed"
VALUE_EXCEEDS_MAX_LEN = "value_exceeds_max_len"
VALUE_OVERFLOWS_CELL = "value_overflows_cell"
VALUE_OVERFLOWS_REGION = "value_overflows_region"
# A field the template can never print, because every slot it owns sits in a
# paragraph the compile marked for unconditional deletion. Compile-time.
ORPHANED_FIELD = "orphaned_field"
# A field that resolved to a real value which is nowhere in the finished
# document. Generation-time.
RESOLVED_VALUE_ABSENT = "resolved_value_absent"
# A value that reads wrong because the template's own text and the source value
# both carried the unit or the currency: "$AUD 76,800 per annum per annum".
VALUE_FORMAT_DOUBLED = "value_format_doubled"
# An adjacent repeated word the template does not itself contain.
DOUBLED_WORD = "doubled_word"
# A slot marked with a *mask* rather than a bracket -- `xxxx年xx月xx日`, `xx个月`
# -- that the fill never replaced.
FILL_MASK_REMAINS = "fill_mask_remains"
# A whole date written into a slot that holds one part of one: the year box of
# `xxxx年xx月xx日` carrying `2026-09-01`.
DATE_PART_MALFORMED = "date_part_malformed"
# A §6 TABLE_ROW could not be repeated: the collection is not a list, a
# required row value is absent, or the manifest promises a repeating row the
# document does not carry. One name for every way "one row per record" fails,
# because they are one failure mode -- the table the reader sees does not say
# what the data says.
ROW_REPEAT = "row_repeat"


@dataclass(frozen=True)
class QaCheck:
    name: str
    description: str
    default_severity: str
    # Whether the check runs when the manifest says nothing. Everything that was
    # already enforced is True, so a manifest compiled before `qa_policy` existed
    # behaves exactly as it did. False is reserved for checks that estimate
    # rather than measure: an organisation opts into a heuristic knowingly.
    default_enabled: bool = True


REGISTRY: dict[str, QaCheck] = {
    c.name: c
    for c in (
        QaCheck(PLACEHOLDER_REMAINS, "A bracket placeholder survived the fill and is visible to the reader.", BLOCKING),
        QaCheck(UNRESOLVED_MERGEFIELD, "A MERGEFIELD instruction was never resolved to a value.", BLOCKING),
        QaCheck(CONTROL_TOKEN_REMAINS, "Template control flow ([[IF ...]]) is printed in the output.", BLOCKING),
        QaCheck(INSTRUCTION_TEXT_REMAINS, "Prose addressed to whoever assembles the letter survived into it.", BLOCKING),
        QaCheck(REQUIRED_VALUE_MISSING, "A field whose on_missing policy is BLOCK resolved to nothing.", BLOCKING),
        QaCheck(BRANCH_SELECTION, "A mutually exclusive branch set did not resolve to exactly one branch.", BLOCKING),
        QaCheck(STATIC_REGION_CHANGED, "A package part the engine must not touch differs from the template.", BLOCKING),
        # The two gates added after a real offer letter shipped with its offer
        # sentence, its date and its hours of work missing, reporting qa_passed.
        # Every existing check looks for what is LEFT OVER -- a placeholder, a
        # mergefield, an instruction. Nothing looked for what was ABSENT, and
        # that asymmetry is the whole reason the letter passed.
        QaCheck(ORPHANED_FIELD, "A declared field sits only in paragraphs the compile deletes, so no document can ever contain it.", BLOCKING),
        QaCheck(RESOLVED_VALUE_ABSENT, "A field resolved to a value that is nowhere in the finished document.", BLOCKING),
        QaCheck(VALUE_FORMAT_DOUBLED, "A unit or currency marker is printed twice because the template and the value both supplied it.", BLOCKING),
        # A warning, not a block. The doubling may be the template author's own
        # typo, and refusing to render somebody's letter over their punctuation
        # is how a gate gets switched off. It is reported so a person decides.
        QaCheck(DOUBLED_WORD, "A word is repeated adjacently in the output.", WARNING),
        # Found by auditing a real run: every gate above looks for a bracket, a
        # mergefield or an instruction, and these templates mark a slot with
        # neither -- `本合同生效日期为xxxx年xx月xx日` is a date placeholder written
        # as a mask. The engine had resolved the date and had nowhere to put it,
        # so the mask survived and the document passed. 395 of them across one
        # eleven-template set.
        QaCheck(FILL_MASK_REMAINS, "A masked slot (xxxx年xx月xx日, xx个月) was never filled and is visible to the reader.", BLOCKING),
        # The same audit, and the more expensive half. A mask is visibly unfilled
        # and a reader catches it; this is filled, wrong, and reads as data:
        # `2026-09-01年2026-09-01月2026-09-01`, produced when the year, month and
        # day slots of one date mask were all bound to the same field.
        QaCheck(DATE_PART_MALFORMED, "A date part slot (year/month/day) holds a whole date rather than its part.", BLOCKING),
        QaCheck(ROW_REPEAT, "A repeating table row could not be rendered once per record of its collection.", BLOCKING),
        QaCheck(VALUE_EXCEEDS_MAX_LEN, "A value is longer than the manifest's declared max_len for that field.", BLOCKING),
        QaCheck(
            VALUE_OVERFLOWS_CELL,
            "A value is estimated not to fit the declared width of the table cell it was written into.",
            BLOCKING,
            default_enabled=False,
        ),
        # §17 lists "Value overflows approved PDF region" as its own row, and it
        # is a *measurement* rather than the estimate above: the region is a
        # stored bounding box and the width comes from the font's own metrics.
        # So it is on by default where the cell heuristic is not.
        QaCheck(
            VALUE_OVERFLOWS_REGION,
            "A value is wider than the approved PDF region the renderer overlaid it into.",
            BLOCKING,
        ),
    )
}


class UnknownCheck(ValueError):
    """A `qa_policy` named a check this build does not implement."""


@dataclass(frozen=True)
class QaFinding:
    """One failure, before the policy has decided what it costs."""

    check: str
    detail: str


@dataclass(frozen=True)
class RoutedFinding:
    check: str
    detail: str
    severity: str

    @property
    def note(self) -> str:
        """The line a reviewer reads in `qa_notes`.

        A blocking finding is its own detail and nothing else -- these strings
        are matched by name in operational tooling and in the golden corpus. A
        warning is prefixed, because a note that stopped the document and a note
        that did not must not look identical in a list of notes.
        """
        return self.detail if self.severity == BLOCKING else f"Warning (non-blocking): {self.detail}"


@dataclass(frozen=True)
class QaOutcome:
    findings: tuple[RoutedFinding, ...] = ()

    @property
    def blocking(self) -> tuple[RoutedFinding, ...]:
        return tuple(f for f in self.findings if f.severity == BLOCKING)

    @property
    def warnings(self) -> tuple[RoutedFinding, ...]:
        return tuple(f for f in self.findings if f.severity == WARNING)

    @property
    def passed(self) -> bool:
        return not self.blocking

    @property
    def notes(self) -> list[str]:
        """Every note in the order the checks produced it, blocking or not."""
        return [f.note for f in self.findings]


@dataclass(frozen=True)
class QaPolicy:
    severities: Mapping[str, str]
    enabled: frozenset

    def runs(self, check: str) -> bool:
        """Whether this check should be executed at all.

        Asked *before* the work, so an opt-in heuristic costs nothing on the
        templates that did not ask for it.
        """
        self._require_known(check)
        return check in self.enabled

    def severity_of(self, check: str) -> str:
        self._require_known(check)
        return self.severities[check]

    def as_dict(self) -> dict:
        """The resolved policy, for the audit record.

        §17 requires QA results in every document's lineage. "Which checks were
        blocking when this document was generated" is part of that result: the
        same notes mean different things under different policies.
        """
        return {
            BLOCKING: sorted(n for n in self.enabled if self.severities[n] == BLOCKING),
            WARNING: sorted(n for n in self.enabled if self.severities[n] == WARNING),
        }

    @staticmethod
    def _require_known(check: str) -> None:
        if check not in REGISTRY:
            raise UnknownCheck(f"No QA check named {check!r}. Known checks: {', '.join(sorted(REGISTRY))}.")


def default_policy() -> QaPolicy:
    """What runs when a manifest declares no `qa_policy`.

    Identical to the behaviour that was hard-coded in the renderer before this
    module: every gate that was enforced is enforced, and blocking.
    """
    return QaPolicy(
        severities={name: c.default_severity for name, c in REGISTRY.items()},
        enabled=frozenset(name for name, c in REGISTRY.items() if c.default_enabled),
    )


def resolve_policy(declared: Mapping | None) -> QaPolicy:
    """Turn a manifest's `qa_policy` into the resolved severity of every check.

    `declared` is the §6 envelope shape, `{"blocking": [...], "warning": [...]}`.
    Anything else raises: a policy that cannot be read must not be treated as a
    policy that permits everything.
    """
    if not declared:
        return default_policy()
    if not isinstance(declared, Mapping):
        raise UnknownCheck(f"qa_policy must be a mapping of {SEVERITIES}, got {type(declared).__name__}.")

    unknown_keys = sorted(set(declared) - set(SEVERITIES))
    if unknown_keys:
        raise UnknownCheck(f"qa_policy has no severity {unknown_keys}; expected {list(SEVERITIES)}.")

    severities = {name: c.default_severity for name, c in REGISTRY.items()}
    enabled = set(name for name, c in REGISTRY.items() if c.default_enabled)
    claimed: dict[str, str] = {}

    for severity in SEVERITIES:
        for name in declared.get(severity) or ():
            QaPolicy._require_known(name)
            if name in claimed and claimed[name] != severity:
                raise UnknownCheck(f"qa_policy lists check {name!r} as both {claimed[name]} and {severity}.")
            claimed[name] = severity
            severities[name] = severity
            # Naming a check is how an opt-in one is turned on. There is no
            # separate "enabled" list to keep in step with this one.
            enabled.add(name)

    return QaPolicy(severities=severities, enabled=frozenset(enabled))


def route(findings: Iterable[QaFinding], policy: QaPolicy) -> QaOutcome:
    """Attach the policy's severity to each finding, keeping check order.

    Order is preserved rather than grouped by severity because `qa_notes` reads
    as the story of one generation: placeholders, then values, then structure.
    Sorting by severity would scatter that.
    """
    routed = []
    for finding in findings:
        if not policy.runs(finding.check):
            # A check that produced a finding while disabled is a wiring bug in
            # the caller, not a document defect, and swallowing it would hide
            # the fact that the work was done for nothing.
            raise UnknownCheck(f"Check {finding.check!r} produced a finding but is not enabled by this policy.")
        routed.append(RoutedFinding(finding.check, finding.detail, policy.severity_of(finding.check)))
    return QaOutcome(findings=tuple(routed))


def findings_for(check: str, details: Sequence[str], policy: QaPolicy) -> list[QaFinding]:
    """`details` as findings for `check`, or nothing when the check is off.

    The gate every check module funnels through, so "is this check enabled" is
    answered in exactly one place.
    """
    if not policy.runs(check):
        return []
    return [QaFinding(check=check, detail=detail) for detail in details]

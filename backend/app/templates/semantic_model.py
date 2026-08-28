"""The nine object types a manifest may contain, and the anchors that pin them
to the template.

§6 of the architecture record specifies the manifest as "the production
contract" and then admits that, until the schema is pinned, that phrase is a
slogan rather than an engineering artefact. Two things were missing from this
build. Manifest objects were loose dicts, so a field could reach approval
carrying no `on_missing`, no status and no declared format, and nothing noticed
until a batch ran. And a mapping's address into the template was a
`(paragraph_index, span_index)` pair, which is a position rather than a
reference: insert one paragraph in Word and every index below it points one
paragraph too high, silently, with the fill still reporting a clean document.

The doc's answer to the second problem is the composite anchor. A placeholder
such as `<New Reporting To>` is a poor anchor on its own -- "Word fragments text
across runs, re-saving normalises whitespace, and the same token can appear more
than once" -- so an anchor carries a path, an ordinal and a hash of the +/-60
characters around the occurrence, and is required to resolve "exactly once
against the pinned template version and fail loudly when it no longer resolves
exactly once". Zero matches and two matches are both errors here, and they carry
different messages because they need different repairs: zero means the template
moved on without the manifest, two means the anchor was never specific enough to
address one place.

The stability ordering of the anchor kinds is the doc's own, and the last row of
its table is the one worth reading twice. A style id or a run colour is
onboarding *evidence* -- it tells the compiler which runs are placeholders and
which are instructions -- and it must never survive into the manifest as the
addressing mechanism, because "a single reformat in Word would silently repoint
every mapping". So `resolve` refuses a style_id anchor outright instead of
resolving it, and every object type whose anchor drives production rejects one
at construction.

The manifests this build already compiles address slots positionally, so
`lift_slot_to_anchor` turns one of those pairs into a run_path anchor with a real
context hash. This module is meant to be adopted by the compiler, the fill engine
and the validator -- not to run beside them as a second, parallel object model.
"""

import dataclasses
import hashlib
import re
from dataclasses import dataclass, field

from app.expressions.token_parser import condition_inputs, evaluate_condition
from app.generation.missing_policy import DEFAULT as ON_MISSING_DEFAULT
from app.generation.missing_policy import ON_MISSING_VALUES

# ---- object types ----

FIELD = "FIELD"
CONDITION = "CONDITION"
SECTION = "SECTION"
TABLE_ROW = "TABLE_ROW"
CALCULATION = "CALCULATION"
NARRATIVE = "NARRATIVE"
STATIC = "STATIC"
HEADER = "HEADER"
FOOTER = "FOOTER"
SIGNATURE = "SIGNATURE"

#: The doc's nine object types. HEADER and FOOTER are one row of its table and
#: one class here; they differ only in which running region they address.
OBJECT_TYPES = (FIELD, CONDITION, SECTION, TABLE_ROW, CALCULATION, NARRATIVE,
                STATIC, HEADER, FOOTER, SIGNATURE)

# ---- object status ----

PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
NOT_APPLICABLE = "NOT_APPLICABLE"

OBJECT_STATUSES = (PROPOSED, APPROVED, NOT_APPLICABLE)

#: "Every non-STATIC object carries status APPROVED or an explicit
#: NOT_APPLICABLE rule" -- the first of the doc's lock-time validation rules.
#: PROPOSED is what the compiler writes and what review has to clear; it is not
#: a state a locked manifest may contain.
LOCKABLE_STATUSES = (APPROVED, NOT_APPLICABLE)

# ---- expression dialects ----

#: What this build actually executes: the subset in `app.expressions.token_parser`.
#: Matches the default already stored on `TemplateManifest.expression_lang`.
DOCUMIND_EXPR_LANG = "documind-expr/1.0"

#: §7's recommendation. Recognised so a manifest can declare it, but nothing
#: here evaluates it -- an object declaring CEL must supply its own input_fields
#: and cannot have its test cases run until a CEL evaluator exists.
CEL_LANG = "cel/1.0"

EXPRESSION_LANGS = (DOCUMIND_EXPR_LANG, CEL_LANG)

# ---- small vocabularies ----

#: The types `app.generation.value_format` can actually render. Declaring a type
#: outside this set means the renderer falls back to `str(value)`, which is how a
#: salary reaches a letter as `55000.0`.
VALUE_TYPES = ("string", "number", "quantity", "currency", "percent", "date")

KEEP = "KEEP"
REMOVE_BLOCK = "REMOVE_BLOCK"
BLOCK_MISSING_INPUT = "BLOCK_MISSING_INPUT"

#: What a CONDITION may do with its region on each branch.
BRANCH_ACTIONS = (KEEP, REMOVE_BLOCK)

#: The three outcomes of evaluating a condition against one record. The third is
#: the one §7 insists on: "a temporary assignment with no end date is not a false
#: condition -- it is incomplete source data, and the correct behaviour is to
#: block the document rather than quietly drop a paragraph the employee was
#: entitled to see."
CONDITION_OUTCOMES = (KEEP, REMOVE_BLOCK, BLOCK_MISSING_INPUT)

#: What a SECTION does when its region has nothing to say. REMOVE deletes the
#: region, KEEP leaves it standing and empty, BLOCK refuses to issue the letter.
#: The doc names the attribute without enumerating it; these are the three the
#: renderer can carry out, and an unrecognised fourth is refused rather than
#: silently treated as REMOVE.
SECTION_EMPTY_BEHAVIOURS = ("REMOVE", "KEEP", "BLOCK")

#: "allowed_mutations (usually none beyond date/ref)". Anything else in a running
#: region is a layout change, and a layout change is a new template version.
ALLOWED_RUNNING_MUTATIONS = ("date", "ref")

IMAGE_POLICIES = ("NONE", "EMBED_APPROVED_IMAGE", "ESIGN")

CITATION_POLICIES = ("REQUIRED", "NOT_REQUIRED")

# ---- anchor kinds ----

ANCHOR_CONTENT_CONTROL = "content_control"
ANCHOR_MERGEFIELD = "mergefield"
ANCHOR_RUN_PATH = "run_path"
ANCHOR_BBOX = "bbox"
ANCHOR_STYLE_ID = "style_id"

ANCHOR_KINDS = (ANCHOR_CONTENT_CONTROL, ANCHOR_MERGEFIELD, ANCHOR_RUN_PATH,
                ANCHOR_BBOX, ANCHOR_STYLE_ID)


@dataclass(frozen=True)
class AnchorKindProfile:
    """One row of the doc's anchor stability table, kept as data.

    `rank` orders the kinds by stability, highest first, so an onboarding step
    that has a choice of anchors can take the best one available rather than the
    first one it happened to find.
    """

    kind: str
    rank: int
    stability: str
    use_when: str
    production_safe: bool


#: The doc's table, verbatim in substance and ordered by stability.
ANCHOR_PROFILES = {
    ANCHOR_CONTENT_CONTROL: AnchorKindProfile(
        ANCHOR_CONTENT_CONTROL, 1, "Highest",
        "You are permitted to modify the template before onboarding", True),
    ANCHOR_MERGEFIELD: AnchorKindProfile(
        ANCHOR_MERGEFIELD, 2, "High",
        "The template already uses Word fields", True),
    ANCHOR_RUN_PATH: AnchorKindProfile(
        ANCHOR_RUN_PATH, 3, "Medium",
        "Legacy templates that must not be altered", True),
    ANCHOR_BBOX: AnchorKindProfile(
        ANCHOR_BBOX, 4, "Fixed to the page",
        "Immutable PDF templates only", True),
    ANCHOR_STYLE_ID: AnchorKindProfile(
        ANCHOR_STYLE_ID, 5, "Low -- onboarding hint only",
        "Colour-coded legacy estates; never used as the production anchor", False),
}

PRODUCTION_ANCHOR_KINDS = frozenset(
    k for k, p in ANCHOR_PROFILES.items() if p.production_safe)

#: Kinds that are evidence about a template, never an address into it.
EVIDENCE_ONLY_ANCHOR_KINDS = frozenset(
    k for k, p in ANCHOR_PROFILES.items() if not p.production_safe)

#: "hash of +/-60 chars of surrounding text".
CONTEXT_WINDOW = 60

#: Paragraphs are joined by a newline to form the text a context hash is taken
#: over, so a token near the start or end of its paragraph hashes real
#: neighbouring prose instead of padding the window with nothing.
PARAGRAPH_SEPARATOR = "\n"

_PARAGRAPH_IN_PATH_RE = re.compile(r"\bp\[(\d+)\]")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# ---- errors ----

class AnchorError(Exception):
    """Base for every way an anchor can be wrong."""


class AnchorResolutionError(AnchorError):
    """The anchor could not be resolved against the pinned template version."""


class AnchorNotFoundError(AnchorResolutionError):
    """Zero matches. The template has moved on without the manifest."""


class AnchorAmbiguousError(AnchorResolutionError):
    """Two or more matches. The anchor never addressed one place."""


class EvidenceOnlyAnchorError(AnchorError):
    """A style id or colour class was used where a production anchor belongs.

    Raised rather than resolved. The doc is emphatic about this one: colour
    coding "must not survive into the manifest as the addressing mechanism -- a
    single reformat in Word would silently repoint every mapping". Silently is
    the operative word; every other failure in this module is loud, and this one
    would not be.
    """


# ---- hashing ----

def _collapse_whitespace(text: str) -> str:
    """Fold every whitespace run to one space and trim the ends.

    Word normalises whitespace when it re-saves a document. Hashing the raw
    window would make an innocuous round-trip through Word fail every anchor in
    the template, which trains people to override the check -- and an override
    that is used routinely stops being a check. Collapsing first means the hash
    still catches an edit to the surrounding words, which is the drift that
    actually repoints a mapping.
    """
    return " ".join(text.split())


def context_hash(text: str, at: int, window: int = CONTEXT_WINDOW) -> str:
    """The doc's `context_hash`: sha256 over +/-`window` chars around `at`.

    `at` is the offset of the occurrence the anchor addresses, so the window runs
    from `at - window` to `at + window` and therefore covers the token itself and
    the prose on both sides of it. Out-of-range offsets raise: an anchor built
    against the wrong text is a bug to surface at compile time, not a hash to
    compare later and find surprising.
    """
    if window < 0:
        raise ValueError(f"context window must not be negative, got {window}")
    if not 0 <= at <= len(text):
        raise ValueError(
            f"offset {at} is outside the {len(text)}-character text the context "
            "hash was asked to cover")
    window_text = text[max(0, at - window): at + window]
    digest = hashlib.sha256(_collapse_whitespace(window_text).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def static_text_hash(text: str) -> str:
    """The hash a STATIC object carries.

    Deliberately *not* whitespace-normalised, unlike `context_hash`. The doc's
    rule is that "every STATIC region hash matches the template version
    byte-for-byte": approved immutable text that gained a double space gained it
    from somebody editing the template, and that has to force a new template
    version rather than pass as the same words.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_text(paragraph_texts) -> str:
    """The single string a context hash is taken over."""
    return PARAGRAPH_SEPARATOR.join(paragraph_texts)


def paragraph_offsets(paragraph_texts) -> list[int]:
    """Where each paragraph starts inside `document_text(paragraph_texts)`."""
    offsets, running = [], 0
    for text in paragraph_texts:
        offsets.append(running)
        running += len(text) + len(PARAGRAPH_SEPARATOR)
    return offsets


def _occurrences(text: str, token: str) -> list[int]:
    """Non-overlapping offsets of `token` in `text`, in order.

    Non-overlapping because these model places a fill engine would write a value
    into, and two writes cannot share a character.
    """
    if not token:
        raise ValueError("an empty token has no occurrences to count")
    found, start = [], 0
    while True:
        at = text.find(token, start)
        if at < 0:
            return found
        found.append(at)
        start = at + len(token)


# ---- the anchor ----

def _require_text(value, label: str, owner: str = "") -> str:
    where = f" on {owner}" if owner else ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}{where} is mandatory and must be non-empty text, got {value!r}")
    return value


@dataclass(frozen=True)
class Anchor:
    """A composite reference into one pinned template version.

    Which attributes are mandatory depends on the kind, and the constructor
    enforces exactly that -- a run_path anchor with no context hash is not a
    weaker anchor, it is an anchor that cannot detect the drift it exists to
    detect, so it is refused rather than accepted and half-trusted.
    """

    kind: str
    # run_path
    path: str | None = None
    ordinal: int | None = None
    token: str | None = None
    context_hash: str | None = None
    # The window the context hash was taken over. Stored because two hashes are
    # only comparable when they cover the same span, and a future change to the
    # default must not silently invalidate every anchor compiled before it.
    context_window: int = CONTEXT_WINDOW
    # content_control
    control_id: str | None = None
    # mergefield
    field_code: str | None = None
    # bbox (immutable PDF templates)
    page: int | None = None
    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None
    # style_id -- evidence only, see EvidenceOnlyAnchorError
    style_id: str | None = None

    def __post_init__(self):
        if self.kind not in ANCHOR_KINDS:
            raise ValueError(
                f"unknown anchor kind {self.kind!r}; the doc's table names {list(ANCHOR_KINDS)}")

        if self.kind == ANCHOR_RUN_PATH:
            _require_text(self.path, "path", "a run_path anchor")
            _require_text(self.token, "token", "a run_path anchor")
            if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 1:
                raise ValueError(
                    f"a run_path anchor needs a 1-based ordinal saying which occurrence of "
                    f"{self.token!r} it addresses, got {self.ordinal!r}")
            _require_text(self.context_hash, "context_hash", "a run_path anchor")
            if not _SHA256_RE.match(self.context_hash):
                raise ValueError(
                    f"context_hash must be sha256:<64 hex chars> as produced by context_hash(), "
                    f"got {self.context_hash!r}")
            if self.context_window < 0:
                raise ValueError(f"context_window must not be negative, got {self.context_window}")
        elif self.kind == ANCHOR_CONTENT_CONTROL:
            _require_text(self.control_id, "control_id", "a content_control anchor")
        elif self.kind == ANCHOR_MERGEFIELD:
            _require_text(self.field_code, "field_code", "a mergefield anchor")
        elif self.kind == ANCHOR_BBOX:
            if not isinstance(self.page, int) or isinstance(self.page, bool) or self.page < 1:
                raise ValueError(f"a bbox anchor needs a 1-based page number, got {self.page!r}")
            for name in ("x0", "y0", "x1", "y1"):
                value = getattr(self, name)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"a bbox anchor needs a numeric {name}, got {value!r}")
            if self.x1 <= self.x0 or self.y1 <= self.y0:
                raise ValueError(
                    f"a bbox anchor needs x0 < x1 and y0 < y1, got "
                    f"({self.x0}, {self.y0}, {self.x1}, {self.y1})")
        elif self.kind == ANCHOR_STYLE_ID:
            _require_text(self.style_id, "style_id", "a style_id anchor")

    # -- stability --

    @property
    def profile(self) -> AnchorKindProfile:
        return ANCHOR_PROFILES[self.kind]

    @property
    def is_production_safe(self) -> bool:
        """Whether this kind may address a mapping in a locked manifest."""
        return self.kind in PRODUCTION_ANCHOR_KINDS

    def assert_production_safe(self, owner: str = "") -> "Anchor":
        """Raise unless this anchor may be the production addressing mechanism."""
        if not self.is_production_safe:
            where = f" on {owner}" if owner else ""
            raise EvidenceOnlyAnchorError(
                f"a {self.kind} anchor{where} is onboarding evidence, not an address: it tells the "
                "compiler which runs are placeholders and which are instructions, and it must not "
                "survive into the manifest, because one reformat in Word would silently repoint "
                "every mapping that used it")
        return self

    @property
    def paragraph_index_hint(self) -> int | None:
        """The paragraph the path claims, or None when the path does not say.

        A hint and not a filter. Paragraph indices shift the moment anybody
        inserts a line above them, so resolution goes by token, ordinal and
        context hash; the hint only lets a caller report that the object moved.
        """
        if not self.path:
            return None
        found = _PARAGRAPH_IN_PATH_RE.search(self.path)
        return int(found.group(1)) if found else None

    def as_dict(self) -> dict:
        """The doc's on-the-wire anchor shape, without the attributes this kind
        does not use."""
        out = {"kind": self.kind}
        for name in ("path", "ordinal", "token", "context_hash", "control_id",
                     "field_code", "page", "x0", "y0", "x1", "y1", "style_id"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.context_hash is not None:
            out["context_window"] = self.context_window
        return out


@dataclass(frozen=True)
class AnchorRange:
    """The region a CONDITION or SECTION governs: `{"from": ..., "to": ...}`.

    Both endpoints must expose a paragraph index, because the rule this type
    exists to support -- "no two objects claim overlapping anchor ranges" -- is
    only computable over comparable positions. A PDF region is one bbox anchor
    and not a range of two, and saying so here is cheaper than discovering it
    when an overlap check quietly compares nothing.
    """

    start: Anchor
    end: Anchor

    def __post_init__(self):
        for label, anchor in (("start", self.start), ("end", self.end)):
            if not isinstance(anchor, Anchor):
                raise TypeError(f"anchor range {label} must be an Anchor, got {type(anchor).__name__}")
            anchor.assert_production_safe(f"an anchor range {label}")
            if anchor.paragraph_index_hint is None:
                raise ValueError(
                    f"anchor range {label} carries no paragraph index in its path ({anchor.path!r}); "
                    "a range needs comparable endpoints, and an immutable-PDF region is addressed by "
                    "a single bbox anchor rather than a range")
        if self.start.paragraph_index_hint > self.end.paragraph_index_hint:
            raise ValueError(
                f"anchor range runs backwards: paragraph {self.start.paragraph_index_hint} "
                f"to {self.end.paragraph_index_hint}")

    @property
    def span(self) -> tuple[int, int]:
        return (self.start.paragraph_index_hint, self.end.paragraph_index_hint)

    def overlaps(self, other: "AnchorRange") -> bool:
        """Whether two ranges claim any paragraph in common.

        Inclusive at both ends: two objects that both claim paragraph 57 are in
        conflict even if one of them claims nothing else.
        """
        mine, theirs = self.span, other.span
        return mine[0] <= theirs[1] and theirs[0] <= mine[1]

    def as_dict(self) -> dict:
        return {"from": self.start.path, "to": self.end.path}


# ---- what an anchor is resolved against ----

@dataclass(frozen=True)
class PageRegion:
    """One addressable region of a pinned PDF page."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str = ""

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)


@dataclass(frozen=True)
class TemplateInventory:
    """Everything a pinned template version offers an anchor to resolve against.

    `None` and `()` mean different things on every optional list, and the
    difference is the whole point: `None` is "you did not give me a mergefield
    inventory", which is a caller mistake and raises; `()` is "this template has
    no mergefields", which is a real answer and produces a not-found error naming
    the anchor. Collapsing the two would turn a wiring bug into a template bug.
    """

    paragraph_texts: tuple
    mergefield_codes: tuple | None = None   # (paragraph_index, code)
    content_controls: tuple | None = None   # (paragraph_index, control_id)
    page_regions: tuple | None = None       # PageRegion

    def __post_init__(self):
        for text in self.paragraph_texts:
            if not isinstance(text, str):
                raise TypeError(f"paragraph texts must be strings, got {type(text).__name__}")

    @classmethod
    def of(cls, template) -> "TemplateInventory":
        """Accept either an inventory or a bare sequence of paragraph texts."""
        if isinstance(template, TemplateInventory):
            return template
        return cls(paragraph_texts=tuple(template))


@dataclass(frozen=True)
class AnchorMatch:
    """Where an anchor resolved, and whether it had moved since it was compiled."""

    anchor: Anchor
    paragraph_index: int | None = None
    offset: int | None = None
    page: int | None = None
    matched_text: str = ""
    # True when the anchor resolved somewhere other than the paragraph its path
    # names -- the template gained or lost paragraphs above this one. Not an
    # error: the anchor did its job. Worth surfacing, because a re-compile should
    # rewrite the path before the drift accumulates.
    drifted_from_path: bool = False


def _drifted(anchor: Anchor, paragraph_index: int) -> bool:
    hint = anchor.paragraph_index_hint
    return hint is not None and hint != paragraph_index


def resolve(anchor: Anchor, paragraph_texts) -> AnchorMatch:
    """Resolve one anchor against a pinned template version, or raise.

    The doc's rule, in one function: "every anchor resolves exactly once against
    the pinned template version". Zero raises `AnchorNotFoundError`, two or more
    raise `AnchorAmbiguousError`, and the messages differ because the repairs do
    -- a not-found anchor needs the manifest re-pointed at a template that
    changed, an ambiguous one needs a more specific anchor before it is ever
    filled. Returning a "best" match instead would put the wrong value in a
    legally binding letter and report the document clean.

    `paragraph_texts` is a sequence of the template's paragraph texts, or a
    `TemplateInventory` when the anchor kind needs more than text.
    """
    if anchor.kind == ANCHOR_STYLE_ID:
        raise EvidenceOnlyAnchorError(
            f"style_id anchor {anchor.style_id!r} cannot be resolved: a style id is an onboarding "
            "hint about which runs mean what, never the production address. Resolving it would "
            "make a reformat in Word repoint the mapping without anybody being told.")

    inventory = TemplateInventory.of(paragraph_texts)
    if anchor.kind == ANCHOR_RUN_PATH:
        return _resolve_run_path(anchor, inventory)
    if anchor.kind == ANCHOR_MERGEFIELD:
        return _resolve_mergefield(anchor, inventory)
    if anchor.kind == ANCHOR_CONTENT_CONTROL:
        return _resolve_content_control(anchor, inventory)
    if anchor.kind == ANCHOR_BBOX:
        return _resolve_bbox(anchor, inventory)
    raise AnchorResolutionError(f"no resolver for anchor kind {anchor.kind!r}")


def _resolve_run_path(anchor: Anchor, inventory: TemplateInventory) -> AnchorMatch:
    texts = inventory.paragraph_texts
    whole = document_text(texts)
    starts = paragraph_offsets(texts)

    occurrences = []  # (paragraph_index, offset_in_paragraph, ordinal_in_paragraph)
    for p_idx, text in enumerate(texts):
        for nth, at in enumerate(_occurrences(text, anchor.token), start=1):
            occurrences.append((p_idx, at, nth))

    if not occurrences:
        raise AnchorNotFoundError(
            f"anchor {anchor.path} looks for {anchor.token!r}, which does not appear anywhere in "
            "the pinned template version; the template has changed since the manifest was compiled")

    at_ordinal = [o for o in occurrences if o[2] == anchor.ordinal]
    if not at_ordinal:
        raise AnchorNotFoundError(
            f"anchor {anchor.path} addresses occurrence {anchor.ordinal} of {anchor.token!r} within "
            f"a paragraph, but no paragraph carries that many: {len(occurrences)} occurrence(s) "
            f"found, in paragraphs {sorted({o[0] for o in occurrences})}")

    matches = [o for o in at_ordinal
               if context_hash(whole, starts[o[0]] + o[1], anchor.context_window) == anchor.context_hash]

    if not matches:
        raise AnchorNotFoundError(
            f"anchor {anchor.path} found {anchor.token!r} in paragraphs "
            f"{sorted({o[0] for o in at_ordinal})}, but the text around every candidate has changed "
            "since the manifest was compiled, so none matches the recorded context hash; re-compile "
            "against this template version rather than filling a place that may no longer mean the "
            "same thing")

    if len(matches) > 1:
        raise AnchorAmbiguousError(
            f"anchor {anchor.path} resolves {len(matches)} times, in paragraphs "
            f"{sorted({o[0] for o in matches})}; an anchor that addresses more than one place cannot "
            "be filled, because nothing here can know which of them the mapping meant")

    p_idx, at, _nth = matches[0]
    return AnchorMatch(anchor=anchor, paragraph_index=p_idx, offset=at,
                       matched_text=anchor.token, drifted_from_path=_drifted(anchor, p_idx))


def _resolve_mergefield(anchor: Anchor, inventory: TemplateInventory) -> AnchorMatch:
    if inventory.mergefield_codes is None:
        raise AnchorResolutionError(
            f"mergefield anchor {anchor.field_code!r} resolves against the template's MERGEFIELD "
            "inventory, which was not supplied; pass a TemplateInventory carrying mergefield_codes "
            "instead of a bare list of paragraph texts")

    matches = [(p_idx, code) for p_idx, code in inventory.mergefield_codes if code == anchor.field_code]
    if not matches:
        raise AnchorNotFoundError(
            f"mergefield anchor {anchor.field_code!r} matches no field in the pinned template "
            f"version, which carries {len(inventory.mergefield_codes)} MERGEFIELD(s)")

    if anchor.ordinal is not None:
        # A MERGEFIELD carries no surrounding text to hash, so a repeated code
        # can only be disambiguated by the author saying which one they meant.
        # Selecting the nth is therefore an explicit decision recorded on the
        # anchor, never an inference made here.
        if anchor.ordinal > len(matches):
            raise AnchorNotFoundError(
                f"mergefield anchor {anchor.field_code!r} addresses occurrence {anchor.ordinal}, "
                f"but the template carries only {len(matches)}")
        matches = [matches[anchor.ordinal - 1]]
    elif len(matches) > 1:
        raise AnchorAmbiguousError(
            f"mergefield anchor {anchor.field_code!r} resolves {len(matches)} times, in paragraphs "
            f"{[p for p, _ in matches]}; set the anchor's ordinal to say which occurrence it means")

    p_idx, code = matches[0]
    return AnchorMatch(anchor=anchor, paragraph_index=p_idx, matched_text=code,
                       drifted_from_path=_drifted(anchor, p_idx))


def _resolve_content_control(anchor: Anchor, inventory: TemplateInventory) -> AnchorMatch:
    if inventory.content_controls is None:
        raise AnchorResolutionError(
            f"content_control anchor {anchor.control_id!r} resolves against the template's SDT "
            "inventory, which was not supplied; pass a TemplateInventory carrying content_controls "
            "instead of a bare list of paragraph texts")

    matches = [(p_idx, cid) for p_idx, cid in inventory.content_controls if cid == anchor.control_id]
    if not matches:
        raise AnchorNotFoundError(
            f"content_control anchor {anchor.control_id!r} matches no content control in the pinned "
            "template version; the control was renamed or removed")
    if len(matches) > 1:
        # SDT ids are unique by construction, so two is not an addressing
        # question -- it is a corrupted or hand-merged template, and filling
        # either one would be a guess.
        raise AnchorAmbiguousError(
            f"content_control anchor {anchor.control_id!r} matches {len(matches)} controls, in "
            f"paragraphs {[p for p, _ in matches]}; content control ids are meant to be unique, so "
            "this template needs repairing before it can be onboarded")

    p_idx, cid = matches[0]
    return AnchorMatch(anchor=anchor, paragraph_index=p_idx, matched_text=cid,
                       drifted_from_path=_drifted(anchor, p_idx))


def _resolve_bbox(anchor: Anchor, inventory: TemplateInventory) -> AnchorMatch:
    if inventory.page_regions is None:
        raise AnchorResolutionError(
            f"bbox anchor on page {anchor.page} resolves against the pinned PDF's region inventory, "
            "which was not supplied; pass a TemplateInventory carrying page_regions instead of a "
            "bare list of paragraph texts")

    # Containment of the region's centre rather than equality of the four
    # coordinates: a PDF text extractor returns boxes that differ from the
    # approved ones by fractions of a point, and demanding equality would fail
    # every anchor for a reason that has nothing to do with the document.
    matches = [r for r in inventory.page_regions
               if r.page == anchor.page
               and anchor.x0 <= r.centre[0] <= anchor.x1
               and anchor.y0 <= r.centre[1] <= anchor.y1]

    if not matches:
        raise AnchorNotFoundError(
            f"bbox anchor ({anchor.x0}, {anchor.y0}, {anchor.x1}, {anchor.y1}) on page {anchor.page} "
            "contains no region of the pinned PDF; the page was re-laid out, so overlaying text at "
            "these coordinates would put it somewhere nobody approved")
    if len(matches) > 1:
        raise AnchorAmbiguousError(
            f"bbox anchor on page {anchor.page} contains {len(matches)} regions; narrow the box until "
            "it addresses one, because overlaying text over two regions destroys one of them")

    region = matches[0]
    return AnchorMatch(anchor=anchor, page=region.page, matched_text=region.text)


# ---- adopting the manifests that already exist ----

def lift_slot_to_anchor(slot: dict, paragraph_texts, *, occurrence: int | None = None) -> Anchor:
    """Turn a compiled manifest's `(paragraph_index, span_index)` slot into an anchor.

    The manifests this build already produces address slots positionally, which
    is what §6 replaces. Lifting rather than re-compiling means an estate that is
    already onboarded gains drift detection without every template going back
    through the compiler.

    A mergefield slot is lifted to a mergefield anchor, not a run_path one: the
    doc ranks MERGEFIELD above run paths for stability, and rewriting a stable
    anchor as a less stable one to keep the code uniform would be a downgrade
    dressed up as a refactor.

    When the slot's token appears more than once in its paragraph, the paragraph
    text alone cannot say which occurrence the span held, so this raises and asks
    the caller for `occurrence` rather than assuming the first -- assuming would
    put the value in the wrong half of "report to <Manager>, copying <Manager>".
    """
    if not isinstance(slot, dict):
        raise TypeError(f"a slot must be a dict, got {type(slot).__name__}")

    p_idx = slot.get("paragraph_index")
    if not isinstance(p_idx, int) or isinstance(p_idx, bool):
        raise ValueError(f"slot {slot!r} carries no integer paragraph_index, so it addresses nothing")

    inventory = TemplateInventory.of(paragraph_texts)
    texts = inventory.paragraph_texts
    if not 0 <= p_idx < len(texts):
        raise ValueError(
            f"slot addresses paragraph {p_idx}, but the template version has {len(texts)} "
            "paragraph(s); the manifest and the template have diverged")

    span_index = slot.get("span_index")
    path = f"body/p[{p_idx}]/r[{span_index}]" if isinstance(span_index, int) else f"body/p[{p_idx}]"

    if slot.get("kind") == "mergefield" or slot.get("code"):
        return Anchor(kind=ANCHOR_MERGEFIELD, path=path,
                      field_code=_require_text(slot.get("code"), "code", "a mergefield slot"))

    token = slot.get("text")
    _require_text(token, "text", f"slot {path}")

    found = _occurrences(texts[p_idx], token)
    if not found:
        raise ValueError(
            f"slot {path} claims the token {token!r}, which paragraph {p_idx} does not contain; "
            "the manifest was compiled against a different version of this template")

    if occurrence is None:
        if len(found) > 1:
            raise ValueError(
                f"token {token!r} appears {len(found)} times in paragraph {p_idx}, and a span index "
                "does not say which one this slot held; pass occurrence=1..{n} to lift it".format(
                    n=len(found)))
        occurrence = 1
    elif not 1 <= occurrence <= len(found):
        raise ValueError(
            f"occurrence {occurrence} was asked for, but {token!r} appears {len(found)} time(s) in "
            f"paragraph {p_idx}")

    absolute = paragraph_offsets(texts)[p_idx] + found[occurrence - 1]
    return Anchor(
        kind=ANCHOR_RUN_PATH,
        path=path,
        ordinal=occurrence,
        token=token,
        context_hash=context_hash(document_text(texts), absolute),
    )


def lift_block_to_anchor_range(block: dict, paragraph_texts) -> AnchorRange:
    """Turn a compiled block's `start_paragraph`/`end_paragraph` into an anchor range.

    The boundary anchors take the boundary paragraphs' own text as their token:
    a block starts at the paragraph that says this and ends at the paragraph that
    says that, which survives an insertion above it in a way that "paragraph 57"
    does not. An empty boundary paragraph cannot be anchored by content, so it
    raises instead of producing an anchor that would match every blank line in
    the document.
    """
    if not isinstance(block, dict):
        raise TypeError(f"a block must be a dict, got {type(block).__name__}")

    start = block.get("start_paragraph")
    end = block.get("end_paragraph")
    for label, value in (("start_paragraph", start), ("end_paragraph", end)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"block {block.get('id')!r} carries no integer {label}")
    if start > end:
        raise ValueError(
            f"block {block.get('id')!r} runs from paragraph {start} to {end}, which is backwards")

    return AnchorRange(start=_paragraph_anchor(start, paragraph_texts),
                       end=_paragraph_anchor(end, paragraph_texts))


def _paragraph_anchor(p_idx: int, paragraph_texts) -> Anchor:
    inventory = TemplateInventory.of(paragraph_texts)
    texts = inventory.paragraph_texts
    if not 0 <= p_idx < len(texts):
        raise ValueError(
            f"block boundary addresses paragraph {p_idx}, but the template version has "
            f"{len(texts)} paragraph(s)")

    token = texts[p_idx].strip()
    if not token:
        raise ValueError(
            f"paragraph {p_idx} is empty, so it cannot be a block boundary anchor: an empty token "
            "would match every blank paragraph in the template")

    at = texts[p_idx].index(token)
    ordinal = _occurrences(texts[p_idx], token).index(at) + 1
    absolute = paragraph_offsets(texts)[p_idx] + at
    return Anchor(kind=ANCHOR_RUN_PATH, path=f"body/p[{p_idx}]", ordinal=ordinal, token=token,
                  context_hash=context_hash(document_text(texts), absolute))


# ---- condition evaluation ----

def condition_outcome(expression: str, record: dict, *,
                      on_true: str = KEEP, on_false: str = REMOVE_BLOCK) -> str:
    """What a condition does to its region for one record: the doc's three outcomes.

    Undecidable is not false. `evaluate_condition` already keeps the two apart
    with a three-state verdict; this maps that verdict onto the manifest's
    branch actions and returns BLOCK_MISSING_INPUT for the third state, because
    "the correct behaviour is to block the document rather than quietly drop a
    paragraph the employee was entitled to see".

    An expression this build cannot evaluate raises rather than resolving to a
    branch. A condition that does not parse is caught at lock time; reaching
    generation with one is a bug, and answering it with REMOVE_BLOCK would delete
    a section of a legal letter on the strength of a syntax error.
    """
    if on_true not in BRANCH_ACTIONS or on_false not in BRANCH_ACTIONS:
        raise ValueError(f"branch actions must be one of {list(BRANCH_ACTIONS)}")

    verdict = evaluate_condition(expression, record)
    if verdict.value is True:
        return on_true
    if verdict.value is False:
        return on_false
    if verdict.reason == "missing_input":
        return BLOCK_MISSING_INPUT
    raise ValueError(
        f"condition {expression!r} could not be evaluated ({verdict.reason}); an expression that "
        "does not parse must never be answered with a branch")


@dataclass(frozen=True)
class TestCaseResult:
    """One declared test case, run."""

    index: int
    record: dict
    expected: str
    actual: str | None
    passed: bool
    error: str | None = None


def run_test_cases(condition: "ConditionObject") -> tuple:
    """Run a CONDITION's declared test cases. "At least one passing test case."

    A failing case is returned rather than raised, because the caller is a review
    screen or a lock check that wants all of them, not the first one.
    """
    if condition.expression_lang != DOCUMIND_EXPR_LANG:
        raise ValueError(
            f"test cases for {condition.object_id!r} are written against {condition.expression_lang}, "
            f"which this build cannot evaluate; only {DOCUMIND_EXPR_LANG} runs here")

    results = []
    for i, case in enumerate(condition.test_cases):
        expected = case["expect"]
        try:
            actual = condition_outcome(condition.expression, dict(case["in"]),
                                       on_true=condition.on_true, on_false=condition.on_false)
        except ValueError as exc:
            results.append(TestCaseResult(i, dict(case["in"]), expected, None, False, str(exc)))
            continue
        results.append(TestCaseResult(i, dict(case["in"]), expected, actual, actual == expected))
    return tuple(results)


# ---- the nine object types ----

def _jsonable(value):
    if isinstance(value, (Anchor, AnchorRange)):
        return value.as_dict()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return value


@dataclass(frozen=True, kw_only=True)
class SemanticObject:
    """What every manifest object carries whatever its type.

    Keyword-only and frozen. Keyword-only because nine object types with
    overlapping attribute names is exactly where a positional argument ends up in
    the wrong slot; frozen because §6's lock semantics say a locked manifest is
    immutable and an object that can be edited in place after approval makes the
    (template_hash, manifest_hash) pair a claim rather than a guarantee.
    """

    object_id: str
    object_type: str
    status: str

    approved_by: str | None = None
    approved_at: str | None = None
    confidence: float | None = None
    evidence: tuple = ()

    def __post_init__(self):
        _require_text(self.object_id, "object_id")
        if self.object_type not in OBJECT_TYPES:
            raise ValueError(
                f"unknown object_type {self.object_type!r}; the doc names {list(OBJECT_TYPES)}")
        if self.status not in OBJECT_STATUSES:
            raise ValueError(
                f"object {self.object_id!r} has status {self.status!r}; expected one of "
                f"{list(OBJECT_STATUSES)}")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def is_lockable(self) -> bool:
        """Whether this object satisfies the doc's first lock rule.

        STATIC is exempt by that rule's own wording -- "every *non-STATIC* object
        carries status APPROVED or an explicit NOT_APPLICABLE rule" -- because
        approved immutable text is guaranteed by the template hash, not by
        somebody signing off a mapping that does not exist.
        """
        return self.object_type == STATIC or self.status in LOCKABLE_STATUSES

    def anchors(self) -> tuple:
        """Every single-point anchor this object addresses."""
        return ()

    def anchor_ranges(self) -> tuple:
        """Every region this object claims."""
        return ()

    def source_refs(self) -> tuple:
        """Every source field this object reads."""
        return ()

    def as_dict(self) -> dict:
        return {f.name: _jsonable(getattr(self, f.name)) for f in dataclasses.fields(self)}


@dataclass(frozen=True, kw_only=True)
class FieldObject(SemanticObject):
    """FIELD -- "must carry: anchor, source_ref, format, on_missing, status".

    `format` has no default even though it may be None. Passing `format=None`
    says "this value is rendered as it arrives"; omitting it says nothing at all,
    and the difference between a declared decision and an absent one is what this
    whole section is about.
    """

    object_type: str = FIELD
    anchor: Anchor
    source_ref: str
    format: str | None
    on_missing: str

    context: str | None = None
    value_type: str = "string"
    transform: str | None = None
    validation: dict = field(default_factory=dict)
    default: object = None

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.anchor, Anchor):
            raise TypeError(f"field {self.object_id!r} needs an Anchor, got {type(self.anchor).__name__}")
        self.anchor.assert_production_safe(f"field {self.object_id!r}")
        _require_text(self.source_ref, "source_ref", f"field {self.object_id!r}")
        if self.on_missing not in ON_MISSING_VALUES:
            raise ValueError(
                f"field {self.object_id!r} declares on_missing={self.on_missing!r}; expected one of "
                f"{list(ON_MISSING_VALUES)}")
        # The same rule `manifests.validator` enforces on the stored shape: a
        # DEFAULT policy with nothing to fall back to is a BLANK policy that
        # nobody chose.
        if self.on_missing == ON_MISSING_DEFAULT and self.default is None:
            raise ValueError(
                f"field {self.object_id!r} declares on_missing=DEFAULT but carries no default value, "
                "so the policy would render nothing and call it deliberate")
        if self.value_type not in VALUE_TYPES:
            raise ValueError(
                f"field {self.object_id!r} declares value_type={self.value_type!r}; the renderer can "
                f"format {list(VALUE_TYPES)}")

    def anchors(self) -> tuple:
        return (self.anchor,)

    def source_refs(self) -> tuple:
        return (self.source_ref,)


@dataclass(frozen=True, kw_only=True)
class ConditionObject(SemanticObject):
    """CONDITION -- "anchor_range, expression, on_true, on_false, test_cases"."""

    object_type: str = CONDITION
    anchor_range: AnchorRange
    expression: str
    on_true: str
    on_false: str
    test_cases: tuple

    expression_lang: str = DOCUMIND_EXPR_LANG
    input_fields: tuple | None = None
    plain_english: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.anchor_range, AnchorRange):
            raise TypeError(
                f"condition {self.object_id!r} needs an AnchorRange, got "
                f"{type(self.anchor_range).__name__}")
        _require_text(self.expression, "expression", f"condition {self.object_id!r}")
        if self.expression_lang not in EXPRESSION_LANGS:
            raise ValueError(
                f"condition {self.object_id!r} declares expression_lang={self.expression_lang!r}; "
                f"expected one of {list(EXPRESSION_LANGS)}")
        for label, action in (("on_true", self.on_true), ("on_false", self.on_false)):
            if action not in BRANCH_ACTIONS:
                raise ValueError(
                    f"condition {self.object_id!r} declares {label}={action!r}; expected one of "
                    f"{list(BRANCH_ACTIONS)}")
        if self.on_true == self.on_false:
            raise ValueError(
                f"condition {self.object_id!r} does the same thing on both branches "
                f"({self.on_true}), so it governs nothing and the region is unconditional")

        object.__setattr__(self, "test_cases", _validated_test_cases(
            self.test_cases, self.object_id, expects=CONDITION_OUTCOMES))

        if self.input_fields is None:
            # "the computed closure of all referenced fields, not a hand-written
            # list" -- the doc says it of required_source_fields, and the reason
            # holds one level down: a hand-listed input that the expression does
            # not read, or reads and does not list, is a binding screen that
            # offers the wrong fields.
            if self.expression_lang != DOCUMIND_EXPR_LANG:
                raise ValueError(
                    f"condition {self.object_id!r} is written in {self.expression_lang}, which this "
                    "build cannot parse, so it must declare its input_fields explicitly")
            object.__setattr__(self, "input_fields", tuple(condition_inputs(self.expression)))
        else:
            object.__setattr__(self, "input_fields", tuple(self.input_fields))

    def anchor_ranges(self) -> tuple:
        return (self.anchor_range,)

    def source_refs(self) -> tuple:
        return tuple(self.input_fields)

    def outcome_for(self, record: dict) -> str:
        """What this condition does to its region for one source record."""
        return condition_outcome(self.expression, record,
                                 on_true=self.on_true, on_false=self.on_false)


@dataclass(frozen=True, kw_only=True)
class SectionObject(SemanticObject):
    """SECTION -- "anchor_range, repeat_over, ordering, empty_behaviour"."""

    object_type: str = SECTION
    anchor_range: AnchorRange
    repeat_over: str | None
    ordering: str | None
    empty_behaviour: str

    name: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.anchor_range, AnchorRange):
            raise TypeError(
                f"section {self.object_id!r} needs an AnchorRange, got "
                f"{type(self.anchor_range).__name__}")
        if self.empty_behaviour not in SECTION_EMPTY_BEHAVIOURS:
            raise ValueError(
                f"section {self.object_id!r} declares empty_behaviour={self.empty_behaviour!r}; "
                f"expected one of {list(SECTION_EMPTY_BEHAVIOURS)}")
        if self.ordering is not None and not self.repeat_over:
            # An ordering with nothing to order is a mapping mistake that reads
            # as harmless and hides a missing repeat_over -- the section renders
            # once instead of once per record and nobody sees the difference
            # until a record has two.
            raise ValueError(
                f"section {self.object_id!r} declares ordering={self.ordering!r} but nothing to "
                "repeat over; a non-repeating section has no ordering")

    def anchor_ranges(self) -> tuple:
        return (self.anchor_range,)

    def source_refs(self) -> tuple:
        return (self.repeat_over,) if self.repeat_over else ()


@dataclass(frozen=True, kw_only=True)
class TableRowObject(SemanticObject):
    """TABLE_ROW -- "anchor_row, iterate_over, per-column field refs"."""

    object_type: str = TABLE_ROW
    anchor_row: Anchor
    iterate_over: str
    column_fields: dict

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.anchor_row, Anchor):
            raise TypeError(
                f"table row {self.object_id!r} needs an Anchor, got {type(self.anchor_row).__name__}")
        self.anchor_row.assert_production_safe(f"table row {self.object_id!r}")
        _require_text(self.iterate_over, "iterate_over", f"table row {self.object_id!r}")
        if not self.column_fields:
            raise ValueError(
                f"table row {self.object_id!r} carries no column field refs, so repeating it would "
                "produce the template row over and over with nothing filled in")
        for column, ref in self.column_fields.items():
            _require_text(ref, f"the source ref for column {column!r}", f"table row {self.object_id!r}")

    def anchors(self) -> tuple:
        return (self.anchor_row,)

    def source_refs(self) -> tuple:
        return (self.iterate_over,) + tuple(self.column_fields.values())


@dataclass(frozen=True, kw_only=True)
class CalculationObject(SemanticObject):
    """CALCULATION -- "expression, input_fields, output_type, rounding, test_cases"."""

    object_type: str = CALCULATION
    expression: str
    input_fields: tuple
    output_type: str
    rounding: int | None
    test_cases: tuple

    expression_lang: str = DOCUMIND_EXPR_LANG
    plain_english: str | None = None

    def __post_init__(self):
        super().__post_init__()
        _require_text(self.expression, "expression", f"calculation {self.object_id!r}")
        if self.expression_lang not in EXPRESSION_LANGS:
            raise ValueError(
                f"calculation {self.object_id!r} declares expression_lang={self.expression_lang!r}; "
                f"expected one of {list(EXPRESSION_LANGS)}")
        if self.output_type not in VALUE_TYPES:
            raise ValueError(
                f"calculation {self.object_id!r} declares output_type={self.output_type!r}; the "
                f"renderer can format {list(VALUE_TYPES)}")
        if self.rounding is not None:
            if not isinstance(self.rounding, int) or isinstance(self.rounding, bool) or self.rounding < 0:
                raise ValueError(
                    f"calculation {self.object_id!r} declares rounding={self.rounding!r}; rounding is "
                    "a number of decimal places, or None for a value that is not rounded")
        elif self.output_type in ("number", "quantity", "currency", "percent"):
            # Unrounded money is the classic version of this: the same figure
            # reaches one letter as 55000.0 and another as 54999.999999999993,
            # and neither of them is what the approver signed.
            raise ValueError(
                f"calculation {self.object_id!r} produces {self.output_type} but declares no "
                "rounding; a numeric output must say how it is rounded rather than inheriting "
                "whatever float arithmetic produced")

        object.__setattr__(self, "input_fields", tuple(self.input_fields))
        if not self.input_fields:
            raise ValueError(
                f"calculation {self.object_id!r} declares no input_fields, so nothing can check that "
                "the source record carries what it needs before a batch runs")

        if self.expression_lang == DOCUMIND_EXPR_LANG:
            undeclared = sorted(set(condition_inputs(self.expression)) - set(self.input_fields))
            if undeclared:
                raise ValueError(
                    f"calculation {self.object_id!r} reads {undeclared} but does not declare them as "
                    "input_fields; required_source_fields is the computed closure of what is "
                    "referenced, and an undeclared input is a column nothing validates before a batch")

        object.__setattr__(self, "test_cases", _validated_test_cases(
            self.test_cases, self.object_id, expects=None))

    def source_refs(self) -> tuple:
        return tuple(self.input_fields)


@dataclass(frozen=True, kw_only=True)
class NarrativeObject(SemanticObject):
    """NARRATIVE -- "prompt_ref, grounding_sources, citation_policy, review_required".

    The doc calls this the rare exception path, and §17's QA table blocks a
    narrative section that "lacks grounding/citations". So an ungrounded
    NARRATIVE cannot be constructed: there is no state of this object that means
    "generate prose about this employee from nothing and put it in the letter".
    """

    object_type: str = NARRATIVE
    prompt_ref: str
    grounding_sources: tuple
    citation_policy: str
    review_required: bool

    anchor: Anchor | None = None

    def __post_init__(self):
        super().__post_init__()
        _require_text(self.prompt_ref, "prompt_ref", f"narrative {self.object_id!r}")
        object.__setattr__(self, "grounding_sources", tuple(self.grounding_sources))
        if not self.grounding_sources:
            raise ValueError(
                f"narrative {self.object_id!r} names no grounding sources; per-generation QA blocks a "
                "narrative section without grounding, so this object could never produce a document")
        if self.citation_policy not in CITATION_POLICIES:
            raise ValueError(
                f"narrative {self.object_id!r} declares citation_policy={self.citation_policy!r}; "
                f"expected one of {list(CITATION_POLICIES)}")
        # `isinstance` and not truthiness: `review_required="no"` is truthy, and
        # reading it as "yes, review this" would be the safe direction while
        # reading it as declared would not. Refusing the string is safer than
        # either.
        if not isinstance(self.review_required, bool):
            raise ValueError(
                f"narrative {self.object_id!r} declares review_required="
                f"{self.review_required!r}; it must be a bool")

    def anchors(self) -> tuple:
        return (self.anchor,) if self.anchor is not None else ()


@dataclass(frozen=True, kw_only=True)
class StaticObject(SemanticObject):
    """STATIC -- "text_hash; any change forces a new template version"."""

    object_type: str = STATIC
    text_hash: str

    anchor: Anchor | None = None
    # Approved immutable text is not a mapping anybody signs off. Its
    # correctness is the template hash's job, which is why the doc exempts STATIC
    # from the status rule.
    status: str = NOT_APPLICABLE

    def __post_init__(self):
        super().__post_init__()
        _require_text(self.text_hash, "text_hash", f"static region {self.object_id!r}")
        if not _SHA256_RE.match(self.text_hash):
            raise ValueError(
                f"static region {self.object_id!r} carries text_hash={self.text_hash!r}; expected "
                "sha256:<64 hex chars> as produced by static_text_hash()")

    def matches(self, text: str) -> bool:
        """Whether this region's approved text is still byte-for-byte what it was."""
        return static_text_hash(text) == self.text_hash

    def anchors(self) -> tuple:
        return (self.anchor,) if self.anchor is not None else ()


@dataclass(frozen=True, kw_only=True)
class RunningRegionObject(SemanticObject):
    """HEADER / FOOTER -- "anchor, allowed_mutations (usually none beyond date/ref)".

    One class for both rows of the doc's table; `object_type` says which running
    region it is. `allowed_mutations` may be empty -- that is the usual case and
    the doc says so -- but it must be passed, because "this footer changes
    nothing" is a decision and an absent attribute is not.
    """

    anchor: Anchor
    allowed_mutations: tuple

    def __post_init__(self):
        super().__post_init__()
        if self.object_type not in (HEADER, FOOTER):
            raise ValueError(
                f"running region {self.object_id!r} must be {HEADER} or {FOOTER}, got "
                f"{self.object_type!r}")
        if not isinstance(self.anchor, Anchor):
            raise TypeError(
                f"running region {self.object_id!r} needs an Anchor, got {type(self.anchor).__name__}")
        self.anchor.assert_production_safe(f"running region {self.object_id!r}")
        object.__setattr__(self, "allowed_mutations", tuple(self.allowed_mutations))
        unknown = [m for m in self.allowed_mutations if m not in ALLOWED_RUNNING_MUTATIONS]
        if unknown:
            raise ValueError(
                f"running region {self.object_id!r} allows {unknown}, which is more than the "
                f"{list(ALLOWED_RUNNING_MUTATIONS)} the doc permits; anything else in a header or "
                "footer is a layout change, and a layout change is a new template version")

    def anchors(self) -> tuple:
        return (self.anchor,)


@dataclass(frozen=True, kw_only=True)
class SignatureObject(SemanticObject):
    """SIGNATURE -- "anchor, signer_source_ref, image_policy, e-sign integration ref"."""

    object_type: str = SIGNATURE
    anchor: Anchor
    signer_source_ref: str
    image_policy: str
    esign_ref: str | None

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.anchor, Anchor):
            raise TypeError(
                f"signature {self.object_id!r} needs an Anchor, got {type(self.anchor).__name__}")
        self.anchor.assert_production_safe(f"signature {self.object_id!r}")
        _require_text(self.signer_source_ref, "signer_source_ref", f"signature {self.object_id!r}")
        if self.image_policy not in IMAGE_POLICIES:
            raise ValueError(
                f"signature {self.object_id!r} declares image_policy={self.image_policy!r}; expected "
                f"one of {list(IMAGE_POLICIES)}")
        if self.image_policy == "ESIGN" and not (self.esign_ref or "").strip():
            raise ValueError(
                f"signature {self.object_id!r} routes to e-sign but names no integration; the letter "
                "would be issued with a signature block nothing ever signs")

    def anchors(self) -> tuple:
        return (self.anchor,)

    def source_refs(self) -> tuple:
        return (self.signer_source_ref,)


def _validated_test_cases(test_cases, object_id: str, expects) -> tuple:
    """Every test case is `{"in": {...}, "expect": ...}`, and there is at least one.

    "Every expression parses, type-checks and has at least one passing test
    case." An expression with no test case is an assertion about a legal document
    that nobody has ever checked, and §7's worked example makes the point that
    the interesting case -- incomplete source data -- is precisely the one an
    author omits when nothing forces them to write it down.
    """
    cases = tuple(test_cases or ())
    if not cases:
        raise ValueError(
            f"object {object_id!r} carries an expression but no test case; a rule nobody has ever "
            "run against a record is not reviewable")
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise TypeError(f"test case {i} of {object_id!r} must be a dict, got {type(case).__name__}")
        if not isinstance(case.get("in"), dict):
            raise ValueError(f"test case {i} of {object_id!r} carries no input record under 'in'")
        if "expect" not in case:
            raise ValueError(f"test case {i} of {object_id!r} declares no expected result")
        if expects is not None and case["expect"] not in expects:
            raise ValueError(
                f"test case {i} of {object_id!r} expects {case['expect']!r}; expected one of "
                f"{list(expects)}")
    return cases


# ---- manifest-level rules over a set of objects ----

def source_field_name(source_ref: str) -> str:
    """`source.new_manager_name` -> `new_manager_name`.

    The doc writes source refs with the `source.` prefix inside expressions and
    without it in the field lists a source file is validated against. Normalising
    in one place keeps the closure from carrying the same column twice under two
    spellings.
    """
    ref = _require_text(source_ref, "source_ref").strip()
    return ref[len("source."):] if ref.startswith("source.") else ref


def required_source_fields(objects) -> tuple:
    """The doc's "computed closure of all referenced fields, not a hand-written list".

    Grounding sources are deliberately not in it: a NARRATIVE grounds on
    documents, not on columns of the source record, and folding them in would
    make a source file fail validation for missing a field that was never a
    field.
    """
    names = set()
    for obj in objects:
        for ref in obj.source_refs():
            names.add(source_field_name(ref))
    return tuple(sorted(names))


def overlapping_anchor_ranges(objects) -> list:
    """Pairs of object ids whose regions intersect -- the doc's fifth lock rule.

    Returned rather than raised, because a review screen wants every conflict at
    once and the caller decides whether an overlap blocks a lock or a save.
    """
    ranged = [(obj.object_id, rng) for obj in objects for rng in obj.anchor_ranges()]
    clashes = []
    for i, (left_id, left) in enumerate(ranged):
        for right_id, right in ranged[i + 1:]:
            if left.overlaps(right):
                clashes.append((left_id, right_id))
    return clashes


@dataclass(frozen=True)
class LockBlocker:
    """One reason a manifest cannot be locked."""

    rule: str
    detail: str
    object_id: str | None = None

    def as_dict(self) -> dict:
        return {"rule": self.rule, "detail": self.detail, "object_id": self.object_id}


def lock_blockers(objects, *, template=None) -> list:
    """The §6 lock rules that are computable from the object model alone.

    Not all of them: type-checking a source_ref against a pinned source schema
    and matching a STATIC hash against the template's bytes both need material
    this function is not given, and a test generation over representative records
    needs a renderer. Those belong where the material is. What is here is what an
    object set can answer about itself -- statuses, overlapping regions,
    expressions that parse, test cases that pass -- plus anchor resolution when a
    `template` is supplied, which is the rule most likely to have quietly stopped
    being true since the manifest was compiled.
    """
    blockers = []

    seen = set()
    for obj in objects:
        if obj.object_id in seen:
            blockers.append(LockBlocker(
                "duplicate_object_id",
                f"more than one object carries the id {obj.object_id!r}", obj.object_id))
        seen.add(obj.object_id)

        if not obj.is_lockable:
            blockers.append(LockBlocker(
                "object_not_approved",
                f"object {obj.object_id!r} is {obj.status}; a locked manifest carries only "
                f"{list(LOCKABLE_STATUSES)}", obj.object_id))

        if isinstance(obj, ConditionObject) and obj.expression_lang == DOCUMIND_EXPR_LANG:
            for result in run_test_cases(obj):
                if not result.passed:
                    blockers.append(LockBlocker(
                        "test_case_failed",
                        f"test case {result.index} of {obj.object_id!r} expected {result.expected} "
                        f"and got {result.actual or result.error}", obj.object_id))

        if template is not None:
            for anchor in obj.anchors():
                blockers += _anchor_blockers(obj.object_id, anchor, template)
            for rng in obj.anchor_ranges():
                blockers += _anchor_blockers(obj.object_id, rng.start, template)
                blockers += _anchor_blockers(obj.object_id, rng.end, template)

    for left_id, right_id in overlapping_anchor_ranges(objects):
        blockers.append(LockBlocker(
            "overlapping_anchor_ranges",
            f"objects {left_id!r} and {right_id!r} both claim the same region", left_id))

    return blockers


def _anchor_blockers(object_id: str, anchor: Anchor, template) -> list:
    try:
        resolve(anchor, template)
    except AnchorError as exc:
        return [LockBlocker("anchor_does_not_resolve_once", str(exc), object_id)]
    return []

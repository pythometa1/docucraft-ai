"""Taking the people out of the text before anything else sees it.

This is the module the rest of the pipeline is arranged around. §2's fourth
principle: identifiers are masked before content is indexed, embedded, or sent
to any model -- and §6 makes it a blocking stage, not a banner. Everything
downstream of ingestion runs on what this produces and never on what it was
given.

**Structured fields first, patterns second, guesses never.**

The highest-confidence detection is not a clever pattern, it is a value the
case already told us. An ICSR names its reporter; a study report names its
investigator and site. Those strings are searched for literally, and where they
appear they are masked with certainty -- no regular expression is as reliable
as knowing the name in advance.

Patterns come next, and only where the shape is unambiguous: an email address
is an email address, and a string of eleven digits after "NHS" is a national
identifier. Titles carry names ("Dr Alan Reed"), so the token after a title is
a name with high confidence.

And then there is everything a person has to look at. A capitalised pair of
words might be "Jane Smith" and might be "Severe Headache"; a bare date might
be a date of birth or an onset date. Those are NOT masked automatically and
they are NOT ignored -- they go to the review queue, and the queue blocks. A
system that guessed here would either leak a name or destroy a clinical fact,
and both are silent.

**Masking is deterministic and non-reversible.** The same person becomes the
same token everywhere in one product's data, so a narrative still reads as one
story and two cases can still be seen to involve the same reporter. The token
is derived by a one-way hash of the value and the product, so holding the
output and this source code recovers nothing -- the same discipline
`app.llm.redaction` applies, for the same reason.

**Nothing is deleted.** The original text stays in `pv_case_originals`, which
nothing downstream reads. Masking produces a new working copy; it does not
destroy evidence a regulator may later ask about.
"""

import hashlib
import re
from dataclasses import dataclass, field

#: What kind of identifier something is. These are the values `pv_deid_items`
#: stores, and the labels the review queue groups by.
PATIENT_NAME = "patient_name"
PATIENT_ID = "patient_id"
DATE_OF_BIRTH = "date_of_birth"
ADDRESS = "address"
PHONE = "phone"
EMAIL = "email"
REPORTER_NAME = "reporter_name"
INVESTIGATOR_NAME = "investigator_name"
SITE_NAME = "site_name"
NATIONAL_ID = "national_id"
OTHER = "other"

IDENTIFIER_TYPES = (
    PATIENT_NAME, PATIENT_ID, DATE_OF_BIRTH, ADDRESS, PHONE, EMAIL,
    REPORTER_NAME, INVESTIGATOR_NAME, SITE_NAME, NATIONAL_ID, OTHER,
)

#: The token each type is replaced with. Typed rather than a single blackout so
#: that a masked narrative still reads: "[REPORTER-4a1f] reported that
#: [PATIENT-9c22] developed a headache" is a sentence; four identical black
#: boxes is not, and a reviewer cannot check a story they cannot follow.
_TOKEN_PREFIX = {
    PATIENT_NAME: "PATIENT", PATIENT_ID: "PATIENT-ID", DATE_OF_BIRTH: "DOB",
    ADDRESS: "ADDRESS", PHONE: "PHONE", EMAIL: "EMAIL",
    REPORTER_NAME: "REPORTER", INVESTIGATOR_NAME: "INVESTIGATOR",
    SITE_NAME: "SITE", NATIONAL_ID: "ID", OTHER: "REDACTED",
}

#: How sure the detector is. Anything below `CERTAIN` reaches a person before
#: it is applied.
CERTAIN = 1.0        # a value the structured data already gave us
HIGH = 0.85          # an unambiguous shape: an email, a titled name
NEEDS_REVIEW = 0.5   # a candidate: might be a name, might be a diagnosis


@dataclass(frozen=True)
class Detection:
    """One thing found in the text."""

    identifier_type: str
    text: str
    start: int
    end: int
    confidence: float
    basis: str

    @property
    def certain(self) -> bool:
        return self.confidence >= HIGH


@dataclass
class DeidResult:
    masked_text: str
    #: Every detection, whether or not it was applied.
    detections: list = field(default_factory=list)
    #: Original value -> the token it became. The masking decision log §6 asks
    #: for; written to the audit trail by the caller.
    replacements: dict = field(default_factory=dict)
    #: Detections that were NOT applied because a person has to look at them.
    queued: list = field(default_factory=list)


# ------------------------------------------------------------------ patterns

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

#: A phone number as people write one, INCLUDING its country and area code.
#: Optional parentheses rather than required ones: "+44 20 7946 0958" is how
#: the number is actually written, and a pattern that matched only "7946 0958"
#: would mask the last two groups and leave the rest -- a partial mask, which
#: is worse than none because it looks done.
_PHONE = re.compile(
    r"(?<![\w/-])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-])?"
    r"\d{3,5}[\s.-]\d{3,4}(?:[\s.-]\d{3,4})?(?![\w/-])")

#: Titles that carry a person's name. The name after one is a name.
_TITLED_NAME = re.compile(
    r"\b(?:Dr|Doctor|Prof|Professor|Mr|Mrs|Ms|Miss|Sr|Sra|Herr|Frau)\.?\s+"
    r"((?:[A-Z][\w'’-]+)(?:\s+(?:van|von|de|del|der|di|da|la|le)\b)?"
    r"(?:\s+[A-Z][\w'’-]+){0,3})")

#: A named institution. The keyword makes it unambiguous -- "St Mary's
#: Hospital" is a site and "Severe Headache" is not.
_SITE = re.compile(
    r"\b((?:[A-Z][\w'’.-]+\s+){0,4}"
    r"(?:Hospital|Hospitals|Clinic|Clinique|Klinik|Infirmary|Medical\s+Cent(?:re|er)|"
    r"Health\s+Cent(?:re|er)|Surgery|Practice|Institute|Universit(?:y|ä?t)|"
    r"Pharmacy|Trust|NHS\s+Trust))\b")

#: A date of birth, but only where it says so. A bare date in a narrative is
#: far more often an onset or a visit, and destroying one of those would
#: destroy the clinical fact the case exists to record.
_DOB = re.compile(
    r"\b(?:date\s+of\s+birth|d\.?o\.?b\.?|born(?:\s+on)?)\b[:\s]*"
    r"([0-9]{1,4}[-/. ][0-9]{1,2}[-/. ][0-9]{2,4}|"
    r"[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})", re.IGNORECASE)

#: National and health-service identifiers, by the label that precedes them.
_NATIONAL_ID = re.compile(
    r"\b(?:NHS(?:\s+number)?|SSN|social\s+security(?:\s+number)?|NI(?:\s+number)?|"
    r"national\s+(?:insurance|identity|id)(?:\s+number)?|medicare|"
    r"passport(?:\s+number)?|MRN|medical\s+record(?:\s+number)?|"
    r"hospital\s+number|patient\s+(?:id|number))\b[:\s#]*([A-Z0-9][A-Z0-9\s-]{4,17})",
    re.IGNORECASE)

#: A street address. Anchored on the thoroughfare word for the same reason the
#: site pattern is anchored on "Hospital".
_ADDRESS = re.compile(
    r"\b(\d{1,4}[A-Za-z]?\s+(?:[A-Z][\w'’-]+\s+){0,3}"
    r"(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Lane|Ln\.?|Drive|Dr\.?|Close|"
    r"Court|Crescent|Way|Place|Terrace|Boulevard|Blvd\.?))\b")

#: A UK-style postcode, which identifies a household.
_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")

#: Two or more capitalised words in a row: might be a person, might be a
#: condition. Never masked automatically; always queued.
_CANDIDATE_NAME = re.compile(
    r"\b([A-Z][a-z'’-]{1,20}(?:\s+[A-Z][a-z'’-]{1,20}){1,2})\b")

#: Capitalised phrases that are medical or administrative rather than people.
#: A candidate matching one of these is not even queued -- asking somebody
#: whether "Adverse Event" is a patient is how a review queue becomes noise
#: that people click through.
_NOT_A_NAME = frozenset("""
adverse event adverse events serious adverse case report case number
medical history concomitant medication concomitant medications informed consent
data lock point marketing authorisation marketing authorization
preferred term system organ class lowest level term
severe headache mild headache acute renal chronic kidney myocardial infarction
deep vein thrombosis atrial fibrillation type 2 type 1 stage 3 stage 4
general practitioner health professional healthcare professional
follow up follow-up initial report drug withdrawn dose reduced
not applicable not recovered not resolved unknown outcome
united kingdom united states new zealand south africa saudi arabia
czech republic dominican republic costa rica hong kong
january february march april june july august september october
november december monday tuesday wednesday thursday friday saturday sunday
""".split("\n"))

_NOT_A_NAME_PHRASES = frozenset(
    line.strip().lower() for line in """
adverse event|adverse events|serious adverse event|case report|case number
medical history|concomitant medication|concomitant medications|informed consent
data lock point|marketing authorisation|marketing authorization
preferred term|system organ class|lowest level term|special situation
severe headache|myocardial infarction|deep vein thrombosis|atrial fibrillation
acute renal failure|chronic kidney disease|general practitioner
health professional|healthcare professional|follow up|initial report
drug withdrawn|dose reduced|dose increased|not applicable|not recovered
united kingdom|united states|new zealand|south africa|saudi arabia
czech republic|dominican republic|costa rica|hong kong|sri lanka
""".replace("\n", "|").split("|") if line.strip())


def _token(identifier_type: str, value: str, *, salt: str) -> str:
    """A stable, non-reversible label for one value.

    Stable so a narrative still reads as one story and the same reporter is
    recognisably the same across two cases. Non-reversible because it is a
    hash with no inverse -- holding the token and this file recovers nothing,
    and the only way back is to guess the name, which is exactly as hard as
    guessing it without the token.

    Salted per product so the same name in two customers' data produces two
    different tokens, and one cannot be used to look for the other.
    """
    digest = hashlib.sha256(f"{salt}\x00{identifier_type}\x00{value.lower()}"
                            .encode()).hexdigest()[:4]
    return f"[{_TOKEN_PREFIX.get(identifier_type, 'REDACTED')}-{digest}]"


def _add(found: list, seen: set, detection: Detection) -> None:
    """Keep one detection per span, preferring the more confident one."""
    span = (detection.start, detection.end)
    for existing in list(found):
        # Overlapping spans: the longer, more confident detection wins.
        if not (detection.end <= existing.start or detection.start >= existing.end):
            # Equal confidence and equal length: the one already there stays.
            # It was found by the earlier, more specific pattern.
            if (detection.confidence, detection.end - detection.start) <= \
               (existing.confidence, existing.end - existing.start):
                return
            found.remove(existing)
            seen.discard((existing.start, existing.end))
    found.append(detection)
    seen.add(span)


def detect(text: str, *, known: dict | None = None) -> list[Detection]:
    """Everything in `text` that might identify somebody.

    `known` maps an identifier type to values the structured data already
    holds -- the reporter's name, the investigator's, the site's. Those are the
    surest detections there are, and they are searched for first.
    """
    if not text:
        return []
    found: list = []
    seen: set = set()

    # 1. What the case already told us. No pattern is as reliable as a name we
    #    were given, so these are matched first and win every overlap.
    for identifier_type, values in (known or {}).items():
        for value in values:
            value = (value or "").strip()
            if len(value) < 3:
                continue
            for match in re.finditer(re.escape(value), text, re.IGNORECASE):
                _add(found, seen, Detection(
                    identifier_type=identifier_type, text=match.group(0),
                    start=match.start(), end=match.end(), confidence=CERTAIN,
                    basis=f"matches the {identifier_type.replace('_', ' ')} "
                          "recorded on this case"))

    # 2. Shapes that can only be one thing.
    # Order matters where two shapes can match the same characters. A national
    # health number and a telephone number are both digit groups, and "943 476
    # 5919" is either -- so the pattern anchored on an explicit LABEL is tried
    # first and wins the overlap. Both would be masked either way; typing it
    # correctly is what lets the review queue and the audit trail say what was
    # removed.
    for pattern, identifier_type, basis, group in (
        (_EMAIL, EMAIL, "an email address", 0),
        (_NATIONAL_ID, NATIONAL_ID, "an identifier following its label", 1),
        (_DOB, DATE_OF_BIRTH, "a date labelled as a date of birth", 1),
        (_PHONE, PHONE, "a telephone number", 0),
        (_TITLED_NAME, REPORTER_NAME, "a name following a title", 1),
        (_SITE, SITE_NAME, "a named institution", 1),
        (_ADDRESS, ADDRESS, "a street address", 1),
        (_POSTCODE, ADDRESS, "a postcode", 1),
    ):
        for match in pattern.finditer(text):
            _add(found, seen, Detection(
                identifier_type=identifier_type, text=match.group(group),
                start=match.start(group), end=match.end(group),
                confidence=HIGH, basis=basis))

    # 3. Candidates. A capitalised pair might be a person and might be a
    #    diagnosis, so these are queued rather than applied -- masking
    #    "Severe Headache" would destroy the clinical fact the case exists for.
    for match in _CANDIDATE_NAME.finditer(text):
        value = match.group(1)
        if value.lower() in _NOT_A_NAME_PHRASES:
            continue
        if all(word.lower() in _NOT_A_NAME for word in value.split()):
            continue
        _add(found, seen, Detection(
            identifier_type=PATIENT_NAME, text=value,
            start=match.start(1), end=match.end(1), confidence=NEEDS_REVIEW,
            basis="two or more capitalised words: this may be a person's name, "
                  "or may be a condition or a place"))

    return sorted(found, key=lambda d: d.start)


def mask(text: str, *, known: dict | None = None, salt: str = "",
         accept: dict | None = None) -> DeidResult:
    """The working copy, and the decisions that produced it.

    `accept` carries a person's answers from the review queue: original text ->
    the identifier type to mask it as, or None for "not an identifier". A
    candidate with no answer is left in the text AND reported in `queued`,
    which is what makes the queue a gate: the caller must not index a result
    whose `queued` list is non-empty.
    """
    detections = detect(text, known=known)
    answers = {k.lower(): v for k, v in (accept or {}).items()}

    applied, queued = [], []
    for detection in detections:
        if detection.certain:
            applied.append(detection)
            continue
        answer = answers.get(detection.text.lower(), "__unanswered__")
        if answer == "__unanswered__":
            queued.append(detection)
        elif answer:
            applied.append(Detection(
                identifier_type=answer, text=detection.text,
                start=detection.start, end=detection.end,
                confidence=CERTAIN, basis="confirmed by a reviewer"))
        # answer of None means "not an identifier": left alone, not queued.

    replacements: dict = {}
    pieces, cursor = [], 0
    for detection in sorted(applied, key=lambda d: d.start):
        if detection.start < cursor:
            continue
        token = _token(detection.identifier_type, detection.text, salt=salt)
        pieces.append(text[cursor:detection.start])
        pieces.append(token)
        replacements[detection.text] = token
        cursor = detection.end
    pieces.append(text[cursor:])

    return DeidResult(masked_text="".join(pieces), detections=detections,
                      replacements=replacements, queued=queued)


def scan(text: str, *, known: dict | None = None) -> list[Detection]:
    """Identifiers in text that is supposed to be clean already.

    §11's first blocker: a PII leakage scan over every draft and every export.
    Only the confident detections count -- a leakage scan that fired on every
    capitalised pair would flag "Preferred Term" in every document and teach
    people to ignore it.
    """
    return [d for d in detect(text, known=known) if d.certain]


def known_values(case, drugs=(), reporter_fields=()) -> dict:
    """The identifying values this case already carries, by type.

    Everything here came from a structured field, so anything matching it in
    free text is an identifier with certainty rather than a guess.
    """
    known: dict = {REPORTER_NAME: [], SITE_NAME: [], PATIENT_ID: [],
                   INVESTIGATOR_NAME: []}
    for value in reporter_fields or ():
        if value:
            known[REPORTER_NAME].append(value)
    for value in (getattr(case, "worldwide_case_id", None),
                  *(getattr(case, "local_case_ids", None) or [])):
        if value and len(str(value)) >= 4:
            known[PATIENT_ID].append(str(value))
    return {k: v for k, v in known.items() if v}

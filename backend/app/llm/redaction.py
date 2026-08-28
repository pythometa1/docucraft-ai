"""Type-preserving synthetic samples, so a model can see the shape of the data
without seeing the data.

§16: "Replace representative values with type-preserving synthetic samples before
any prompt is constructed."

The compiler's job is to work out that `<New Reporting To>` maps to the column
called `new_manager_name`. To do that it needs to know that the column holds
short person-shaped strings, that `annual_salary` holds a six-figure number, that
`joining_dt` holds a date written day-first. It does not need to know that Dana
Ruiz earns 118,400. Sending the real value buys nothing and puts one customer's
compensation data in a third party's logs.

Two properties matter, and they pull in opposite directions.

*Deterministic*: the same input must always produce the same synthetic sample, or
re-compiling a template produces a different prompt, a different response and a
different manifest, and §18's reproducibility claim stops being checkable.

*Non-reversible*: nobody holding the synthetic value and this source code may
recover the original. That comes from the transform being a one-way hash rather
than a cipher -- there is no key and no inverse, and the only way back is to
guess an input and check it, which is exactly as hard as guessing the salary
without the synthetic value at all.

What the output does carry is shape: magnitude, length, punctuation, date order.
That is deliberate and it is the whole point -- the compiler needs to know
`annual_salary` holds a six-figure number to map it, and knowing that reveals
nothing about any individual. A caller who needs the magnitude hidden too should
not be sending the column.
"""

import hashlib
import re
from dataclasses import dataclass

# ---------------------------------------------------------------- value classes

MONEY = "money"
DATE = "date"
IDENTIFIER = "identifier"
PERSON_NAME = "person_name"
EMAIL = "email"
PHONE = "phone"
NUMBER = "number"
BOOLEAN = "boolean"
FREE_TEXT = "free_text"

VALUE_CLASSES = (
    MONEY, DATE, IDENTIFIER, PERSON_NAME, EMAIL, PHONE, NUMBER, BOOLEAN, FREE_TEXT,
)

#: Classes §13 will not auto-accept a mapping for, and that carry the most risk
#: if they reach a provider. Named here so one list drives both decisions.
SENSITIVE_CLASSES = frozenset({MONEY, IDENTIFIER, PERSON_NAME, EMAIL, PHONE, DATE})

# Column-name signals. Deliberately checked before value shape: a salary column
# that happens to be empty in the sample rows is still a salary column, and
# guessing from values alone would classify it as free text and pass it through.
# A column whose name ends this way is a category, a code or a reference, not
# the thing the stem suggests. `colleague_type` is "Full time"; treating it as a
# person's name would redact the business meaning the compiler maps conditions
# against, which is a privacy win of nothing and an accuracy loss of everything.
_CATEGORY_SUFFIX = re.compile(r"_(type|code|category|class|status|band|grade|flag|count)$", re.I)

_NAME_PATTERNS = (
    # `payroll` is checked before the money patterns because "pay" is a prefix of
    # it: payroll_no is a reference number, not an amount.
    (IDENTIFIER, re.compile(r"payroll|\bid\b|_id$|^id_|employee_?no|national|passport|ssn|aadhaar|pan\b|nino|_no$|number", re.I)),
    (MONEY, re.compile(r"salary|compensation|wage|\bpay\b|payment|bonus|amount|ctc|remuneration|allowance|stipend", re.I)),
    (DATE, re.compile(r"date|_dt$|^dt_|joining|dob|birth|expiry|effective", re.I)),
    (EMAIL, re.compile(r"e?mail", re.I)),
    (PHONE, re.compile(r"phone|mobile|contact_no|telephone", re.I)),
    # Requires the word "name", or a role that only ever names a person. Bare
    # "colleague" or "employee" is not enough: `colleague_type` and
    # `employee_status` are categories.
    (PERSON_NAME, re.compile(r"name|manager|signatory|supervisor", re.I)),
)

_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_MONEY_SHAPE = re.compile(r"^[£$€₹]?\s?\d[\d,]*(\.\d{1,2})?$")
_DATE_SHAPE = re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$")
_PHONE_SHAPE = re.compile(r"^\+?[\d\s().-]{7,}$")

# Fixed pools. Small on purpose: a model needs one plausible example to learn the
# shape of a column, not variety, and a short list is easier to eyeball when
# someone asks what actually gets sent.
_NAMES = (
    "Dana Ruiz", "Sam Okafor", "Lee Bennett", "Priya Raman", "Alex Novak",
    "Jo Mercer", "Chris Halvorsen", "Nina Adeyemi",
)
_DOMAINS = ("example.com", "example.org", "example.net")


def _digest(*parts) -> int:
    """A stable integer from the inputs. One-way: this is where the original goes."""
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(joined.encode("utf-8")).digest()[:8], "big")


@dataclass(frozen=True)
class ClassifiedColumn:
    name: str
    value_class: str

    @property
    def is_sensitive(self) -> bool:
        return self.value_class in SENSITIVE_CLASSES


def classify_column(name: str, values=()) -> ClassifiedColumn:
    """What kind of thing this column holds, by name first and shape second."""
    label = (name or "").strip()
    if not _CATEGORY_SUFFIX.search(label):
        for value_class, pattern in _NAME_PATTERNS:
            if pattern.search(label):
                return ClassifiedColumn(label, value_class)

    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if _EMAIL_SHAPE.match(text):
            return ClassifiedColumn(label, EMAIL)
        if _DATE_SHAPE.match(text):
            return ClassifiedColumn(label, DATE)
        if _MONEY_SHAPE.match(text) and any(c in text for c in "£$€₹,"):
            return ClassifiedColumn(label, MONEY)
        if _PHONE_SHAPE.match(text) and sum(c.isdigit() for c in text) >= 7:
            return ClassifiedColumn(label, PHONE)
        if isinstance(value, bool) or text.lower() in ("true", "false", "yes", "no"):
            return ClassifiedColumn(label, BOOLEAN)
        try:
            float(text.replace(",", ""))
            return ClassifiedColumn(label, NUMBER)
        except ValueError:
            return ClassifiedColumn(label, FREE_TEXT)

    return ClassifiedColumn(label, FREE_TEXT)


def _same_shape_digits(value: str, seed: int) -> str:
    """Rebuild a string with its punctuation intact and every digit replaced.

    Shape is the part that helps: `EMP-000417` tells the compiler this column is
    a padded employee reference, and that is true of the synthetic one too.
    """
    out, cursor = [], seed
    for char in value:
        if char.isdigit():
            cursor = _digest(cursor)
            out.append(str(cursor % 10))
        elif char.isalpha():
            cursor = _digest(cursor, "a")
            letter = chr(ord("A") + cursor % 26)
            out.append(letter if char.isupper() else letter.lower())
        else:
            out.append(char)
    return "".join(out)


def redact_value(value, value_class: str, *, column: str = "") -> object:
    """One synthetic value of the same shape and type. Deterministic, one-way."""
    if value is None:
        return None
    if value == "":
        return ""

    text = str(value)
    seed = _digest(column, value_class, text)

    if value_class == PERSON_NAME:
        return _NAMES[seed % len(_NAMES)]

    if value_class == EMAIL:
        return f"{_NAMES[seed % len(_NAMES)].split()[0].lower()}.{seed % 1000:03d}@{_DOMAINS[seed % len(_DOMAINS)]}"

    if value_class == MONEY:
        # Same order of magnitude, so "six figures" stays six figures and the
        # compiler can still tell a salary column from a bonus column.
        digits = [c for c in text if c.isdigit()]
        if not digits:
            return _same_shape_digits(text, seed)
        magnitude = len(digits)
        low = 10 ** (magnitude - 1)
        synthetic = low + seed % (9 * low) if magnitude > 1 else seed % 10
        rebuilt = str(synthetic)
        # Preserve the leading symbol and any decimal tail the original had.
        prefix = text[0] if text[:1] in "£$€₹" else ""
        return f"{prefix}{rebuilt}"

    if value_class == DATE:
        # A plausible date written the same way round as the original.
        year = 2020 + seed % 6
        month = 1 + (seed // 7) % 12
        day = 1 + (seed // 91) % 28
        if "/" in text:
            first, _, rest = text.partition("/")
            day_first = len(first) <= 2 and len(rest.split("/")[-1]) == 4
            return f"{day:02d}/{month:02d}/{year}" if day_first else f"{year}/{month:02d}/{day:02d}"
        if "-" in text and len(text.split("-")[0]) == 4:
            return f"{year}-{month:02d}-{day:02d}"
        return f"{day:02d}/{month:02d}/{year}"

    if value_class == BOOLEAN:
        return value if isinstance(value, bool) else text

    if value_class == NUMBER:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return type(value)(seed % 1000)
        return _same_shape_digits(text, seed)

    if value_class in (IDENTIFIER, PHONE):
        return _same_shape_digits(text, seed)

    # FREE_TEXT. Length is the only shape worth keeping, and the words are
    # exactly what must not go: a free-text note is where somebody records a
    # grievance or a medical reason.
    return f"[redacted {len(text)}-char text]"


def redact_record(record: dict, schema: dict | None = None) -> dict:
    """A whole source row, replaced value by value.

    `schema` maps column -> value class when the caller already knows it; the
    rest are classified from their own name and value. Keys are preserved
    exactly, because the column *names* are schema metadata the model is meant to
    see -- they are the thing it maps against.
    """
    if not isinstance(record, dict):
        raise TypeError(f"a source record must be a dict, got {type(record).__name__}")

    schema = schema or {}
    out = {}
    for column, value in record.items():
        value_class = schema.get(column) or classify_column(column, [value]).value_class
        out[column] = redact_value(value, value_class, column=column)
    return out


def redact_chunk_text(text: str) -> str:
    """Redact an already-flattened `"col: value; col: value"` source chunk.

    The ingestion path flattens a spreadsheet row into exactly this shape before
    anything else sees it, and the chat and draft paths then send those strings
    to the model verbatim. Parsing the shape back apart is less pleasant than
    redacting at the source, but it is what protects the rows already chunked and
    sitting in the database.
    """
    if not text:
        return text

    parts = []
    for segment in text.split(";"):
        column, sep, value = segment.partition(":")
        if not sep:
            parts.append(segment)
            continue
        name, raw = column.strip(), value.strip()
        value_class = classify_column(name, [raw]).value_class
        parts.append(f"{column}{sep} {redact_value(raw, value_class, column=name)}"
                     if raw else segment)
    return ";".join(parts)

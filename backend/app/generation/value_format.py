"""Locale-aware rendering of resolved values.

A worldwide template estate cannot hard-code US conventions. `1,234.56` is
wrong in Germany (`1.234,56`) and wrong in India (`1,23,456.00` — lakh
grouping, not thousands). Dates are worse: `04/08/2026` is 4 August in most of
the world and 8 April in the US, and a wrong date on an offer letter or a tox
report is a real defect, not a cosmetic one.

Resolution order for a locale: the field's own setting, then the manifest's,
then the project's region default, then `en_US`. Every step is explicit so a
reviewer can see which one applied.

Formatting never raises. A value that cannot be formatted is passed through
unchanged — a slightly-wrong separator is recoverable, a failed generation is
not.
"""

from datetime import date, datetime

from babel import Locale, UnknownLocaleError
from babel.dates import format_date
from babel.numbers import format_currency, format_decimal, format_percent

from app.expressions.token_parser import _as_number

DEFAULT_LOCALE = "en_US"

# Regions are how projects are actually tagged in this product, so map them to
# a sensible default rather than making every template author pick a locale.
# Deliberately empty, and kept as a named seam rather than deleted.
#
# This used to map a project's business region onto one country's conventions:
# "Europe" -> de_DE, "Asia Pacific" -> en_AU, "Latin America" -> es_419. A
# continent is not a locale. The effect was that filing an Australian offer
# letter under the "Europe" region silently rendered every figure in German
# form -- $82.000,00 instead of $82,000.00 -- with nothing in the UI, the
# lineage or the QA gates indicating a formatting decision had been made at
# all. A wrong number that looks deliberate is the worst outcome this pipeline
# can produce.
#
# Locale now comes only from somewhere that actually knows it: the field, the
# manifest, or an explicit locale on the generate request. Absent all three it
# falls back to DEFAULT_LOCALE, which is at least uniform and predictable.
# Populate this only with regions that map to exactly one locale.
REGION_LOCALES: dict[str, str] = {}

_DATE_INPUT_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
    "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
)

# Word template authors write patterns, not locale codes.
_TOKEN_PATTERNS = {
    "DD/MM/YYYY": "dd/MM/yyyy",
    "MM/DD/YYYY": "MM/dd/yyyy",
    "YYYY-MM-DD": "yyyy-MM-dd",
    "DD.MM.YYYY": "dd.MM.yyyy",
    "DD-MM-YYYY": "dd-MM-yyyy",
}


def resolve_locale(field: dict | None = None, manifest_locale: str | None = None, region: str | None = None,
                   project_locale: str | None = None) -> str:
    """The locale to write this value in, most specific source first."""
    return resolved_locale(field, manifest_locale, region, project_locale)[0]


def resolved_locale(field: dict | None = None, manifest_locale: str | None = None, region: str | None = None,
                    project_locale: str | None = None) -> tuple[str, str]:
    """`(locale, where_it_came_from)`.

    The second half is the point. Formatting silently fell back to
    `DEFAULT_LOCALE` for every project -- `REGION_LOCALES` is empty because the
    region vocabulary is continental -- so an Australian letter was written in
    American date style and nothing anywhere said a choice had been made. A
    reviewer could read the document, see "May 9, 2024", and have no way to tell
    a configured decision from a default nobody made.

    Naming the source makes it auditable without failing a generation that has
    always worked. `"default"` is a real answer and it is recorded as one.
    """
    for candidate, source in (
        ((field or {}).get("locale"), "field"),
        (manifest_locale, "manifest"),
        (project_locale, "project"),
        (REGION_LOCALES.get(region or ""), "region"),
        (DEFAULT_LOCALE, "default"),
    ):
        if not candidate:
            continue
        normalised = str(candidate).replace("-", "_")
        try:
            Locale.parse(normalised)
            return normalised, source
        except (UnknownLocaleError, ValueError):
            continue
    return DEFAULT_LOCALE, "default"


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def format_value(value, field: dict | None = None, locale: str = DEFAULT_LOCALE) -> str:
    """Render one resolved value for insertion into the document."""
    if value is None:
        return ""
    field = field or {}
    field_type = field.get("type", "string")

    try:
        if field_type == "currency":
            return _format_currency(value, field, locale)
        if field_type == "percent":
            return _format_percent(value, field, locale)
        if field_type in ("number", "quantity"):
            return _format_number(value, field, locale)
        if field_type == "date":
            return _format_date(value, field, locale)
    except Exception:
        # Never let a formatting edge case break a generation.
        return str(value)

    return str(value)


def _grouping_pattern(locale: str, decimals: int) -> str:
    """The locale's own integer grouping, with a fixed number of decimals.

    A hardcoded `#,##0.00` looks locale-aware and is not: it forces Western
    three-digit grouping everywhere, so an Indian salary rendered `1,200,000.00`
    where the reader expects `12,00,000.00`. Taking the pattern from the locale
    keeps lakh/crore grouping, and every other convention CLDR knows about.
    """
    try:
        pattern = Locale.parse(locale).decimal_formats[None].pattern
    except (UnknownLocaleError, ValueError, KeyError):
        pattern = "#,##0.###"
    integer_part = pattern.split(".")[0]
    return f"{integer_part}.{'0' * decimals}" if decimals else integer_part


def _format_currency(value, field: dict, locale: str) -> str:
    number = _as_number(value)
    if number is None:
        return str(value)

    decimals = field.get("decimals")
    decimals = 2 if not isinstance(decimals, int) else decimals

    code = field.get("currency")
    if not code:
        # Deliberately number-only when no currency is declared: this template
        # class writes the symbol as static text immediately before the field,
        # so emitting one here would render "$$72,000.00".
        return format_decimal(number, format=_grouping_pattern(locale, decimals), locale=locale)
    return format_currency(number, code, locale=locale)


def _format_percent(value, field: dict, locale: str) -> str:
    """A rate stored as a fraction, rendered as a percentage.

    Source data holds 0.05 for a 5% variable-pay component, and writing "0.05"
    into "a variable component of 0.05 of CTC" is wrong in a way a reader will
    notice immediately.
    """
    number = _as_number(value)
    if number is None:
        return str(value)
    decimals = field.get("decimals")
    return format_percent(
        number, locale=locale,
        format=_grouping_pattern(locale, decimals if isinstance(decimals, int) else 0) + "%",
    )


def _format_number(value, field: dict, locale: str) -> str:
    number = _as_number(value)
    if number is None:
        return str(value)
    decimals = field.get("decimals")
    if isinstance(decimals, int):
        text = format_decimal(number, format=_grouping_pattern(locale, decimals), locale=locale)
    else:
        text = format_decimal(number, locale=locale)
    unit = field.get("unit")
    return f"{text} {unit}" if unit else text


def _format_date(value, field: dict, locale: str) -> str:
    parsed = parse_date(value)
    if parsed is None:
        return str(value)
    pattern = field.get("format")
    if pattern:
        # Honour an explicit author pattern over the locale default -- a
        # template that says DD/MM/YYYY means it.
        return format_date(parsed, format=_TOKEN_PATTERNS.get(pattern.upper(), pattern), locale=locale)
    return format_date(parsed, format="medium", locale=locale)

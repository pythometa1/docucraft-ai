"""Locale rendering and structural clustering.

Both exist because the product targets a worldwide estate. Both fail silently
if wrong: `1,234.56` is simply the wrong number in Germany, and clustering by
text groups an estate by language rather than by document type.
"""

import pytest

from app.templates.fingerprint import StructuralFingerprint, structural_similarity
from app.generation.value_format import format_value, parse_date, resolve_locale


# --------------------------------------------------------------------- locale
@pytest.mark.parametrize("locale,expected", [
    ("en_US", "72,000.00"),
    ("de_DE", "72.000,00"),   # separators swap
    ("fr_FR", "72 000,00"),
])
def test_currency_grouping_follows_locale(locale, expected):
    assert format_value("72000", {"type": "currency"}, locale) == expected


def test_indian_grouping_is_lakh_not_thousands():
    """en_IN groups 12,34,567 — a plain thousands separator is wrong there."""
    assert format_value("1234567", {"type": "number"}, "en_IN") == "12,34,567"


def test_currency_omits_the_symbol_unless_one_is_declared():
    """This template class writes '$' as static text immediately before the
    field, so emitting a symbol here renders '$$72,000.00'."""
    assert "$" not in format_value("72000", {"type": "currency"}, "en_US")
    assert "A$" in format_value("72000", {"type": "currency", "currency": "AUD"}, "en_US")


@pytest.mark.parametrize("raw", ["2026-08-04", "04/08/2026", "04.08.2026", "4 Aug 2026"])
def test_dates_parse_from_the_formats_spreadsheets_actually_use(raw):
    assert parse_date(raw).isoformat() == "2026-08-04"


def test_author_date_pattern_wins_over_locale_default():
    """A template that says DD/MM/YYYY means it."""
    assert format_value("2026-08-04", {"type": "date", "format": "DD/MM/YYYY"}, "en_US") == "04/08/2026"


def test_unformattable_values_pass_through_rather_than_breaking_generation():
    assert format_value("not a number", {"type": "currency"}, "de_DE") == "not a number"
    assert format_value("sometime next year", {"type": "date"}, "en_US") == "sometime next year"


def test_locale_resolution_order():
    assert resolve_locale({"locale": "de-DE"}, "en_GB", "North America") == "de_DE"
    assert resolve_locale({}, "en_GB", "North America") == "en_GB"
    assert resolve_locale({}, None, None) == "en_US"
    assert resolve_locale({"locale": "not-a-locale"}, None, None) == "en_US"


def test_a_business_region_never_picks_a_number_format():
    """"Europe" used to resolve to de_DE, so an Australian offer letter filed
    under the Europe region rendered $82.000,00. A continent is not a locale,
    and the choice was invisible -- no lineage entry, no QA note, no UI."""
    for region in ("Europe", "Asia Pacific", "Latin America", "Middle East & Africa", "Global"):
        assert resolve_locale({}, None, region) == "en_US", region


# ----------------------------------------------------------------- clustering
def _fingerprint(**kw) -> StructuralFingerprint:
    base = dict(
        mergefield_codes=frozenset({"LAB__FT_SALARY__38_HR_", "LAB__FT_TP_38_HR"}),
        paragraph_band=3, table_count=1, table_shapes=(12,),
        blue_span_count=17, red_span_count=27, hyperlink_count=1,
        bracket_tokens=frozenset({"colleague first name", "position title"}),
    )
    base.update(kw)
    return StructuralFingerprint(**base)


def test_a_translated_template_still_matches_its_source():
    """The whole point: text similarity would score these near zero, because a
    German offer letter shares almost no vocabulary with its English twin. The
    MERGEFIELD codes and the layout do not change."""
    english = _fingerprint()
    german = _fingerprint(bracket_tokens=frozenset({"vorname des kollegen", "positionsbezeichnung"}))
    assert structural_similarity(english, german) >= 0.6


def test_an_unrelated_template_does_not_match():
    offer_letter = _fingerprint()
    invoice = StructuralFingerprint(
        mergefield_codes=frozenset({"INV_TOTAL", "INV_DATE"}),
        paragraph_band=0, table_count=1, table_shapes=(3,),
        blue_span_count=2, red_span_count=0, hyperlink_count=0,
        bracket_tokens=frozenset({"invoice number"}),
    )
    assert structural_similarity(offer_letter, invoice) < 0.5


def test_templates_without_mergefields_are_not_penalised():
    """An absent signal is absent, not zero — otherwise every uncoloured
    template looks maximally dissimilar to every other one."""
    a = _fingerprint(mergefield_codes=frozenset())
    b = _fingerprint(mergefield_codes=frozenset())
    assert structural_similarity(a, b) >= 0.9


# ------------------------------------------------- grouping comes from CLDR
def test_indian_currency_uses_lakh_grouping():
    """`format="#,##0.00"` looks locale-aware and is not -- it forces Western
    three-digit grouping everywhere, so an Indian salary rendered
    `1,200,000.00` where the letter should read `12,00,000.00`."""
    assert format_value("1200000", {"type": "currency"}, "en_IN") == "12,00,000.00"
    assert format_value("1200000", {"type": "currency", "decimals": 0}, "en_IN") == "12,00,000"


@pytest.mark.parametrize("locale,expected", [
    ("en_US", "1,200,000.00"),
    ("de_DE", "1.200.000,00"),
    ("en_IN", "12,00,000.00"),
])
def test_currency_grouping_still_follows_each_locale(locale, expected):
    assert format_value("1200000", {"type": "currency"}, locale) == expected


def test_a_rate_stored_as_a_fraction_renders_as_a_percentage():
    """Source data holds 0.05 for a 5% variable-pay component; "a variable
    component of 0.05 of CTC" is wrong in a way any reader spots."""
    assert format_value("0.05", {"type": "percent"}, "en_IN") == "5%"
    assert format_value("0.125", {"type": "percent", "decimals": 1}, "en_US") == "12.5%"


def test_a_number_with_declared_decimals_keeps_locale_grouping():
    assert format_value("1234567", {"type": "number", "decimals": 2}, "en_IN") == "12,34,567.00"

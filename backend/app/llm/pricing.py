"""What a model call costs, and where that number came from.

There was no pricing anywhere in this codebase. Not a table, not a constant, not
a column -- while `llm/provider.py` carried a comment saying Gemini folds its
thinking tokens into the output count "so the cost figures on the analytics page
compare like with like", about a page that had never shown a cost.

Three rules, each of which exists because the alternative produces a number
somebody would act on and should not.

**A rate is read from the vendor, with the date.** Every figure below was taken
from the vendor's own published pricing page on the date in its `source`, not
recalled. A price carried forward from memory is a plausible number attached to
somebody's invoice.

**An unpriced model has no cost -- not a cost of zero.** `rate_for` returns
`None`, `cost_micro_usd` returns `None`, and the column is nullable so it can
hold that. `metrics.py` already holds this line for measurements ("no data" must
never render as `0.0`); it matters more for money, because a call that looks free
is a call nobody investigates.

**Money is integer micro-dollars, never a float or a `Numeric`.** `Numeric`
round-trips through float on SQLite, which is the backend the test suite runs on,
so a column of them does not sum exactly. Micro-dollars per thousand tokens gives
six decimal places of a dollar -- finer than any published rate -- and sums
exactly on both backends.
"""

from dataclasses import dataclass

#: Read from each vendor's published pricing page on this date. Update the
#: figures and this date together, never one without the other.
RATES_READ_ON = "2026-08-30"


@dataclass(frozen=True)
class ModelRate:
    """What one model costs, per thousand tokens, in millionths of a dollar.

    Per *thousand* rather than per million because a single call is measured in
    thousands, and an integer rate per ktok keeps the multiplication exact:
    `tokens * rate // 1000` never needs a float.
    """

    model: str
    input_micro_usd_per_ktok: int
    output_micro_usd_per_ktok: int
    source: str
    note: str = ""

    @property
    def input_usd_per_mtok(self) -> float:
        return self.input_micro_usd_per_ktok / 1000.0

    @property
    def output_usd_per_mtok(self) -> float:
        return self.output_micro_usd_per_ktok / 1000.0

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "source": self.source,
            "note": self.note,
        }


def _rate(model: str, input_usd_per_mtok: float, output_usd_per_mtok: float,
          source: str, note: str = "") -> ModelRate:
    """Declare a rate in the units the vendor publishes, store it in ours."""
    return ModelRate(
        model=model,
        input_micro_usd_per_ktok=round(input_usd_per_mtok * 1000),
        output_micro_usd_per_ktok=round(output_usd_per_mtok * 1000),
        source=source, note=note,
    )


_ANTHROPIC = f"https://platform.claude.com/docs/en/about-claude/pricing ({RATES_READ_ON})"
_GEMINI = f"https://ai.google.dev/gemini-api/docs/pricing ({RATES_READ_ON})"
_OPENAI = f"https://developers.openai.com/api/docs/pricing ({RATES_READ_ON})"

#: The six models `app/config.py` can be configured with. A model that is not
#: here has NO rate -- see the module docstring.
#:
#: Standard, non-batch, non-cached, global-inference rates only. This build
#: makes single synchronous calls with no prompt caching and no batch API, so
#: those are the rates it is actually billed at. Cache reads and writes are not
#: modelled because no provider here reports cache token counts; that is an
#: absence, not an oversight.
DEFAULT_RATES: dict[str, ModelRate] = {
    "claude-opus-5": _rate("claude-opus-5", 5.00, 25.00, _ANTHROPIC),
    "claude-sonnet-5": _rate("claude-sonnet-5", 2.00, 10.00, _ANTHROPIC),
    "gemini-2.5-pro": _rate(
        "gemini-2.5-pro", 1.25, 10.00, _GEMINI,
        note="Prompts over 200k tokens are billed at $2.50/$15.00; not modelled, "
             "so a very long compile is under-costed rather than over-costed."),
    "gemini-3.6-flash": _rate(
        "gemini-3.6-flash", 0.75, 3.75, _GEMINI,
        note="Promotional through 2026-12-31, then $1.50/$7.50. Re-read this page "
             "in January or every Gemini figure halves against the invoice."),
    "gpt-5": _rate("gpt-5", 1.25, 10.00, _OPENAI),
    "gpt-5-mini": _rate("gpt-5-mini", 0.25, 2.00, _OPENAI),
}


def canonical_model(raw: str | None) -> str:
    """Normalise a model name to the key a rate is filed under.

    Vendors answer with dated snapshot aliases -- `claude-sonnet-5-20260214`,
    `gpt-5-2026-03-01` -- and a snapshot costs what the model costs. Without
    this, every real response would be an unpriced model and the whole cost
    column would be NULL.
    """
    import re

    name = (raw or "").strip().lower()
    if not name:
        return ""
    if name in DEFAULT_RATES:
        return name
    # Trailing date suffix: -YYYYMMDD or -YYYY-MM-DD.
    stripped = re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2})$", "", name)
    if stripped in DEFAULT_RATES:
        return stripped
    # A vendor prefix some deployments carry: "anthropic/claude-sonnet-5".
    if "/" in stripped:
        tail = stripped.rsplit("/", 1)[-1]
        if tail in DEFAULT_RATES:
            return tail
    return stripped


def rate_for(db, *, org_id: str, model: str) -> ModelRate | None:
    """The organisation's own rate, else the shipped default, else nothing.

    An org override wins so a tenant on a negotiated price sees their numbers
    rather than ours, and so a model this build has never heard of can still be
    costed by whoever knows what they pay for it.
    """
    from app.models import OrgModelRate

    key = canonical_model(model)
    if not key:
        return None

    row = (
        db.query(OrgModelRate)
        .filter(OrgModelRate.org_id == org_id, OrgModelRate.model == key)
        .one_or_none()
    )
    if row is not None:
        return ModelRate(
            model=key,
            input_micro_usd_per_ktok=row.input_micro_usd_per_ktok,
            output_micro_usd_per_ktok=row.output_micro_usd_per_ktok,
            source=f"org:{row.id}",
            note=row.note or "",
        )
    return DEFAULT_RATES.get(key)


def rate_source(rate: ModelRate | None) -> str | None:
    """A short label recording which table produced a cost, kept on the row.

    So a figure can still be explained a year later, after the defaults have
    moved and the org's override has been edited twice.
    """
    if rate is None:
        return None
    if rate.source.startswith("org:"):
        return rate.source
    return f"default:{RATES_READ_ON}"


def cost_micro_usd(rate: ModelRate | None, *, input_tokens: int,
                   output_tokens: int) -> int | None:
    """What this call cost, in millionths of a dollar, or None if unpriced.

    Integer arithmetic throughout. Rounding is applied once, at the end, so a
    thousand small calls do not accumulate a thousand rounding errors.
    """
    if rate is None:
        return None
    micro = (
        max(0, int(input_tokens or 0)) * rate.input_micro_usd_per_ktok
        + max(0, int(output_tokens or 0)) * rate.output_micro_usd_per_ktok
    )
    return micro // 1000


def usd(micro_usd: int | None) -> float | None:
    """Micro-dollars to dollars, for the API boundary. None stays None."""
    return None if micro_usd is None else micro_usd / 1_000_000.0


def priced_models() -> tuple:
    return tuple(sorted(DEFAULT_RATES))

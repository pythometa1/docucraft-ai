"""The one place source data is allowed to become a prompt.

§16's LLM boundary controls, in the order the doc gives them: data minimisation
("send template context and schema metadata to the model, never send full source
rows"), value redaction, zero retention, residency ("EU, UK and India customer
data pinned to in-region model deployments; residency recorded per
organisation"), and model pinning ("the manifest records the model and version
used during compilation, so a provider model change never silently alters an
approved manifest").

The live problem this closes: `source_ingestion` flattens each spreadsheet row
into `"employee_id: 44182; full_name: Dana Ruiz; annual_salary: 118400"` and
stores it as a chunk, and the chat and draft-generation paths then hand those
chunk texts to the provider verbatim. Every salary and every identifier in a
customer's payroll extract has been reaching a third party in the clear.

The design is a choke point rather than a rule. A rule ("remember to redact")
is a thing to forget at the eleventh call site; a choke point is a function that
raises when handed something it must not send. `prepare_context` is that
function, and `UnredactedValueError` is what it does when the guarantee cannot
be met -- it fails closed, because the alternative failure is silent and
irreversible.

Residency is checked in the same place and for the same reason. An organisation
whose data is pinned to the EU, and a configured deployment that is not in the
EU, is not a request to degrade gracefully: the prompt is never built, and
`ResidencyViolation` says which region was promised and which one the
configuration actually points at. A residency breach discovered after the
request has left the building cannot be taken back.
"""

from dataclasses import dataclass, field

from app.llm.redaction import (
    SENSITIVE_CLASSES, ClassifiedColumn, classify_column, redact_chunk_text, redact_record,
)
from app.tenancy import (  # noqa: F401 -- ResidencyViolation is re-exported for callers
    LLMDataPolicy, ProviderBoundary, ResidencyViolation, configured_provider_boundary,
    enforce_llm_policy,
)


class UnredactedValueError(RuntimeError):
    """Raised when raw source values would have left the process."""


@dataclass(frozen=True)
class SchemaMetadata:
    """What the model is allowed to know about a source column.

    The name and the type, which are what it maps against, plus one synthetic
    sample so it can see the shape. Never a real value.
    """

    column: str
    value_class: str
    sample: object

    def as_prompt_line(self) -> str:
        return f"{self.column} ({self.value_class}) e.g. {self.sample!r}"


@dataclass(frozen=True)
class PromptRecord:
    """What was sent, for whom, and under what model. §16's audit half.

    `model` is pinned onto the manifest at compile time. A provider silently
    upgrading its model behind the same name is the §19 failure this records:
    without it, an approved manifest can start compiling differently and nobody
    can attribute the change.
    """

    org_id: str
    model: str
    schema_columns: tuple
    redacted_columns: tuple
    context_items: int
    #: Where this prompt was actually processed, and whether the deployment that
    #: processed it is contractually zero-retention. §16 asks for residency
    #: "recorded per organisation"; recording it only on the organisation says
    #: what was promised, not what happened, and the two are the same thing only
    #: for as long as nobody edits the provider configuration.
    residency: str = "GLOBAL"
    zero_retention: bool = False

    def as_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "model": self.model,
            "schema_columns": list(self.schema_columns),
            "redacted_columns": list(self.redacted_columns),
            "context_items": self.context_items,
            "residency": self.residency,
            "zero_retention": self.zero_retention,
        }


@dataclass(frozen=True)
class PreparedContext:
    """The only shape allowed past the boundary."""

    schema: tuple = ()
    context_chunks: tuple = ()
    record: PromptRecord | None = None
    template_context: str = ""

    def schema_block(self) -> str:
        return "\n".join(item.as_prompt_line() for item in self.schema)


def describe_schema(columns, sample_rows=()) -> tuple:
    """Turn source columns into the metadata the model may see.

    Samples come from `redact_value`, so a column of salaries is described by a
    plausible salary of the right magnitude rather than by anybody's. Classifying
    from several rows rather than one matters: the first row of a spreadsheet is
    routinely the one with the blank optional fields.
    """
    described = []
    for column in columns:
        values = [row.get(column) for row in sample_rows if isinstance(row, dict)]
        classified: ClassifiedColumn = classify_column(column, values)
        first_real = next((v for v in values if v not in (None, "")), "sample")
        redacted = redact_record({column: first_real})[column]
        described.append(SchemaMetadata(column=column, value_class=classified.value_class, sample=redacted))
    return tuple(described)


def _sensitive_pairs(text: str) -> list[tuple]:
    """(column, value) for each sensitive field in a flattened `"col: value"` chunk.

    Returns the pairs rather than a bool so an error can name them -- a guard
    that says "something was unsafe" gets disabled; one that says
    "annual_salary and national_insurance_no" gets fixed.
    """
    found = []
    for segment in (text or "").split(";"):
        column, sep, value = segment.partition(":")
        if not sep or not value.strip():
            continue
        classified = classify_column(column.strip(), [value.strip()])
        if classified.is_sensitive:
            found.append((classified.name, value.strip()))
    return found


def _surviving_values(original: str, outgoing: str) -> list[str]:
    """Original sensitive values still present in the text about to be sent.

    The check has to be on values, not on column names. A redacted chunk still
    says `annual_salary:` -- that is the schema, and the schema is the part the
    model is meant to see. Re-detecting by column name would fire on every
    successfully redacted chunk and make the guard useless.
    """
    return [
        value for _column, value in _sensitive_pairs(original)
        if value and value in outgoing
    ]


def prepare_context(
    *,
    org_id: str,
    model: str,
    template_context: str = "",
    columns=(),
    sample_rows=(),
    context_chunks=(),
    allow_redaction: bool = True,
    policy: LLMDataPolicy | None = None,
    provider_boundary: ProviderBoundary | None = None,
) -> PreparedContext:
    """Assemble everything a prompt is allowed to contain, and nothing else.

    `columns` and `sample_rows` become schema metadata with synthetic samples.
    `context_chunks` are source-derived texts (the chat and RAG paths); each is
    redacted on the way through, and if redaction is refused and a chunk still
    carries sensitive values, this raises rather than sending it.

    `policy` is what the organisation was promised -- resolve it with
    `tenancy.llm_policy_for(db, org_id)`. Omitting it means no residency or
    retention constraint is on record for this tenant, which is a real state
    (most customers outside the named regions have none) rather than a way to
    skip the check: the check still runs, and an organisation that *has*
    recorded a requirement gets a refusal when the configuration cannot meet it.
    """
    if not org_id or not str(org_id).strip():
        raise UnredactedValueError(
            "prepare_context needs an org_id: a prompt that cannot be attributed to a "
            "tenant cannot be logged, retained or deleted under that tenant's policy."
        )
    if not model or not str(model).strip():
        raise UnredactedValueError(
            "prepare_context needs the model id: §16 pins the model onto the manifest so a "
            "provider changing it cannot silently alter an approved compile."
        )

    # Before anything is assembled. A prompt that has been built and then
    # discarded is a prompt that existed in this process's memory with the
    # tenant's context in it; the cheaper and more honest failure is to never
    # construct it.
    boundary = provider_boundary or configured_provider_boundary()
    enforce_llm_policy(policy or LLMDataPolicy(), boundary)

    schema = describe_schema(columns, sample_rows)

    safe_chunks, redacted_columns = [], []
    for chunk in context_chunks:
        original = chunk if isinstance(chunk, str) else (chunk or {}).get("text", "")
        sensitive = _sensitive_pairs(original)
        text = original
        if sensitive:
            if not allow_redaction:
                raise UnredactedValueError(
                    "refusing to send source values to the model: "
                    f"{sorted({c for c, _ in sensitive})} carry customer data. §16 allows template "
                    "context and schema metadata, never full source rows."
                )
            redacted_columns.extend(column for column, _ in sensitive)
            text = redact_chunk_text(original)

        # Belt to the braces, checked on values rather than column names: whatever
        # the branch above did, no original sensitive value goes out.
        survivors = _surviving_values(original, text)
        if survivors:
            raise UnredactedValueError(
                f"redaction left {len(survivors)} source value(s) in place; refusing to send."
            )

        safe_chunks.append({
            "id": (chunk or {}).get("id") if isinstance(chunk, dict) else None,
            "text": text,
        })

    return PreparedContext(
        schema=schema,
        context_chunks=tuple(safe_chunks),
        template_context=template_context,
        record=PromptRecord(
            org_id=org_id,
            model=model,
            schema_columns=tuple(item.column for item in schema),
            redacted_columns=tuple(sorted(set(redacted_columns))),
            context_items=len(safe_chunks),
            residency=boundary.residency,
            zero_retention=boundary.zero_retention,
        ),
    )

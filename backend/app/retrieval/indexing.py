"""What actually gets written into the embeddings table, and what never does.

§10 gives two lists, and the second one is the important half:

    Embed                                    Do not embed merely for mapping
    template field context and meaning       exact salary values
    conditional instruction text             employee IDs
    section/paragraph semantics              dates that are already typed fields
    source column name + description         boolean or enum values already explicit
      + type + samples                       every row of a clean structured file
    approved historical mappings             coordinates as semantic vectors
    organization/domain terminology

The distinction is the whole design. A vector is a copy of its input that
survives in a different shape, so embedding a spreadsheet row makes the vector
store a second, unindexed copy of the payroll -- one that a deletion has to
find, and that the tenant filter is the only thing standing between. Embedding
the *column* instead ("annual_salary, a six-figure number, e.g. 431907") gives
the compiler everything it needs to map `<Annual Salary>` and carries nobody's
salary.

So the samples in a column description come from `app.llm.redaction`, the same
type-preserving synthetic values the model boundary uses. There is one rule
here and it is enforced rather than documented: nothing this module writes
contains a value read from a customer's file.
"""

from app.llm.redaction import classify_column, redact_value
from app.retrieval.vector import VectorIndex

#: `Embedding.kind` values. Two things are indexed, and they are asymmetric on
#: purpose: the source side describes columns, the template side describes the
#: sentence a field sits in. §13's `sentence_context_match` signal is the reason
#: the second exists at all.
SOURCE_COLUMN = "source_column_description"
TEMPLATE_FIELD = "template_field_context"

#: How many rows are read to characterise a column. Enough to get past the blank
#: optional fields that cluster in the first row of a real export, few enough
#: that a 50,000-row workbook still indexes in one pass.
SAMPLE_ROWS = 25


def describe_column(column: str, values=()) -> str:
    """The sentence that stands in for a column in the index.

    Name, type and one synthetic sample. The name is the strongest signal and is
    a customer's own vocabulary, which is exactly what has to be searchable; the
    sample is what lets "six-figure number" be distinguishable from "four-digit
    reference" without either being anybody's.
    """
    classified = classify_column(column, values)
    real = next((v for v in values if v not in (None, "")), None)
    if real is None:
        return f"{column} ({classified.value_class}), no value present in the sample rows"
    sample = redact_value(real, classified.value_class, column=column)
    return f"{column} ({classified.value_class}), for example {sample}"


def index_source_columns(index: VectorIndex, *, org_id: str, source_version_id: str,
                         columns, records=(), doc_type: str | None = None) -> list:
    """Index one upload's columns. Never its rows.

    `records` are read only to classify and to draw a sample from; no value out
    of them is written. The record id is the column, not the row, so a source
    file of 50,000 employees contributes as many vectors as it has columns --
    which is the difference between a semantic memory and a second copy of the
    payroll.
    """
    if not org_id:
        raise ValueError("indexing source columns needs an org_id; an untenanted vector is unreachable and undeletable")

    sample = [r for r in list(records)[:SAMPLE_ROWS] if isinstance(r, dict)]
    written = []
    for column in columns:
        if not column or str(column).startswith("_"):
            continue  # `_row_index` and friends are bookkeeping, not schema
        values = [row.get(column) for row in sample]
        written.append(index.index_text(
            record_id=f"{source_version_id}:{column}",
            org_id=org_id,
            kind=SOURCE_COLUMN,
            text=describe_column(str(column), values),
            doc_type=doc_type,
            metadata={"source_version_id": source_version_id, "column": str(column)},
        ))
    return written


def index_manifest_fields(index: VectorIndex, *, org_id: str, manifest_id: str,
                          fields, doc_type: str | None = None) -> list:
    """Index the context a template field sits in.

    §10 embeds "template field context and business meaning", which is the
    sentence around the placeholder rather than the placeholder itself. "You
    will report to <New Reporting To>" is what makes `new_manager_name` findable;
    the token on its own is a string of two words that half the estate shares.
    """
    if not org_id:
        raise ValueError("indexing manifest fields needs an org_id")

    written = []
    for field in fields or []:
        field_id = field.get("id")
        if not field_id:
            continue
        # `compiled_from` is the instruction or sentence the compiler read; the
        # id is the fallback when a field came from a bare placeholder.
        context = (field.get("compiled_from") or field.get("source_hint") or "").strip()
        text = f"{field_id}: {context}" if context else str(field_id)
        written.append(index.index_text(
            record_id=f"{manifest_id}:{field_id}",
            org_id=org_id,
            kind=TEMPLATE_FIELD,
            text=text,
            doc_type=doc_type,
            metadata={"manifest_id": manifest_id, "field_id": field_id},
        ))
    return written

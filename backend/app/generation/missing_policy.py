"""What happens when a source value a template needs is not there.

The architecture record calls this the most expensive bug class in document
automation: a silently blank field in a legally binding letter costs more than
an outright failure, because nobody notices until the employee does. Before
this module the fill engine wrote `""` for every unresolved field -- including
a required one -- and reported the document clean.

So the behaviour is declared per field on the manifest and enforced by the
renderer, never inferred and never defaulted to "leave it empty". A field that
does not say what it wants gets BLOCK when it is marked required and BLANK when
it is not, which is the conservative reading of a manifest compiled before this
policy existed.

The three states the doc separates, and which one each maps to:

  absent from the record   -> schema violation      -> undecided, apply policy
  present, value null      -> known unknown         -> undecided, apply policy
  present, empty string    -> legitimately blank    -> a real value, render it
  present, sentinel text   -> dirty source data     -> normalised on the way in
"""

# Sentinels a human types into a spreadsheet to mean "nothing here". They are
# normalised once, during source parsing, so the rest of the pipeline never has
# to guess whether "N/A" is a value or the absence of one. Doing this at render
# time instead would mean a template that legitimately prints "N/A" could not.
SOURCE_SENTINELS = frozenset({"n/a", "na", "-", "--", "null", "none", "nil", "#n/a"})

BLOCK = "BLOCK"
BLANK = "BLANK"
DEFAULT = "DEFAULT"
REMOVE_SENTENCE = "REMOVE_SENTENCE"

ON_MISSING_VALUES = (BLOCK, BLANK, DEFAULT, REMOVE_SENTENCE)

ON_MISSING_HELP = {
    BLOCK: "Block the document. The letter is not issued without this value.",
    BLANK: "Render nothing in its place and carry on.",
    DEFAULT: "Render the field's declared `default` value.",
    REMOVE_SENTENCE: "Delete the sentence that contains the placeholder.",
}


def normalise_source_value(value):
    """Fold a dirty-source sentinel down to None, once, at the boundary.

    Returns the value unchanged when it carries meaning. An empty string is
    returned as-is: "" is a legitimately blank field, which is a different thing
    from a missing one, and collapsing the two here would re-create exactly the
    ambiguity this module exists to remove.
    """
    if isinstance(value, str) and value.strip().casefold() in SOURCE_SENTINELS:
        return None
    return value


def field_on_missing(field: dict) -> str:
    """The declared policy for one manifest field, or the conservative default.

    An unrecognised policy string is treated as BLOCK rather than ignored. A
    typo in a manifest should stop a batch, not silently downgrade a required
    field to "leave it empty" -- `manifest_validation` rejects it at lock time,
    and this is the belt to that braces for manifests locked earlier.
    """
    declared = str(field.get("on_missing") or "").strip().upper()
    if declared in ON_MISSING_VALUES:
        return declared
    if declared:
        return BLOCK
    return BLOCK if field.get("required") else BLANK


def is_missing(value) -> bool:
    """Whether a resolved value counts as missing. `""` deliberately does not."""
    return value is None

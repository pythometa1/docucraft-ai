"""Org-scoped named number sequences: INV-0001, and whatever comes next.

A gap or a duplicate in an invoice numbering is a finding in a tax audit, so
allocation is race-safe rather than probably-fine: on PostgreSQL the sequence
row is locked (`SELECT ... FOR UPDATE`) for the length of the caller's
transaction, so two concurrent generations serialise on the row and cannot
hand out the same number. SQLite has a single writer, which makes the plain
read-increment path equivalent there.

Allocation does NOT commit. The caller does, after the thing being numbered
exists -- so a generation that fails after allocating rolls the increment back
with everything else, and the number is never burned on a document that was
never made.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import NumberSequence

#: The one sequence this vertical uses today. A later vertical adds its own key
#: and its own default prefix here rather than inventing a second allocator.
INVOICE_KEY = "invoice"

DEFAULT_PREFIXES = {INVOICE_KEY: "INV-"}


def allocate(db: Session, org_id: str, key: str = INVOICE_KEY) -> str:
    """The next formatted number for this org's sequence, e.g. `INV-0042`.

    Creates the sequence on first use with the key's default prefix. The
    increment lives in the caller's open transaction; see the module docstring
    for why that is the point rather than an accident.
    """
    stmt = select(NumberSequence).where(
        NumberSequence.org_id == org_id, NumberSequence.key == key)
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    sequence = db.scalar(stmt)
    if sequence is None:
        sequence = NumberSequence(
            org_id=org_id, key=key,
            prefix=DEFAULT_PREFIXES.get(key, f"{key.upper()}-"),
            padding=4, next_value=1)
        db.add(sequence)
        db.flush()
        # Re-read under the lock: a concurrent first allocation may have
        # created the row between our miss and our insert, in which case the
        # unique constraint on (org_id, key) has already made one of us lose --
        # loudly, which is the correct outcome for a numbering race.
        if db.get_bind().dialect.name == "postgresql":
            sequence = db.scalar(stmt)

    value = sequence.next_value
    sequence.next_value = value + 1
    db.flush()
    return f"{sequence.prefix}{value:0{max(0, sequence.padding)}d}"

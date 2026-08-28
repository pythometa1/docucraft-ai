"""merge the revisions that branched off 9f3a6c1d8b25

Template families, instrumentation logs and the semantic-memory tables were each
written against the same parent, so each became a head. Alembic refuses to
resolve `head` while more than one exists, which takes every environment down --
including the test suite, which builds its schema by running the chain.

This revision is the point where the chain is a single line again. It has no
operations of its own by design: a merge that also changes the schema is a merge
nobody can revert cleanly.

Revision ID: f1a0c6d24e7b
Revises: c7b1e4a9d206, c7f4d9a1e8b3, b2f47c9e1a63
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'f1a0c6d24e7b'
down_revision: Union[str, Sequence[str], None] = ('c7b1e4a9d206', 'c7f4d9a1e8b3', 'b2f47c9e1a63')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

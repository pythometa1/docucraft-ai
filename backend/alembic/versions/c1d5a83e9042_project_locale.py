"""How a project writes its dates and amounts.

`region` cannot answer this. The vocabulary is continental -- "Europe", "Asia
Pacific" -- and none of those map to one locale, which is why `REGION_LOCALES`
is empty. So every document generated so far was formatted `en_US` whatever the
project said, and nothing recorded that a decision had been made.

Nullable on purpose. Backfilling a guess is what produced "$82.000,00" from a
project tagged "Europe"; a project that has not said keeps the documented
default, and now says so in each document's lineage.

Revision ID: c1d5a83e9042
Revises: a4c9e1f70b58
"""

import sqlalchemy as sa
from alembic import op

revision = "c1d5a83e9042"
down_revision = "a4c9e1f70b58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("locale", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "locale")

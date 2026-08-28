"""template families, and the column that lets a manifest name the one it came from

§11 is the scaling lever: "Thousands of files should not imply thousands of
independent manual mappings." The unit it scales in is the template family, and
this build had nowhere to keep one. `template_clusters` records what a single
bulk-onboarding run happened to group together; it does not keep the structural
fingerprint, so a template uploaded on its own a month later cannot be compared
against anything and is compiled from scratch.

§18 prices that: ~15-40 model calls for a template with no family match against
~2-6 with a strong one, and "family reuse is the primary cost lever, not model
selection". A missing fingerprint column is therefore a line item, not a schema
nicety.

`template_manifests.template_family_id` is the other half. §6's envelope has
carried the field since it was written; the row had no column for it, so
`envelope_from_row` took it as a parameter and every caller that did not happen
to know it recorded None. An inherited manifest has to be able to say which
family's approved work it was built from, and survive a restart saying it.

Nothing is backfilled. The obvious move -- turn each existing `template_clusters`
row into a family -- would have to invent the one column that matters, because
no fingerprint was ever computed for those rows. A family whose fingerprint is a
guess is worse than no family at all: it would sit in the match set answering
questions about templates it has never seen. Existing clusters keep working as
clusters, and families are minted with a real fingerprint as templates are
onboarded.

Revision ID: c7b1e4a9d206
Revises: 9f3a6c1d8b25
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7b1e4a9d206'
down_revision: Union[str, None] = '9f3a6c1d8b25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'template_families',
        sa.Column('id', sa.String(), nullable=False),
        # NOT NULL from the start rather than nullable-then-backfilled: there is
        # no existing data to migrate, and a nullable tenant on the table that
        # decides which manifests inherit into which templates is exactly the
        # hole §19 calls "potentially existential".
        sa.Column('org_id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        # JSON on both dialects: SQLAlchemy maps this to TEXT on SQLite and JSON
        # on Postgres, so the suite and CI's Postgres 16 run agree without a
        # dialect branch here.
        sa.Column('fingerprint', sa.JSON(), nullable=False, server_default='{}'),
        # A family exists to lend an approved manifest to its members, and a
        # manifest belongs to a template *version*. Nullable would mean a family
        # that matches templates and has nothing to give them.
        sa.Column('representative_template_version_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_template_families_org_id', 'template_families', ['org_id'])

    op.add_column(
        'template_manifests',
        sa.Column('template_family_id', sa.String(), nullable=True),
    )
    op.create_index(
        'ix_template_manifests_template_family_id', 'template_manifests', ['template_family_id']
    )


def downgrade() -> None:
    op.drop_index('ix_template_manifests_template_family_id', table_name='template_manifests')
    op.drop_column('template_manifests', 'template_family_id')
    op.drop_index('ix_template_families_org_id', table_name='template_families')
    op.drop_table('template_families')

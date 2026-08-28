"""per-tenant retention and residency, and the receipt that a deletion happened

§16 "Retention and deletion" asks for three things this schema could not hold.
A per-tenant schedule for source uploads, which are "the most sensitive artefact
and the least useful to retain". A retention period for generated documents set
"according to the customer's records policy, not a default of your choosing" --
which means the column has to be able to say *unset*, because a NOT NULL default
would be exactly the default of our choosing the doc rules out. And "a tenant
offboarding routine that produces a verifiable deletion certificate", which
needs somewhere to put the certificate.

`org_data_policies` also carries the §16 LLM boundary row that had nowhere to
live: residency ("EU, UK and India customer data pinned to in-region model
deployments; residency recorded per organisation") and the zero-retention flag.
Recorded per organisation is the operative phrase -- a residency requirement
that lives in a provider console cannot be enforced at the prompt boundary.

Revision ID: d3f81a5c47b9
Revises: f1a0c6d24e7b
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3f81a5c47b9'
down_revision: Union[str, None] = 'f1a0c6d24e7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'org_data_policies',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        # A platform default rather than NULL: "no policy recorded" must not
        # read as "keep the payroll extract forever".
        sa.Column('source_retention_days', sa.Integer(), nullable=False, server_default='30'),
        # Nullable on purpose. NULL is "the customer has not told us", and the
        # sweep deletes nothing while it stays that way.
        sa.Column('generated_document_retention_days', sa.Integer(), nullable=True),
        sa.Column('residency', sa.String(), nullable=False, server_default='GLOBAL'),
        sa.Column('zero_retention_required', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # Unique, not merely indexed: two policies for one tenant means two answers
    # to "when do we delete their contracts", and whichever the query happened
    # to read would be the one that ran.
    op.create_index('ix_org_data_policies_org_id', 'org_data_policies', ['org_id'], unique=True)

    op.create_table(
        'deletion_certificates',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        # The row outlives the organisation it names, so it cannot rely on a
        # join to say whose data it accounts for.
        sa.Column('org_name', sa.String(), nullable=True),
        sa.Column('scope', sa.String(), nullable=False),
        sa.Column('scope_id', sa.String(), nullable=True),
        sa.Column('counts', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('blobs_deleted', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('derived_forgotten', sa.JSON(), nullable=False, server_default='{}'),
        # The hash is the verifiable part. The manifest of deleted ids is handed
        # back to the caller and never stored -- keeping a list of everything we
        # destroyed would be a smaller copy of the thing we said we destroyed.
        sa.Column('manifest_sha256', sa.String(), nullable=False),
        sa.Column('issued_by', sa.String(), nullable=True),
        sa.Column('issued_by_email', sa.String(), nullable=True),
        sa.Column('issued_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_deletion_certificates_org_id', 'deletion_certificates', ['org_id'])


def downgrade() -> None:
    op.drop_index('ix_deletion_certificates_org_id', table_name='deletion_certificates')
    op.drop_table('deletion_certificates')
    op.drop_index('ix_org_data_policies_org_id', table_name='org_data_policies')
    op.drop_table('org_data_policies')

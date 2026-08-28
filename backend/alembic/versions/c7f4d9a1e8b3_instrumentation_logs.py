"""the §22 logs: mapping suggestions, QA failures and operation timings

§22: "Log every mapping suggestion with its score, its evidence and the
reviewer's decision. This is the only dataset that can ever calibrate the
weights in §13, and it cannot be reconstructed later." The same paragraph asks
for every QA failure with the check that fired and whether a human overrode it.
§18 adds the other half: every service level objective in that table is an
unmeasured design target, and stays one until something records how long the
operations actually take.

None of the three existed. This migration creates them.

`suggestion_logs` carries a unique index on (org_id, manifest_id,
source_version_id, object_id) as a backstop under the writer's own dedupe:
re-opening the binding screen restates the same suggestion, and a reviewer who
looked twice must not appear in a calibration fit twice. It is a backstop
rather than the whole guarantee because SQL treats NULLs as distinct, so a
suggestion recorded before any source file exists is deduped by the writer
alone.

Revision ID: c7f4d9a1e8b3
Revises: 9f3a6c1d8b25
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7f4d9a1e8b3'
down_revision: Union[str, None] = '9f3a6c1d8b25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'suggestion_logs',
        sa.Column('id', sa.String(), nullable=False),
        # §16: org_id NOT NULL on every table that can hold customer data, and a
        # column name lifted out of a customer's spreadsheet is customer data.
        sa.Column('org_id', sa.String(), nullable=False),
        sa.Column('manifest_id', sa.String(), nullable=False),
        sa.Column('source_version_id', sa.String(), nullable=True),
        sa.Column('object_id', sa.String(), nullable=False),
        sa.Column('suggested_column', sa.String(), nullable=True),
        sa.Column('method', sa.String(), nullable=False, server_default='unmatched'),
        sa.Column('score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('band', sa.String(), nullable=False),
        sa.Column('vetoes', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('evidence', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('weights_calibrated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('reviewer_decision', sa.String(), nullable=False, server_default='pending'),
        sa.Column('final_column', sa.String(), nullable=True),
        sa.Column('decided_by', sa.String(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_suggestion_logs_org_id', 'suggestion_logs', ['org_id'])
    op.create_index('ix_suggestion_logs_manifest_id', 'suggestion_logs', ['manifest_id'])
    op.create_index('ix_suggestion_logs_source_version_id', 'suggestion_logs', ['source_version_id'])
    op.create_index('ix_suggestion_logs_reviewer_decision', 'suggestion_logs', ['reviewer_decision'])
    op.create_index(
        'uq_suggestion_logs_object',
        'suggestion_logs',
        ['org_id', 'manifest_id', 'source_version_id', 'object_id'],
        unique=True,
    )

    op.create_table(
        'qa_failure_logs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        sa.Column('manifest_id', sa.String(), nullable=True),
        sa.Column('generation_id', sa.String(), nullable=True),
        sa.Column('document_version_id', sa.String(), nullable=True),
        sa.Column('check_name', sa.String(), nullable=False),
        sa.Column('severity', sa.String(), nullable=False, server_default='blocking'),
        sa.Column('object_id', sa.String(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''),
        sa.Column('phase', sa.String(), nullable=False, server_default='pre_approval'),
        sa.Column('overridden', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('overridden_by', sa.String(), nullable=True),
        sa.Column('overridden_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qa_failure_logs_org_id', 'qa_failure_logs', ['org_id'])
    op.create_index('ix_qa_failure_logs_manifest_id', 'qa_failure_logs', ['manifest_id'])
    op.create_index('ix_qa_failure_logs_document_version_id', 'qa_failure_logs', ['document_version_id'])
    op.create_index('ix_qa_failure_logs_check_name', 'qa_failure_logs', ['check_name'])
    op.create_index('ix_qa_failure_logs_phase', 'qa_failure_logs', ['phase'])

    op.create_table(
        'operation_timings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('org_id', sa.String(), nullable=False),
        sa.Column('operation', sa.String(), nullable=False),
        sa.Column('duration_ms', sa.Float(), nullable=False),
        sa.Column('unit_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('outcome', sa.String(), nullable=False, server_default='ok'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_operation_timings_org_id', 'operation_timings', ['org_id'])
    op.create_index('ix_operation_timings_operation', 'operation_timings', ['operation'])
    op.create_index('ix_operation_timings_created_at', 'operation_timings', ['created_at'])


def downgrade() -> None:
    # Dropping these destroys the only copy of the calibration corpus -- §22's
    # "cannot be reconstructed later" is a statement about this downgrade as
    # much as about not writing the rows in the first place.
    op.drop_index('ix_operation_timings_created_at', table_name='operation_timings')
    op.drop_index('ix_operation_timings_operation', table_name='operation_timings')
    op.drop_index('ix_operation_timings_org_id', table_name='operation_timings')
    op.drop_table('operation_timings')

    op.drop_index('ix_qa_failure_logs_phase', table_name='qa_failure_logs')
    op.drop_index('ix_qa_failure_logs_check_name', table_name='qa_failure_logs')
    op.drop_index('ix_qa_failure_logs_document_version_id', table_name='qa_failure_logs')
    op.drop_index('ix_qa_failure_logs_manifest_id', table_name='qa_failure_logs')
    op.drop_index('ix_qa_failure_logs_org_id', table_name='qa_failure_logs')
    op.drop_table('qa_failure_logs')

    op.drop_index('uq_suggestion_logs_object', table_name='suggestion_logs')
    op.drop_index('ix_suggestion_logs_reviewer_decision', table_name='suggestion_logs')
    op.drop_index('ix_suggestion_logs_source_version_id', table_name='suggestion_logs')
    op.drop_index('ix_suggestion_logs_manifest_id', table_name='suggestion_logs')
    op.drop_index('ix_suggestion_logs_org_id', table_name='suggestion_logs')
    op.drop_table('suggestion_logs')

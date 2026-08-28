"""record which renderer produced each document version

Serves two requirements at once: renderer version in the audit lineage, so a
regression can be attributed to a specific renderer release; and the guard that
stops the editor rebuilding a template-filled .docx out of HTML.

The backfill matters more than the column. Existing rows are classified by how
they were actually produced -- a document whose draft names a template version
went through `assemble_from_docx_template` and must never be HTML-saved, and one
generated from a manifest went through the fill engine. Anything that cannot be
classified is left NULL, which `is_html_editable` treats as not editable: a
legacy HTML document loses in-app editing until it is regenerated, which is the
recoverable direction of that trade.

Revision ID: 7c1f4a9e2b31
Revises: 860cde4d3161
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c1f4a9e2b31'
down_revision: Union[str, None] = '860cde4d3161'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The compiler's own warnings, and the record of a human answering them.
    # Both were computed and discarded, which is why approval could not insist
    # on either.
    op.add_column('template_manifests', sa.Column('warnings', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('template_manifests', sa.Column('warning_dispositions', sa.JSON(), nullable=False, server_default='{}'))

    # The §6 envelope. A locked manifest pins an exact (template_hash,
    # manifest_hash) pair, which is what lets any document be reproduced from
    # that pair plus its source row -- the difference between an audit trail
    # that can be defended and one that only records which id ran.
    op.add_column('template_manifests', sa.Column('template_hash', sa.String(), nullable=True))
    op.add_column('template_manifests', sa.Column('manifest_hash', sa.String(), nullable=True))
    op.add_column('template_manifests', sa.Column('source_schema_ref', sa.String(), nullable=True))
    op.add_column('template_manifests', sa.Column('source_schema_hash', sa.String(), nullable=True))
    op.add_column('template_manifests', sa.Column('expression_lang', sa.String(), nullable=False, server_default='documind-expr/1.0'))
    op.add_column('template_manifests', sa.Column('renderer_contract', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('template_manifests', sa.Column('required_source_fields', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('template_manifests', sa.Column('qa_policy', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('template_manifests', sa.Column('supersedes', sa.String(), nullable=True))
    op.add_column('template_manifests', sa.Column('compiled_model', sa.String(), nullable=True))

    op.add_column('document_versions', sa.Column('renderer', sa.String(), nullable=True))

    # Manifest-driven fills: the fill engine's anchored OOXML surgery.
    op.execute(
        """
        UPDATE document_versions
           SET renderer = 'ooxml_fill/1.0'
         WHERE renderer IS NULL
           AND blob_path IS NOT NULL
           AND document_id IN (
               SELECT id FROM generated_documents WHERE draft_id IS NULL
           )
        """
    )

    # Legacy draft path against a Word template: layout came from the template,
    # so an HTML save would destroy it.
    op.execute(
        """
        UPDATE document_versions
           SET renderer = 'docx_template_assembly/1.0'
         WHERE renderer IS NULL
           AND blob_path IS NOT NULL
           AND document_id IN (
               SELECT gd.id
                 FROM generated_documents gd
                 JOIN draft_documents dd ON dd.id = gd.draft_id
                WHERE dd.template_version_id IS NOT NULL
           )
        """
    )

    # Legacy draft path with no template: genuinely built from HTML.
    op.execute(
        """
        UPDATE document_versions
           SET renderer = 'html_assembly/1.0'
         WHERE renderer IS NULL
           AND blob_path IS NOT NULL
           AND document_id IN (
               SELECT gd.id
                 FROM generated_documents gd
                 JOIN draft_documents dd ON dd.id = gd.draft_id
                WHERE dd.template_version_id IS NULL
           )
        """
    )


def downgrade() -> None:
    op.drop_column('document_versions', 'renderer')
    for column in (
        'compiled_model', 'supersedes', 'qa_policy', 'required_source_fields',
        'renderer_contract', 'expression_lang', 'source_schema_hash',
        'source_schema_ref', 'manifest_hash', 'template_hash',
    ):
        op.drop_column('template_manifests', column)
    op.drop_column('template_manifests', 'warning_dispositions')
    op.drop_column('template_manifests', 'warnings')

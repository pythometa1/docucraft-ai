"""store the approved PDF region inventory on the template version

§12's overlay path writes only into "stored dynamic regions / anchors /
bounding boxes". They have to be stored somewhere, and the somewhere has to be
the template version: coordinates recomputed at render time are coordinates
nobody approved, which is the whole difference between an overlay and a guess.

Empty for every DOCX template, which is all of them today. A DOCX is addressed
by run paths and MERGEFIELDs, not by page geometry -- §6's anchor table ranks
bounding boxes as "fixed to the page, immutable PDF templates only".

Revision ID: b6d1f3a90c24
Revises: a4c8e2b15f31
Create Date: 2026-08-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6d1f3a90c24'
down_revision: Union[str, None] = 'a4c8e2b15f31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'template_versions',
        sa.Column('page_regions', sa.JSON(), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('template_versions', 'page_regions')

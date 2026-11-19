"""index job lease/fence and run retry not-before

Additive: lease/fence/attempt columns make knowledge index jobs recoverable
by claim (a crashed worker's lease expires and a new claim bumps the fence);
``next_attempt_at`` holds a transient-retry not-before time on occurrences.

Revision ID: d5e0a7c31b92
Revises: b7c4d2e91f38
Create Date: 2026-11-19 09:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d5e0a7c31b92"
down_revision: Union[str, Sequence[str], None] = "b7c4d2e91f38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_index_jobs") as batch:
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("fence", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("lease_owner", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("automation_runs") as batch:
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("automation_runs") as batch:
        batch.drop_column("next_attempt_at")
    with op.batch_alter_table("knowledge_index_jobs") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
        batch.drop_column("fence")
        batch.drop_column("attempt")

"""batches and resumable uploads

Additive only: creates the batch/upload tables and their indexes. Existing
conversions keep working with a null `batch_id` (no such column is added to
`public_conversions`; the link lives on `public_batch_items.conversion_id`).

Revision ID: c4f1a2b7d910
Revises: a95827baa7d8
Create Date: 2026-11-17 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4f1a2b7d910"
down_revision: Union[str, Sequence[str], None] = "a95827baa7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Creates the batch and upload tables (no destructive change)."""
    op.create_table(
        "public_batches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("client_batch_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "client_batch_key", name="uq_pub_batch_owner_key"),
    )
    op.create_index(
        "ix_pub_batches_owner_created", "public_batches", ["owner_id", "created_at", "id"], unique=False
    )
    op.create_index("ix_pub_batches_status", "public_batches", ["status"], unique=False)

    op.create_table(
        "public_batch_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("client_item_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("upload_id", sa.String(length=64), nullable=True),
        sa.Column("conversion_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("pages_total", sa.Integer(), nullable=True),
        sa.Column("pages_attempted", sa.Integer(), nullable=True),
        sa.Column("pages_completed", sa.Integer(), nullable=True),
        sa.Column("pages_failed", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["public_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "client_item_id", name="uq_pub_item_batch_client"),
        sa.UniqueConstraint("upload_id"),
        sa.UniqueConstraint("conversion_id"),
    )
    op.create_index(
        "ix_pub_items_batch_ordinal", "public_batch_items", ["batch_id", "ordinal", "id"], unique=False
    )
    op.create_index("ix_pub_items_conversion", "public_batch_items", ["conversion_id"], unique=False)
    op.create_index("ix_pub_items_status", "public_batch_items", ["status"], unique=False)

    op.create_table(
        "public_upload_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("expected_bytes", sa.Integer(), nullable=False),
        sa.Column("received_bytes", sa.Integer(), nullable=False),
        sa.Column("part_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["public_batch_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pub_uploads_owner_state",
        "public_upload_sessions",
        ["owner_id", "state", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_pub_uploads_item_state", "public_upload_sessions", ["item_id", "state"], unique=False
    )
    op.create_index(
        "ix_pub_uploads_state_expiry", "public_upload_sessions", ["state", "expires_at"], unique=False
    )

    op.create_table(
        "public_upload_parts",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("upload_id", sa.String(length=64), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["upload_id"], ["public_upload_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upload_id", "part_number", name="uq_pub_part_number"),
    )
    op.create_index(
        "ix_pub_parts_upload_number", "public_upload_parts", ["upload_id", "part_number"], unique=False
    )

    op.create_table(
        "public_batch_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("item_status", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["public_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "sequence", name="uq_pub_batch_event_seq"),
    )
    op.create_index(
        "ix_pub_batch_events_seq", "public_batch_events", ["batch_id", "sequence"], unique=False
    )


def downgrade() -> None:
    """Drops only the tables this revision created."""
    op.drop_index("ix_pub_batch_events_seq", table_name="public_batch_events")
    op.drop_table("public_batch_events")
    op.drop_index("ix_pub_parts_upload_number", table_name="public_upload_parts")
    op.drop_table("public_upload_parts")
    op.drop_index("ix_pub_uploads_state_expiry", table_name="public_upload_sessions")
    op.drop_index("ix_pub_uploads_item_state", table_name="public_upload_sessions")
    op.drop_index("ix_pub_uploads_owner_state", table_name="public_upload_sessions")
    op.drop_table("public_upload_sessions")
    op.drop_index("ix_pub_items_status", table_name="public_batch_items")
    op.drop_index("ix_pub_items_conversion", table_name="public_batch_items")
    op.drop_index("ix_pub_items_batch_ordinal", table_name="public_batch_items")
    op.drop_table("public_batch_items")
    op.drop_index("ix_pub_batches_status", table_name="public_batches")
    op.drop_index("ix_pub_batches_owner_created", table_name="public_batches")
    op.drop_table("public_batches")

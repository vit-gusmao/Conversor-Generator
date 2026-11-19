"""sources and automation

Additive only: creates the source/automation tables from the upgrade plan
(doc 02 §5). Existing conversion data is untouched; all new tables start
empty. Mirrors ``sources/models.py`` and ``automation/models.py`` exactly.

Revision ID: f2a8c61b9e73
Revises: e08fecd3c89f
Create Date: 2026-11-18 22:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f2a8c61b9e73"
down_revision: Union[str, Sequence[str], None] = "e08fecd3c89f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BIG_ID = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    """Creates the sources/automation tables (no destructive change)."""
    op.create_table(
        "source_roots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("runtime_id", sa.String(length=128), nullable=False),
        sa.Column("canonical_path", sa.String(length=1024), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("granted_by", sa.String(length=64), nullable=False),
        sa.Column("grant_channel", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_source_roots_owner_id", "source_roots", ["owner_id"])
    op.create_index(
        "ix_source_roots_owner_active", "source_roots", ["owner_id", "revoked_at"], unique=False
    )

    op.create_table(
        "file_sources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column(
            "root_id",
            sa.String(length=64),
            sa.ForeignKey("source_roots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("recursive", sa.Boolean(), nullable=False),
        sa.Column("include_patterns", sa.JSON(), nullable=True),
        sa.Column("exclude_patterns", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_file_sources_owner_id", "file_sources", ["owner_id"])
    op.create_index(
        "ix_file_sources_owner_enabled", "file_sources", ["owner_id", "enabled"], unique=False
    )
    op.create_index("ix_file_sources_root", "file_sources", ["root_id"], unique=False)

    op.create_table(
        "source_imports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("schedule_run_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("force", sa.Boolean(), nullable=False),
        sa.Column("collection_id", sa.String(length=64), nullable=True),
        sa.Column("total_files", sa.Integer(), nullable=False),
        sa.Column("imported_files", sa.Integer(), nullable=False),
        sa.Column("skipped_files", sa.Integer(), nullable=False),
        sa.Column("failed_files", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_source_import_idem"),
    )
    op.create_index("ix_source_imports_owner_id", "source_imports", ["owner_id"])
    op.create_index("ix_source_imports_source_id", "source_imports", ["source_id"])
    op.create_index("ix_source_imports_status", "source_imports", ["status"])
    op.create_index("ix_source_imports_schedule_run_id", "source_imports", ["schedule_run_id"])
    op.create_index(
        "ix_source_imports_owner_status", "source_imports", ["owner_id", "status"], unique=False
    )
    op.create_index(
        "ix_source_imports_source_created", "source_imports", ["source_id", "created_at"], unique=False
    )

    op.create_table(
        "source_import_items",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column(
            "import_id",
            sa.String(length=64),
            sa.ForeignKey("source_imports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("client_item_id", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", BIG_ID, nullable=False),
        sa.Column("mtime_ns", BIG_ID, nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("snapshot_key", sa.String(length=512), nullable=True),
        sa.Column("upload_id", sa.String(length=64), nullable=True),
        sa.Column("conversion_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "client_item_id", name="uq_source_import_item_client"),
    )
    op.create_index("ix_source_import_items_import_id", "source_import_items", ["import_id"])
    op.create_index("ix_source_import_items_status", "source_import_items", ["status"])
    op.create_index(
        "ix_source_import_items_import_status",
        "source_import_items",
        ["import_id", "status"],
        unique=False,
    )

    op.create_table(
        "source_file_versions",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("options_hash", sa.String(length=64), nullable=False),
        sa.Column("last_success_import_id", sa.String(length=64), nullable=False),
        sa.Column("conversion_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "source_id",
            "relative_path",
            "sha256",
            "options_hash",
            name="uq_source_file_version_identity",
        ),
    )
    op.create_index(
        "ix_source_file_versions_lookup",
        "source_file_versions",
        ["owner_id", "source_id", "relative_path"],
        unique=False,
    )

    op.create_table(
        "source_knowledge_heads",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("collection_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("active_document_id", sa.String(length=64), nullable=False),
        sa.Column("active_run_id", sa.String(length=64), nullable=False),
        sa.Column("index_generation", sa.Integer(), nullable=False),
        sa.Column("source_missing", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "collection_id",
            "source_id",
            "relative_path",
            name="uq_source_knowledge_head_identity",
        ),
    )
    op.create_index(
        "ix_source_knowledge_heads_collection",
        "source_knowledge_heads",
        ["owner_id", "collection_id"],
        unique=False,
    )

    op.create_table(
        "automation_schedules",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("rule_json", sa.JSON(), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("misfire_policy", sa.String(length=32), nullable=False),
        sa.Column("overlap_policy", sa.String(length=32), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("index_collection_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_schedules_owner_id", "automation_schedules", ["owner_id"])
    op.create_index("ix_automation_schedules_source_id", "automation_schedules", ["source_id"])
    op.create_index(
        "ix_automation_schedules_due",
        "automation_schedules",
        ["enabled", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_schedules_owner",
        "automation_schedules",
        ["owner_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "automation_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("schedule_id", sa.String(length=64), nullable=False),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("occurrence_key", sa.String(length=256), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=True),
        sa.Column("import_id", sa.String(length=64), nullable=True),
        sa.Column("index_job_id", sa.String(length=64), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("occurrence_key", name="uq_automation_run_occurrence"),
    )
    op.create_index("ix_automation_runs_state", "automation_runs", ["state"])
    op.create_index("ix_automation_runs_import_id", "automation_runs", ["import_id"])
    op.create_index(
        "ix_automation_runs_state_lease", "automation_runs", ["state", "lease_until"], unique=False
    )
    op.create_index(
        "ix_automation_runs_owner_schedule",
        "automation_runs",
        ["owner_id", "schedule_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "automation_events",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("automation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_automation_event_sequence"),
    )
    op.create_index("ix_automation_events_run_id", "automation_events", ["run_id"])


def downgrade() -> None:
    """Removes only the tables this revision created."""
    op.drop_table("automation_events")
    op.drop_table("automation_runs")
    op.drop_table("automation_schedules")
    op.drop_table("source_knowledge_heads")
    op.drop_table("source_file_versions")
    op.drop_table("source_import_items")
    op.drop_table("source_imports")
    op.drop_table("file_sources")
    op.drop_table("source_roots")

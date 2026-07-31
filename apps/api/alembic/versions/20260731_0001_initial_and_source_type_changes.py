"""initial schema and source type migration

Revision ID: 20260731_0001
Revises:
Create Date: 2026-07-31 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260731_0001"
down_revision = None
branch_labels = None
depends_on = None


source_type_new = postgresql.ENUM("s3", "mysql", "postgresql", "file", "ebs", "rds", name="sourcetype_new")
source_type_old = postgresql.ENUM("s3", "mysql", "postgresql", "efs", "ebs", "rds", "other", name="sourcetype")
run_status = postgresql.ENUM("queued", "running", "success", "failed", "cancelled", name="runstatus")


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()

    op.execute("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='sources') THEN
        IF EXISTS (SELECT 1 FROM sources WHERE source_type IN ('efs', 'other')) THEN
          RAISE EXCEPTION 'Cannot migrate: sources with type efs/other exist';
        END IF;
      END IF;
    END $$;
    """)

    source_type_old.create(bind, checkfirst=True)
    run_status.create(bind, checkfirst=True)

    destinations = sa.Table(
        "destinations",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("bucket", sa.String(length=120), nullable=False),
        sa.Column("region", sa.String(length=80), nullable=False),
        sa.Column("secret_ref", sa.String(length=255), nullable=False),
        sa.Column("encryption", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    sources = sa.Table(
        "sources",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("source_type", source_type_old, nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    bindings = sa.Table(
        "bindings",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("schedule_cron", sa.String(length=80), nullable=False),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["destination_id"], ["destinations.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    backup_runs = sa.Table(
        "backup_runs",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("binding_id", sa.Integer(), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_transferred", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("artifact_ref", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["binding_id"], ["bindings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    destinations.create(bind, checkfirst=True)
    sources.create(bind, checkfirst=True)
    bindings.create(bind, checkfirst=True)
    backup_runs.create(bind, checkfirst=True)

    sa.Index("ix_bindings_source_id", bindings.c.source_id).create(bind=bind, checkfirst=True)
    sa.Index("ix_bindings_destination_id", bindings.c.destination_id).create(bind=bind, checkfirst=True)
    sa.Index("ix_backup_runs_binding_id", backup_runs.c.binding_id).create(bind=bind, checkfirst=True)

    source_type_new.create(bind, checkfirst=True)
    op.execute("ALTER TABLE sources ALTER COLUMN source_type TYPE sourcetype_new USING source_type::text::sourcetype_new")
    source_type_old.drop(bind, checkfirst=True)
    op.execute("ALTER TYPE sourcetype_new RENAME TO sourcetype")


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_backup_runs_binding_id", table_name="backup_runs")
    op.drop_table("backup_runs")
    op.drop_index("ix_bindings_destination_id", table_name="bindings")
    op.drop_index("ix_bindings_source_id", table_name="bindings")
    op.drop_table("bindings")
    op.drop_table("sources")
    op.drop_table("destinations")

    run_status.drop(bind, checkfirst=True)
    source_type = postgresql.ENUM("s3", "mysql", "postgresql", "efs", "ebs", "rds", "other", name="sourcetype")
    source_type.drop(bind, checkfirst=True)

"""backfill missing columns for existing schemas

Revision ID: 20260731_0002
Revises: 20260731_0001
Create Date: 2026-07-31 00:30:00.000000
"""

from alembic import op


revision = "20260731_0002"
down_revision = "20260731_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing deployments may already have tables created by legacy startup code.
    # Add any missing columns expected by current ORM models.
    op.execute(
        """
        ALTER TABLE destinations
        ADD COLUMN IF NOT EXISTS encryption JSON NOT NULL DEFAULT '{}'::json
        """
    )

    op.execute(
        """
        ALTER TABLE bindings
        ADD COLUMN IF NOT EXISTS last_scheduled_at TIMESTAMPTZ NULL
        """
    )

    op.execute(
        """
        ALTER TABLE backup_runs
        ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE backup_runs
        ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE backup_runs
        ADD COLUMN IF NOT EXISTS artifact_ref VARCHAR(255) NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        ALTER TABLE backup_runs
        ADD COLUMN IF NOT EXISTS message TEXT NOT NULL DEFAULT ''
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS ix_bindings_source_id ON bindings (source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_bindings_destination_id ON bindings (destination_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_backup_runs_binding_id ON backup_runs (binding_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_backup_runs_binding_id")
    op.execute("DROP INDEX IF EXISTS ix_bindings_destination_id")
    op.execute("DROP INDEX IF EXISTS ix_bindings_source_id")

    op.execute("ALTER TABLE backup_runs DROP COLUMN IF EXISTS message")
    op.execute("ALTER TABLE backup_runs DROP COLUMN IF EXISTS artifact_ref")
    op.execute("ALTER TABLE backup_runs DROP COLUMN IF EXISTS max_attempts")
    op.execute("ALTER TABLE backup_runs DROP COLUMN IF EXISTS attempts")
    op.execute("ALTER TABLE bindings DROP COLUMN IF EXISTS last_scheduled_at")
    op.execute("ALTER TABLE destinations DROP COLUMN IF EXISTS encryption")

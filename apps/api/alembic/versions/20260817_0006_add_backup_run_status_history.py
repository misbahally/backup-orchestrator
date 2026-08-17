"""add backup_run_status_history table for tracking job status changes

Revision ID: 20260817_0006
Revises: 20260817_0005
Create Date: 2026-08-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260817_0006"
down_revision = "20260817_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_run_status_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("backup_run_id", sa.Integer(), nullable=False),
        sa.Column("old_status", sa.Enum("queued", "running", "success", "failed", "cancelled", name="runstatus", create_type=False), nullable=True),
        sa.Column("new_status", sa.Enum("queued", "running", "success", "failed", "cancelled", name="runstatus", create_type=False), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["backup_run_id"], ["backup_runs.id"], ),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_backup_run_status_history_backup_run_id", "backup_run_status_history", ["backup_run_id"], unique=False)
    op.create_index("ix_backup_run_status_history_changed_at", "backup_run_status_history", ["changed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_backup_run_status_history_changed_at", table_name="backup_run_status_history")
    op.drop_index("ix_backup_run_status_history_backup_run_id", table_name="backup_run_status_history")
    op.drop_table("backup_run_status_history")

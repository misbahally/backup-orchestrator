"""add workers

Revision ID: 20260831_0001
Revises: 20260731_0001
Create Date: 2026-08-31 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "20260831_0001"
down_revision = "20260731_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )

    op.add_column("sources", sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id"), nullable=True))
    op.create_index("ix_sources_worker_id", "sources", ["worker_id"])

    op.add_column("backup_runs", sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id"), nullable=True))
    op.add_column("backup_runs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("backup_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("backup_runs", sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default="false"))
    op.create_index("ix_backup_runs_worker_id", "backup_runs", ["worker_id"])


def downgrade() -> None:
    op.drop_index("ix_backup_runs_worker_id", "backup_runs")
    op.drop_column("backup_runs", "cancel_requested")
    op.drop_column("backup_runs", "last_heartbeat_at")
    op.drop_column("backup_runs", "claimed_at")
    op.drop_column("backup_runs", "worker_id")
    op.drop_index("ix_sources_worker_id", "sources")
    op.drop_column("sources", "worker_id")
    op.drop_table("workers")

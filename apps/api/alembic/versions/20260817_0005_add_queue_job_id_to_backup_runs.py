"""add queue_job_id to backup_runs

Revision ID: 20260817_0005
Revises: 20260731_0004
Create Date: 2026-08-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260817_0005"
down_revision = "20260731_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backup_runs", sa.Column("queue_job_id", sa.String(length=64), nullable=False, server_default=""))
    op.create_index("ix_backup_runs_queue_job_id", "backup_runs", ["queue_job_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_backup_runs_queue_job_id", table_name="backup_runs")
    op.drop_column("backup_runs", "queue_job_id")

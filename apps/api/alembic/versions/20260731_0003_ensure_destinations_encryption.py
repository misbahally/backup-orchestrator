"""ensure destinations.encryption exists on upgraded databases

Revision ID: 20260731_0003
Revises: 20260731_0002
Create Date: 2026-07-31 01:00:00.000000
"""

from alembic import op


revision = "20260731_0003"
down_revision = "20260731_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE destinations
        ADD COLUMN IF NOT EXISTS encryption JSON NOT NULL DEFAULT '{}'::json
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE destinations DROP COLUMN IF EXISTS encryption")

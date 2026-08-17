"""initial schema

Revision ID: 20260731_0001
Revises:
Create Date: 2026-07-31 00:00:00.000000
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260731_0001"
down_revision = None
branch_labels = None
depends_on = None

# Default admin password hash for "admin" (pbkdf2_sha256, 390000 iterations)
_ADMIN_PASSWORD_HASH = (
    "pbkdf2_sha256$390000$a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    "$524949adad3ba01ffceb27aef2b86ee95528c968a9914b745072fff7a2896fb4"
)


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("source_type", sa.Enum("s3", "mysql", "postgresql", "file", "ebs", "rds", name="sourcetype"), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "destinations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("bucket", sa.String(120), nullable=False),
        sa.Column("region", sa.String(80), nullable=False),
        sa.Column("secret_ref", sa.String(255), nullable=False),
        sa.Column("encryption", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("schedule_cron", sa.String(80), nullable=True),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["destination_id"], ["destinations.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bindings_source_id", "bindings", ["source_id"])
    op.create_index("ix_bindings_destination_id", "bindings", ["destination_id"])

    op.create_table(
        "backup_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("binding_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("queued", "running", "success", "failed", "cancelled", name="runstatus"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bytes_transferred", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=True),
        sa.Column("artifact_ref", sa.String(255), nullable=True),
        sa.Column("queue_job_id", sa.String(64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["binding_id"], ["bindings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backup_runs_binding_id", "backup_runs", ["binding_id"])
    op.create_index("ix_backup_runs_queue_job_id", "backup_runs", ["queue_job_id"])

    op.create_table(
        "backup_run_status_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("backup_run_id", sa.Integer(), nullable=False),
        sa.Column("old_status", sa.Enum("queued", "running", "success", "failed", "cancelled", name="runstatus"), nullable=True),
        sa.Column("new_status", sa.Enum("queued", "running", "success", "failed", "cancelled", name="runstatus"), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["backup_run_id"], ["backup_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backup_run_status_history_backup_run_id", "backup_run_status_history", ["backup_run_id"])
    op.create_index("ix_backup_run_status_history_changed_at", "backup_run_status_history", ["changed_at"])

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    # Seed default admin user
    now = datetime.now(timezone.utc)
    op.execute(
        sa.text(
            "INSERT INTO users (username, password_hash, created_at, updated_at) "
            "VALUES (:username, :password_hash, :now, :now)"
        ).bindparams(username="admin", password_hash=_ADMIN_PASSWORD_HASH, now=now)
    )


def downgrade() -> None:
    op.drop_table("user_sessions")
    op.drop_table("users")
    op.drop_index("ix_backup_run_status_history_changed_at", "backup_run_status_history")
    op.drop_index("ix_backup_run_status_history_backup_run_id", "backup_run_status_history")
    op.drop_table("backup_run_status_history")
    op.drop_index("ix_backup_runs_queue_job_id", "backup_runs")
    op.drop_index("ix_backup_runs_binding_id", "backup_runs")
    op.drop_table("backup_runs")
    op.drop_index("ix_bindings_destination_id", "bindings")
    op.drop_index("ix_bindings_source_id", "bindings")
    op.drop_table("bindings")
    op.drop_table("destinations")
    op.drop_table("sources")
    op.execute("DROP TYPE IF EXISTS runstatus")
    op.execute("DROP TYPE IF EXISTS sourcetype")

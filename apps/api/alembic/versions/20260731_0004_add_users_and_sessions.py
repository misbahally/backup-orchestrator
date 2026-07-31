"""add users and user_sessions tables with a seeded admin user

Revision ID: 20260731_0004
Revises: 20260731_0003
Create Date: 2026-07-31 02:00:00.000000
"""

import hashlib
import secrets

from alembic import op
import sqlalchemy as sa


revision = "20260731_0004"
down_revision = "20260731_0003"
branch_labels = None
depends_on = None

PBKDF2_ITERATIONS = 390_000
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    users_table = sa.table(
        "users",
        sa.column("username", sa.String),
        sa.column("password_hash", sa.String),
    )

    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT 1 FROM users WHERE username = :username"), {"username": DEFAULT_ADMIN_USERNAME}).first()
    if existing is None:
        op.execute(
            users_table.insert().values(
                username=DEFAULT_ADMIN_USERNAME,
                password_hash=_hash_password(DEFAULT_ADMIN_PASSWORD),
            )
        )


def downgrade() -> None:
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("users")

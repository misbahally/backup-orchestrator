import os

from orchestrator_core.database import build_engine, build_session_local
from orchestrator_core.models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://backup:backup@postgres:5432/backup_control"
)

engine = build_engine(DATABASE_URL)
SessionLocal = build_session_local(engine)

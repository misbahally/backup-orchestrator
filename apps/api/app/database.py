from orchestrator_core.database import build_engine, build_session_local
from orchestrator_core.models import Base

from .config import settings


engine = build_engine(settings.database_url)
SessionLocal = build_session_local(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

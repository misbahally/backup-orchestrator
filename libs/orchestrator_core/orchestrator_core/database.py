from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def build_engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def build_session_local(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)

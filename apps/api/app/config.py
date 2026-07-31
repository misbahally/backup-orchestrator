from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://backup:backup@postgres:5432/backup_control"
    redis_url: str = "redis://redis:6379/0"
    cors_origins: str = "http://localhost:8080"
    api_keys: str = ""
    expose_docs: bool = True
    max_retries: int = 3
    rq_job_timeout: str = "2h"
    file_source_allowed_roots: str = "/data:/mnt/backups"
    log_level: str = "INFO"


settings = Settings()

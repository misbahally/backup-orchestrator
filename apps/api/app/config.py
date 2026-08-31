from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://backup:backup@postgres:5432/backup_control"
    cors_origins: str = "http://localhost:8080"
    api_keys: str = ""
    expose_docs: bool = True
    max_retries: int = 3
    file_source_allowed_roots: str = "/data:/mnt/backups"
    log_level: str = "INFO"
    worker_registration_token: str = ""
    scheduler_interval_seconds: int = 60
    worker_heartbeat_timeout_seconds: int = 120
    run_heartbeat_timeout_seconds: int = 3600


settings = Settings()

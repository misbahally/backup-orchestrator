import json
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from .models import RunStatus, SourceType


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=255)


class DestinationCreate(BaseModel):
    name: str
    provider: str = "s3-compatible"
    endpoint: str
    bucket: str
    region: str = "us-east-1"
    secret_ref: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    encryption: dict = {}
    is_active: bool = True

    def destination_kwargs(self) -> dict:
        return self.model_dump(exclude={"access_key_id", "secret_access_key", "session_token"})

    @model_validator(mode="before")
    @classmethod
    def coalesce_credentials(cls, data):
        if not isinstance(data, dict):
            return data

        secret_ref = str(data.get("secret_ref") or "").strip()
        access_key_id = str(data.get("access_key_id") or "").strip()
        secret_access_key = str(data.get("secret_access_key") or "").strip()
        session_token = str(data.get("session_token") or "").strip()

        if not secret_ref and (access_key_id or secret_access_key or session_token):
            data["secret_ref"] = json.dumps(
                {
                    "aws_access_key_id": access_key_id,
                    "aws_secret_access_key": secret_access_key,
                    "aws_session_token": session_token,
                },
                separators=(",", ":"),
            )

        for field_name in ("access_key_id", "secret_access_key", "session_token"):
            data.pop(field_name, None)

        return data


class DestinationRead(DestinationCreate):
    id: int

    class Config:
        from_attributes = True


class SourceCreate(BaseModel):
    name: str
    source_type: SourceType
    settings: dict = {}
    is_active: bool = True


class SourceDatabaseScanRequest(BaseModel):
    source_type: SourceType
    settings: dict = {}


class SourceRead(SourceCreate):
    id: int

    class Config:
        from_attributes = True


class BindingCreate(BaseModel):
    source_id: int
    destination_id: int
    schedule_cron: str = "0 2 * * *"
    policy: dict = {}
    is_active: bool = True


class RunCancelRequest(BaseModel):
    run_ids: list[int]


class BindingRead(BindingCreate):
    id: int

    class Config:
        from_attributes = True


class RunRead(BaseModel):
    id: int
    binding_id: int
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    bytes_transferred: int
    message: str
    attempts: int = 0
    max_attempts: int = 0
    artifact_ref: str = ""

    class Config:
        from_attributes = True


class RunStatusHistoryRead(BaseModel):
    id: int
    backup_run_id: int
    old_status: RunStatus | None
    new_status: RunStatus
    changed_at: datetime
    reason: str = ""

    class Config:
        from_attributes = True

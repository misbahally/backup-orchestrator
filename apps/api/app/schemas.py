from datetime import datetime

from pydantic import BaseModel

from .models import RunStatus, SourceType


class DestinationCreate(BaseModel):
    name: str
    provider: str = "s3-compatible"
    endpoint: str
    bucket: str
    region: str = "us-east-1"
    secret_ref: str
    encryption: dict = {}
    is_active: bool = True


class DestinationRead(DestinationCreate):
    id: int

    class Config:
        from_attributes = True


class SourceCreate(BaseModel):
    name: str
    source_type: SourceType
    settings: dict = {}
    is_active: bool = True


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

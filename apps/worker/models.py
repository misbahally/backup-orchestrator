"""Lightweight dataclasses mirroring the API claim response; no ORM dependency."""
from dataclasses import dataclass, field
import enum


class SourceType(str, enum.Enum):
    s3 = "s3"
    mysql = "mysql"
    postgresql = "postgresql"
    file = "file"
    ebs = "ebs"
    rds = "rds"


class RunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class Destination:
    endpoint: str = ""
    bucket: str = ""
    region: str = "us-east-1"
    secret_ref: str = ""
    encryption: dict = field(default_factory=dict)
    # Compatibility shims used by plugins
    name: str = ""
    provider: str = "s3-compatible"
    is_active: bool = True


@dataclass
class Source:
    source_type: SourceType = SourceType.s3
    settings: dict = field(default_factory=dict)
    id: int = 0
    name: str = ""
    is_active: bool = True
    worker_id: int | None = None


@dataclass
class Binding:
    id: int = 0
    policy: dict = field(default_factory=dict)
    source_id: int = 0
    destination_id: int = 0
    schedule_cron: str = ""
    is_active: bool = True


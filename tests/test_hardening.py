import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "apps" / "worker"))

os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / '.pytest_cache' / 'backup_control.sqlite'}")

from app.database import Base as ApiBase
from app.database import engine as api_engine
from app.main import app as api_app
import app.main as api_main
from database import Base as WorkerBase
from database import engine as worker_engine
import tasks
from models import BackupRun, Binding, Destination, RunStatus, Source, SourceType
from plugins.s3_to_s3 import _upload_extra_args, _customer_key_headers
from plugins.db_to_s3 import run_database_dump_to_s3


@pytest.fixture(autouse=True)
def reset_db():
    ApiBase.metadata.drop_all(bind=api_engine)
    WorkerBase.metadata.drop_all(bind=worker_engine)
    ApiBase.metadata.create_all(bind=api_engine)
    WorkerBase.metadata.create_all(bind=worker_engine)
    yield
    ApiBase.metadata.drop_all(bind=api_engine)
    WorkerBase.metadata.drop_all(bind=worker_engine)


class FakeS3Client:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def head_bucket(self, Bucket):
        self.calls.append(("head_bucket", Bucket))
        return {}


def test_validation_endpoints_return_structured_results(monkeypatch):
    monkeypatch.setattr(api_main, "_make_s3_client", lambda region, endpoint, creds: FakeS3Client())

    client = TestClient(api_app)
    response = client.post(
        "/validate/source",
        json={
            "name": "src",
            "source_type": "s3",
            "settings": {"bucket": "demo-bucket"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["message"].startswith("Source bucket")


def test_run_backup_job_updates_status(monkeypatch):
    monkeypatch.setattr(tasks, "run_s3_to_s3", lambda source, destination, binding: {"copied_objects": 1, "skipped_objects": 0, "transferred_bytes": 7})

    db = tasks.SessionLocal()
    try:
        source = Source(name="src", source_type=SourceType.s3, settings={"bucket": "demo"}, is_active=True)
        destination = Destination(name="dest", provider="s3-compatible", endpoint="", bucket="dest", region="us-east-1", secret_ref="", is_active=True)
        binding = Binding(source_id=1, destination_id=1, schedule_cron="0 2 * * *", policy={}, is_active=True)
        db.add(source)
        db.add(destination)
        db.add(binding)
        db.commit()
        db.refresh(source)
        db.refresh(destination)
        db.refresh(binding)

        run = BackupRun(binding_id=binding.id, status=RunStatus.queued, message="Queued")
        db.add(run)
        db.commit()
        db.refresh(run)

        tasks.run_backup_job(run.id)
    finally:
        db.close()

    db = tasks.SessionLocal()
    try:
        refreshed = db.get(BackupRun, run.id)
        assert refreshed is not None
        assert refreshed.status == RunStatus.success
        assert refreshed.bytes_transferred == 7
        assert "Completed" in refreshed.message
    finally:
        db.close()


def test_destination_encryption_is_persisted_by_api():
    client = TestClient(api_app)
    response = client.post(
        "/destinations",
        json={
            "name": "dest",
            "provider": "s3-compatible",
            "endpoint": "https://example.invalid",
            "bucket": "demo-bucket",
            "region": "us-east-1",
            "secret_ref": "env:AWS_SECRET_ACCESS_KEY",
            "encryption": {"mode": "SSE-KMS", "kms_key_id": "kms-key-123"},
        },
    )

    assert response.status_code == 200
    assert response.json()["encryption"] == {"mode": "SSE-KMS", "kms_key_id": "kms-key-123"}


def test_s3_plugin_encryption_headers_are_explicit():
    assert _upload_extra_args({"mode": "SSE-S3"}) == {"ServerSideEncryption": "AES256"}

    headers = _customer_key_headers({"mode": "SSE-C", "customer_key_ref": "env:MY_KEY"})
    assert headers["SSECustomerAlgorithm"] == "AES256"
    assert headers["SSECustomerKey"] == ""


def test_database_dump_plugin_uploads_dump_to_destination(monkeypatch):
    uploaded = {}

    class FakeS3Client:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
            uploaded["bucket"] = bucket
            uploaded["key"] = key
            uploaded["data"] = fileobj.read()

    class FakeCompletedProcess:
        def __init__(self):
            self.returncode = 0

    def fake_run(command, stdout=None, stderr=None, check=False, **kwargs):
        assert command[0].endswith("mysqldump")
        stdout.write(b"CREATE TABLE demo;\n")
        return FakeCompletedProcess()

    monkeypatch.setattr("plugins.db_to_s3._make_s3_client", lambda region, endpoint, creds: FakeS3Client())
    monkeypatch.setattr("plugins.db_to_s3._load_secret", lambda secret_ref: {})
    monkeypatch.setattr("plugins.db_to_s3.subprocess.run", fake_run)

    source = SimpleNamespace(
        settings={
            "engine": "mysql",
            "host": "db.internal",
            "port": 3306,
            "database": "app",
            "username": "backup",
            "password": "secret",
        },
    )
    destination = SimpleNamespace(region="us-east-1", endpoint="", bucket="backups", secret_ref="")
    binding = SimpleNamespace(policy={})

    summary = run_database_dump_to_s3(source, destination, binding)

    assert summary["copied_objects"] == 1
    assert summary["transferred_bytes"] == len(b"CREATE TABLE demo;\n")
    assert uploaded["bucket"] == "backups"
    assert uploaded["key"].endswith("mysql-app.sql")
    assert uploaded["data"] == b"CREATE TABLE demo;\n"

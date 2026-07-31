import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "orchestrator_core"))
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "apps" / "worker"))

os.environ.setdefault("DATABASE_URL", f"sqlite:///{ROOT / '.pytest_cache' / 'backup_control.sqlite'}")
os.environ.setdefault("FILE_SOURCE_ALLOWED_ROOTS", str(ROOT / "tests"))

from app.database import Base as ApiBase
from app.database import engine as api_engine
from app.main import app as api_app
import app.main as api_main
from database import Base as WorkerBase
from database import engine as worker_engine
import scheduler
import tasks
from models import BackupRun, Binding, Destination, RunStatus, Source, SourceType
from plugins.db_to_s3 import run_database_dump_to_s3


@pytest.fixture(autouse=True)
def reset_db():
    ApiBase.metadata.drop_all(bind=api_engine)
    WorkerBase.metadata.drop_all(bind=worker_engine)
    ApiBase.metadata.create_all(bind=api_engine)
    yield
    ApiBase.metadata.drop_all(bind=api_engine)


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


def test_cron_validation_rejects_invalid_expression():
    client = TestClient(api_app)

    src = client.post(
        "/sources",
        json={"name": "src", "source_type": "s3", "settings": {"bucket": "demo"}},
    ).json()
    dst = client.post(
        "/destinations",
        json={"name": "dst", "provider": "s3-compatible", "endpoint": "", "bucket": "demo", "region": "us-east-1", "secret_ref": ""},
    ).json()

    response = client.post(
        "/bindings",
        json={"source_id": src["id"], "destination_id": dst["id"], "schedule_cron": "not-a-cron", "policy": {}, "is_active": True},
    )
    assert response.status_code == 422


def test_legacy_source_types_are_rejected():
    client = TestClient(api_app)
    response = client.post(
        "/sources",
        json={"name": "legacy", "source_type": "efs", "settings": {}},
    )
    assert response.status_code == 422


def test_file_source_allowlist_check():
    client = TestClient(api_app)
    response = client.post(
        "/validate/source",
        json={
            "name": "src-file",
            "source_type": "file",
            "settings": {"root_path": "/etc"},
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_scheduler_should_enqueue_when_cron_due():
    binding = SimpleNamespace(id=1, schedule_cron="* * * * *", last_scheduled_at=None)
    now = scheduler._utcnow()
    assert scheduler._should_enqueue(binding, now, 60) is True


def test_run_backup_job_updates_status(monkeypatch):
    monkeypatch.setattr(tasks, "run_s3_to_s3", lambda source, destination, binding: {"copied_objects": 1, "skipped_objects": 0, "transferred_bytes": 7})

    db = tasks.SessionLocal()
    try:
        source = Source(name="src", source_type=SourceType.s3, settings={"bucket": "demo"}, is_active=True)
        destination = Destination(name="dest", provider="s3-compatible", endpoint="", bucket="dest", region="us-east-1", secret_ref="", is_active=True)
        db.add(source)
        db.add(destination)
        db.commit()
        binding = Binding(source_id=source.id, destination_id=destination.id, schedule_cron="0 2 * * *", policy={}, is_active=True)
        db.add(binding)
        db.commit()

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


def test_database_dump_plugin_uploads_dump_to_destination(monkeypatch):
    uploaded = {}

    class FakeS3UploadClient:
        def upload_fileobj(self, fileobj, bucket, key, **kwargs):
            uploaded["bucket"] = bucket
            uploaded["key"] = key
            uploaded["data"] = fileobj.read()

        def delete_object(self, Bucket, Key):
            return None

    class FakeProcess:
        def __init__(self, stdout_data=b"", stderr_data=b"", returncode=0):
            self.stdout = SimpleNamespace(read=lambda: stdout_data, close=lambda: None)
            self.stderr = SimpleNamespace(read=lambda: stderr_data)
            self._returncode = returncode

        def wait(self):
            return self._returncode

    calls = []

    def fake_popen(command, stdout=None, stderr=None, env=None, stdin=None):
        calls.append(command)
        if command[0] == "gzip":
            return FakeProcess(stdout_data=b"COMPRESSED")
        return FakeProcess(stdout_data=b"CREATE TABLE demo;\n")

    monkeypatch.setattr("plugins.db_to_s3._make_s3_client", lambda region, endpoint, creds: FakeS3UploadClient())
    monkeypatch.setattr("plugins.db_to_s3._load_secret", lambda secret_ref: {})
    monkeypatch.setattr("plugins.db_to_s3.subprocess.Popen", fake_popen)

    source = SimpleNamespace(
        settings={
            "engine": "mysql",
            "host": "db.internal",
            "port": 3306,
            "database": "app",
            "username": "backup",
            "password": "secret",
            "compress": False,
        },
    )
    destination = SimpleNamespace(region="us-east-1", endpoint="", bucket="backups", secret_ref="", encryption={})
    binding = SimpleNamespace(policy={})

    summary = run_database_dump_to_s3(source, destination, binding)

    assert summary["copied_objects"] == 1
    assert uploaded["bucket"] == "backups"
    assert uploaded["key"].endswith("mysql-app.sql")
    assert calls[0][0].endswith("mysqldump")

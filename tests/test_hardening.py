import base64
import hashlib
import json
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
os.environ.setdefault("API_KEYS", "test-key")

TEST_API_HEADERS = {"X-API-Key": "test-key"}

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


def test_destination_credentials_can_be_set_directly_in_api():
    client = TestClient(api_app, headers=TEST_API_HEADERS)
    response = client.post(
        "/destinations",
        json={
            "name": "dst-direct-creds",
            "provider": "s3-compatible",
            "endpoint": "http://localhost:9000",
            "bucket": "demo-bucket",
            "region": "us-east-1",
            "access_key_id": "AKIA123",
            "secret_access_key": "SECRET123",
            "session_token": "TOKEN123",
        },
    )

    assert response.status_code == 200
    stored = json.loads(response.json()["secret_ref"])
    assert stored["aws_access_key_id"] == "AKIA123"
    assert stored["aws_secret_access_key"] == "SECRET123"
    assert stored["aws_session_token"] == "TOKEN123"


def test_destination_validation_accepts_legacy_credential_fields(monkeypatch):
    monkeypatch.setattr(api_main, "_make_s3_client", lambda region, endpoint, creds: FakeS3Client())

    client = TestClient(api_app, headers=TEST_API_HEADERS)
    response = client.post(
        "/validate/destination",
        json={
            "name": "dst-direct-creds-validation",
            "provider": "s3-compatible",
            "endpoint": "http://localhost:9000",
            "bucket": "demo-bucket",
            "region": "us-east-1",
            "access_key_id": "AKIA123",
            "secret_access_key": "SECRET123",
            "session_token": "TOKEN123",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_validation_endpoints_return_structured_results(monkeypatch):
    monkeypatch.setattr(api_main, "_make_s3_client", lambda region, endpoint, creds: FakeS3Client())

    client = TestClient(api_app, headers=TEST_API_HEADERS)
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
    client = TestClient(api_app, headers=TEST_API_HEADERS)

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
    client = TestClient(api_app, headers=TEST_API_HEADERS)
    response = client.post(
        "/sources",
        json={"name": "legacy", "source_type": "efs", "settings": {}},
    )
    assert response.status_code == 422


def test_file_source_allowlist_check():
    client = TestClient(api_app, headers=TEST_API_HEADERS)
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


def test_sse_customer_key_headers_use_raw_32_byte_key():
    from plugins.s3_to_s3 import _customer_key_headers

    raw_key = "a" * 32
    headers = _customer_key_headers({"mode": "SSE-C", "customer_key": raw_key})

    assert headers["SSECustomerAlgorithm"] == "AES256"
    assert headers["SSECustomerKey"] == raw_key.encode("utf-8")
    assert headers["SSECustomerKeyMD5"] == base64.b64encode(hashlib.md5(raw_key.encode("utf-8")).digest()).decode("ascii")


def test_sse_customer_key_accepts_base64_encoded_32_byte_input():
    from plugins.s3_to_s3 import _customer_key_headers

    key_bytes = b"x" * 32
    base64_key = base64.b64encode(key_bytes).decode("ascii")
    headers = _customer_key_headers({"mode": "SSE-C", "customer_key": base64_key})

    assert headers["SSECustomerAlgorithm"] == "AES256"
    assert headers["SSECustomerKey"] == key_bytes
    assert headers["SSECustomerKeyMD5"] == base64.b64encode(hashlib.md5(key_bytes).digest()).decode("ascii")


def test_sse_customer_key_rejects_invalid_aes256_length():
    from plugins.s3_to_s3 import _customer_key_headers

    with pytest.raises(ValueError, match="exactly 32 bytes"):
        _customer_key_headers({"mode": "SSE-C", "customer_key": "too-short"})


def test_sse_aws_secrets_arn_resolves_secret(monkeypatch):
    from plugins.s3_to_s3 import _resolve_sse_customer_key

    class FakeSecretsManagerClient:
        def get_secret_value(self, SecretId):
            assert SecretId == "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-sse-key"
            return {"SecretString": "from-aws-secrets"}

    def fake_boto_client(service_name, **kwargs):
        assert service_name == "secretsmanager"
        assert kwargs["region_name"] == "us-east-1"
        assert "aws_access_key_id" not in kwargs
        assert "aws_secret_access_key" not in kwargs
        assert "aws_session_token" not in kwargs
        return FakeSecretsManagerClient()

    monkeypatch.setattr("plugins.s3_to_s3.boto3.client", fake_boto_client)
    monkeypatch.setattr("plugins.s3_to_s3._load_secret", lambda secret_ref: {"aws_access_key_id": "AKIA", "aws_secret_access_key": "SECRET"})

    resolved = _resolve_sse_customer_key({"mode": "SSE-C", "aws_secrets_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-sse-key"}, "us-east-1", {"aws_access_key_id": "AKIA", "aws_secret_access_key": "SECRET"})

    assert resolved == "from-aws-secrets"


def test_sse_aws_secrets_region_override_is_used(monkeypatch):
    from plugins.s3_to_s3 import _resolve_sse_customer_key

    class FakeSecretsManagerClient:
        def get_secret_value(self, SecretId):
            return {"SecretString": "from-secret-region"}

    def fake_boto_client(service_name, **kwargs):
        assert service_name == "secretsmanager"
        assert kwargs["region_name"] == "eu-west-1"
        return FakeSecretsManagerClient()

    monkeypatch.setattr("plugins.s3_to_s3.boto3.client", fake_boto_client)

    resolved = _resolve_sse_customer_key(
        {"mode": "SSE-C", "aws_secrets_arn": "arn:aws:secretsmanager:eu-west-1:736517612587:secret:wasabi-sync-zgXsKK", "aws_secrets_region": "eu-west-1"},
        "us-east-1",
        {},
    )

    assert resolved == "from-secret-region"


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

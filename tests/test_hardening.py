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
os.environ.setdefault("WORKER_REGISTRATION_TOKEN", "test-reg-token")

TEST_API_HEADERS = {"X-API-Key": "test-key"}

from app.database import Base as ApiBase
from app.database import engine as api_engine
from app.main import app as api_app
import app.main as api_main
import tasks
import plugins.db_to_s3 as db_to_s3
from plugins.db_to_s3 import _selected_databases, run_database_dump_to_s3


@pytest.fixture(autouse=True)
def reset_db():
    ApiBase.metadata.drop_all(bind=api_engine)
    ApiBase.metadata.create_all(bind=api_engine)
    yield
    ApiBase.metadata.drop_all(bind=api_engine)


def _register_worker(client: TestClient, name: str = "test-worker") -> int:
    """Register a worker and return its ID."""
    resp = client.post(
        "/workers/register",
        json={"name": name},
        headers={"X-Worker-Registration-Token": "test-reg-token", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["worker_id"]


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
    class FakeCursor:
        def execute(self, query):
            self.query = query

        def fetchall(self):
            return [("app_db",), ("analytics",)]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            return None

    monkeypatch.setattr(api_main.psycopg2, "connect", lambda **kwargs: FakeConn())

    client = TestClient(api_app, headers=TEST_API_HEADERS)
    response = client.post(
        "/sources/scan-databases",
        json={
            "source_type": "postgresql",
            "settings": {
                "host": "db.local",
                "port": 5432,
                "username": "backup_user",
                "password": "secret",
                "database": "postgres",
                "databases": ["analytics"],
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["databases"] == ["app_db", "analytics"]
    assert payload["selected_databases"] == ["analytics", "postgres"]


def test_selected_databases_normalizes_values():
    assert _selected_databases({"databases": ["db1", " db2 ", "db1", ""], "database": "db3"}) == ["db1", "db2", "db3"]
    assert _selected_databases({"database": "main"}) == ["main"]


def test_cron_validation_rejects_invalid_expression():
    client = TestClient(api_app, headers=TEST_API_HEADERS)
    worker_id = _register_worker(client)

    src = client.post(
        "/sources",
        json={"name": "src", "source_type": "s3", "settings": {"bucket": "demo"}, "worker_id": worker_id},
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


def test_sse_customer_key_rejects_non_base64_input():
    from plugins.s3_to_s3 import _customer_key_headers

    with pytest.raises(ValueError, match="base64-encoded"):
        _customer_key_headers({"mode": "SSE-C", "customer_key": "not_base64$$$"})


def test_sse_customer_key_accepts_base64_encoded_32_byte_input():
    from plugins.s3_to_s3 import _customer_key_headers

    key_bytes = b"x" * 32
    base64_key = base64.b64encode(key_bytes).decode("ascii")
    headers = _customer_key_headers({"mode": "SSE-C", "customer_key": base64_key})

    assert headers["SSECustomerAlgorithm"] == "AES256"
    assert headers["SSECustomerKey"] == base64_key
    assert headers["SSECustomerKeyMD5"] == base64.b64encode(hashlib.md5(key_bytes).digest()).decode("ascii")


def test_sse_customer_key_rejects_invalid_aes256_length():
    from plugins.s3_to_s3 import _customer_key_headers

    short_key = base64.b64encode(b"short").decode("ascii")
    with pytest.raises(ValueError, match="decode to exactly 32 bytes"):
        _customer_key_headers({"mode": "SSE-C", "customer_key": short_key})


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


def test_compressed_copy_skips_head_when_destination_is_missing(monkeypatch, tmp_path):
    from boto3.s3.transfer import TransferConfig
    from plugins.s3_to_s3 import _copy_one_object

    class SourceBody:
        def close(self):
            pass

    class SourceClient:
        def get_object(self, **kwargs):
            return {"Body": SourceBody(), "Metadata": {}, "ContentType": "text/plain"}

    class DestinationClient:
        def upload_file(self, filename, bucket, key, **kwargs):
            assert bucket == "destination"
            assert key == "logs/example.txt"

    compressed_file = tmp_path / "example.txt.gz"
    compressed_file.write_bytes(b"compressed")
    monkeypatch.setattr(
        "plugins.s3_to_s3._gzip_to_tempfile",
        lambda stream, compression_level: (str(compressed_file), 7),
    )
    monkeypatch.setattr(
        "plugins.s3_to_s3._head_object_if_exists",
        lambda *args, **kwargs: pytest.fail("HEAD should not be called for an unlisted destination object"),
    )

    result = _copy_one_object(
        source_key="logs/example.txt",
        source_meta={"size": 7, "last_modified": None},
        source_bucket="source",
        source_prefix="",
        dest_prefix="",
        src_client=SourceClient(),
        dst_client=DestinationClient(),
        source_encryption={},
        destination_encryption={},
        src_region="us-east-1",
        src_creds={},
        destination_bucket="destination",
        destination_objects={},
        transfer_config=TransferConfig(),
        size_only=False,
        exact_timestamps=False,
        compression={
            "enabled": True,
            "algorithm": "gzip",
            "level": 6,
            "min_size_bytes": 0,
            "include_extensions": [".txt"],
            "exclude_extensions": [],
        },
    )

    assert result == {
        "source_key": "logs/example.txt",
        "target_key": "logs/example.txt",
        "copied": 1,
        "skipped": 0,
        "transferred_bytes": 7,
    }


def test_worker_registration_and_token():
    client = TestClient(api_app, headers=TEST_API_HEADERS)
    worker_id = _register_worker(client, "alpha")
    assert worker_id > 0

    # Re-register same name should rotate token (idempotent)
    resp2 = client.post(
        "/workers/register",
        json={"name": "alpha"},
        headers={"X-Worker-Registration-Token": "test-reg-token"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["worker_id"] == worker_id


def test_worker_registration_wrong_token():
    client = TestClient(api_app, headers=TEST_API_HEADERS)
    resp = client.post(
        "/workers/register",
        json={"name": "bad-token-worker"},
        headers={"X-Worker-Registration-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_cancel_queued_run_sets_cancelled_immediately():
    client = TestClient(api_app, headers=TEST_API_HEADERS)
    worker_id = _register_worker(client)

    src = client.post("/sources", json={"name": "src", "source_type": "s3", "settings": {"bucket": "b"}, "worker_id": worker_id}).json()
    dst = client.post("/destinations", json={"name": "dst", "provider": "s3-compatible", "endpoint": "", "bucket": "b", "region": "us-east-1", "secret_ref": ""}).json()
    binding = client.post("/bindings", json={"source_id": src["id"], "destination_id": dst["id"], "schedule_cron": "0 2 * * *", "policy": {}, "is_active": True}).json()

    run = client.post(f"/runs/trigger/{binding['id']}").json()
    assert run["status"] == "queued"

    cancel_resp = client.post(f"/runs/{run['id']}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["result"] == "cancelled"

    refreshed = client.get(f"/runs/{run['id']}").json()
    assert refreshed["status"] == "cancelled"


def test_source_requires_valid_worker():
    client = TestClient(api_app, headers=TEST_API_HEADERS)

    resp = client.post("/sources", json={"name": "src", "source_type": "s3", "settings": {"bucket": "b"}, "worker_id": 9999})
    assert resp.status_code == 404

    resp2 = client.post("/sources", json={"name": "src-no-worker", "source_type": "s3", "settings": {"bucket": "b"}})
    assert resp2.status_code == 400


def test_worker_claim_only_gets_own_sources():
    client = TestClient(api_app, headers=TEST_API_HEADERS)
    w1_id = _register_worker(client, "worker-1")
    w2_id = _register_worker(client, "worker-2")

    # Rotate tokens to get fresh ones
    w1_token = client.post("/workers/register", json={"name": "worker-1"}, headers={"X-Worker-Registration-Token": "test-reg-token"}).json()["token"]
    w2_token = client.post("/workers/register", json={"name": "worker-2"}, headers={"X-Worker-Registration-Token": "test-reg-token"}).json()["token"]

    # Create source pinned to w2
    src = client.post("/sources", json={"name": "src-w2", "source_type": "s3", "settings": {"bucket": "b"}, "worker_id": w2_id}).json()
    dst = client.post("/destinations", json={"name": "dst", "provider": "s3-compatible", "endpoint": "", "bucket": "b", "region": "us-east-1", "secret_ref": ""}).json()
    binding = client.post("/bindings", json={"source_id": src["id"], "destination_id": dst["id"], "schedule_cron": "0 2 * * *", "policy": {}, "is_active": True}).json()

    # Trigger a run
    client.post(f"/runs/trigger/{binding['id']}")

    # Use separate clients (no default API-Key) so worker bearer token is the only auth
    worker_client = TestClient(api_app)
    w1_headers = {"Authorization": f"Bearer {w1_token}"}
    w2_headers = {"Authorization": f"Bearer {w2_token}"}

    # Worker 1 claims nothing (source is pinned to w2)
    claim1 = worker_client.post("/workers/runs/claim", headers=w1_headers)
    assert claim1.status_code == 204

    # Worker 2 claims the run
    claim2 = worker_client.post("/workers/runs/claim", headers=w2_headers)
    assert claim2.status_code == 200
    ctx = claim2.json()
    assert ctx["source_type"] == "s3"
    assert "bucket" in ctx["destination"]


def test_database_dump_plugin_excludes_system_schemas_from_selection():
    source_settings = {
        "database": "mysql",
        "databases": ["information_schema", "app", "performance_schema", "app"],
    }

    assert db_to_s3._selected_databases(source_settings) == ["app"]


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

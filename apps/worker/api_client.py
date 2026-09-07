import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("worker-api-client")


def _default_name() -> str:
    import socket
    return socket.gethostname()


class WorkerApiClient:
    def __init__(self) -> None:
        self.api_url = os.environ.get("API_URL", "http://backup-orchestrator-api:8000").rstrip("/")
        self.registration_token = os.environ.get("WORKER_REGISTRATION_TOKEN", "")
        self.worker_name = (os.environ.get("WORKER_NAME") or "").strip() or _default_name()
        self.state_path = Path(os.environ.get("WORKER_STATE_PATH", "/tmp/worker_state.json"))
        self.token: str = ""
        self.worker_id: int = 0
        self._load_state()

    def _load_state(self) -> None:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self.token = str(data.get("token") or "")
                self.worker_id = int(data.get("worker_id") or 0)
            except Exception:
                pass

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps({"token": self.token, "worker_id": self.worker_id}))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def register(self) -> None:
        if not self.registration_token:
            raise RuntimeError("WORKER_REGISTRATION_TOKEN is not set")
        resp = httpx.post(
            f"{self.api_url}/workers/register",
            json={"name": self.worker_name},
            headers={
                "X-Worker-Registration-Token": self.registration_token,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = str(data["token"])
        self.worker_id = int(data["worker_id"])
        self._save_state()
        logger.info("Registered as worker %r (id=%s)", self.worker_name, self.worker_id)

    def ensure_registered(self) -> None:
        if not self.token or not self.worker_id:
            self.register()
            return
        # Quick validation — heartbeat doubles as a token check
        try:
            resp = httpx.post(f"{self.api_url}/workers/heartbeat", headers=self._headers(), timeout=5)
            if resp.status_code == 401:
                logger.info("Token rejected; re-registering")
                self.register()
        except httpx.TransportError:
            pass  # API not reachable yet; carry on and retry later

    def heartbeat(self) -> bool:
        try:
            resp = httpx.post(f"{self.api_url}/workers/heartbeat", headers=self._headers(), timeout=5)
            if resp.status_code == 401:
                logger.warning("Heartbeat 401 — re-registering")
                self.register()
                return True
            return resp.is_success
        except Exception as exc:
            logger.warning("Heartbeat failed: %s", exc)
            return False

    def claim_run(self) -> dict | None:
        try:
            resp = httpx.post(f"{self.api_url}/workers/runs/claim", headers=self._headers(), timeout=15)
            if resp.status_code == 204:
                return None
            if resp.status_code == 401:
                self.register()
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Claim failed: %s", exc)
            return None

    def report_status(
        self,
        run_id: int,
        status: str,
        message: str = "",
        bytes_transferred: int = 0,
        artifact_ref: str = "",
        retryable: bool = False,
    ) -> dict:
        try:
            resp = httpx.post(
                f"{self.api_url}/workers/runs/{run_id}/status",
                json={
                    "status": status,
                    "message": message,
                    "bytes_transferred": bytes_transferred,
                    "artifact_ref": artifact_ref,
                    "retryable": retryable,
                },
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 401:
                self.register()
                return {"ok": True, "cancel_requested": False}
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("report_status failed for run %s: %s", run_id, exc)
            return {"ok": False, "cancel_requested": False}

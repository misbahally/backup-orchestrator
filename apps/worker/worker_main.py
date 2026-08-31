import logging
import os
import time

from prometheus_client import start_http_server
from orchestrator_core import __version__ as APP_VERSION

from api_client import WorkerApiClient
from tasks import run_backup_job

logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
logger = logging.getLogger("backup-worker")


def main() -> None:
    logger.info("Worker starting (version v%s)", APP_VERSION)
    metrics_port = int(os.environ.get("METRICS_PORT", "9090"))
    start_http_server(metrics_port)

    poll_interval = int(os.environ.get("WORKER_POLL_INTERVAL", "10"))
    client = WorkerApiClient()
    client.ensure_registered()

    while True:
        client.heartbeat()
        context = client.claim_run()
        if context is not None:
            run_backup_job(context, client)
        else:
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()

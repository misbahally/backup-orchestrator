import logging
import os

from prometheus_client import start_http_server
from redis import Redis
from rq import Connection, Queue, Worker
from orchestrator_core import __version__ as APP_VERSION

from scheduler import reconcile_orphaned_runs

logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
logger = logging.getLogger("backup-worker")


def main() -> None:
    logger.info("Worker starting (version v%s)", APP_VERSION)
    metrics_port = int(os.environ.get("METRICS_PORT", "9090"))
    start_http_server(metrics_port)

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    conn = Redis.from_url(redis_url)
    reconcile_orphaned_runs(redis_conn=conn)
    with Connection(conn):
        worker = Worker([Queue("backup-runs")])
        worker.work()


if __name__ == "__main__":
    main()

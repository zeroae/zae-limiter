#!/usr/bin/env python3
"""Sidecar that maintains Lambda workers for Locust stress testing.

This script runs as a non-essential container in the ECS task alongside
the Locust master. It continuously invokes Lambda workers to connect to
the master, replacing them as they timeout (60s Lambda limit).

Uses a closed-loop control system:
- Tracks pending invocations (Lambda invoked but not yet connected)
- Only invokes new workers when connected + pending < desired
- Times out pending invocations after PENDING_TIMEOUT seconds

Environment variables:
    DESIRED_WORKERS: Number of workers to maintain (default: 10)
    WORKER_FUNCTION_NAME: Lambda function name to invoke
    MASTER_PORT: Locust master port (default: 5557)
    POLL_INTERVAL: Seconds between maintenance loops (default: 5)
    PENDING_TIMEOUT: Seconds before pending invocation expires (default: 30)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
import urllib.request

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def get_task_ip() -> str:
    """Get task's private IP from ECS metadata endpoint v4.

    Returns:
        The task's private IPv4 address.

    Raises:
        RuntimeError: If metadata endpoint is not available or IP not found.
    """
    metadata_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not metadata_uri:
        raise RuntimeError("ECS_CONTAINER_METADATA_URI_V4 not set")

    with urllib.request.urlopen(f"{metadata_uri}/task", timeout=5) as resp:
        metadata = json.loads(resp.read())

    for container in metadata.get("Containers", []):
        for network in container.get("Networks", []):
            if network.get("NetworkMode") == "awsvpc":
                ips = network.get("IPv4Addresses", [])
                if ips:
                    return str(ips[0])

    raise RuntimeError("Could not determine task IP from ECS metadata")


def get_connected_workers() -> int:
    """Query Locust master API for connected worker count."""
    try:
        with urllib.request.urlopen("http://localhost:8089/stats/requests", timeout=5) as resp:
            data = json.loads(resp.read())
            # The stats endpoint includes worker count in state info
            count = data.get("worker_count", 0)
            return int(count) if count is not None else 0
    except Exception:
        # Master might not be ready yet, or API format changed
        return -1  # Unknown


class WorkerPool:
    """Tracks pending Lambda invocations for closed-loop control."""

    def __init__(self, pending_timeout: float = 30.0):
        self.pending_timeout = pending_timeout
        self.pending: list[float] = []  # Timestamps of pending invocations

    def add_pending(self, count: int = 1) -> None:
        """Record new pending invocations."""
        now = time.time()
        self.pending.extend([now] * count)

    def expire_old(self) -> int:
        """Remove expired pending invocations. Returns count expired."""
        now = time.time()
        cutoff = now - self.pending_timeout
        original = len(self.pending)
        self.pending = [t for t in self.pending if t > cutoff]
        return original - len(self.pending)

    def connected(self, actual_count: int, previous_count: int) -> None:
        """Adjust pending based on newly connected workers."""
        # If connected count increased, remove that many from pending
        new_connections = max(0, actual_count - previous_count)
        if new_connections > 0 and self.pending:
            # Remove oldest pending entries (FIFO)
            self.pending = self.pending[new_connections:]

    @property
    def pending_count(self) -> int:
        return len(self.pending)


def main() -> None:
    """Run the worker orchestration loop."""
    desired_workers = int(os.environ.get("DESIRED_WORKERS", "10"))
    worker_function = os.environ["WORKER_FUNCTION_NAME"]
    master_port = int(os.environ.get("MASTER_PORT", "5557"))
    poll_interval = int(os.environ.get("POLL_INTERVAL", "5"))
    pending_timeout = float(os.environ.get("PENDING_TIMEOUT", "30"))

    master_ip = get_task_ip()
    logger.info(
        "Orchestrator started: master=%s:%d, desired=%d, pending_timeout=%ds",
        master_ip,
        master_port,
        desired_workers,
        int(pending_timeout),
    )

    lambda_client = boto3.client("lambda")
    payload_config: dict[str, object] = {
        "mode": "worker",
        "master_host": master_ip,
        "master_port": master_port,
    }
    user_classes = os.environ.get("LOCUST_USER_CLASSES")
    if user_classes:
        payload_config["user_classes"] = user_classes
    locustfile = os.environ.get("LOCUSTFILE")
    if locustfile:
        payload_config["locustfile"] = locustfile

    payload = json.dumps({"config": payload_config}).encode()

    pool = WorkerPool(pending_timeout=pending_timeout)
    last_connected = -1
    shutdown = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal shutdown
        logger.info("Received signal %d, initiating shutdown...", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not shutdown:
        try:
            # Query Locust master for actual connected workers
            connected = get_connected_workers()

            # Expire old pending invocations
            expired = pool.expire_old()
            if expired > 0:
                logger.warning("Expired %d pending invocations (failed to connect)", expired)

            # Update pending based on new connections
            if connected >= 0 and last_connected >= 0:
                pool.connected(connected, last_connected)

            # Calculate how many workers we need
            if connected < 0:
                # Master not ready - assume 0 connected, keep pending
                effective = pool.pending_count
                needed = max(0, desired_workers - effective)
                logger.info(
                    "Master not ready: pending=%d, invoking=%d",
                    pool.pending_count,
                    needed,
                )
            else:
                effective = connected + pool.pending_count
                needed = max(0, desired_workers - effective)

                # Log state on every poll for visibility
                logger.info(
                    "Workers: connected=%d, pending=%d, effective=%d/%d, need=%d",
                    connected,
                    pool.pending_count,
                    effective,
                    desired_workers,
                    needed,
                )

            # Invoke new workers if needed
            if needed > 0:
                for _ in range(needed):
                    lambda_client.invoke(
                        FunctionName=worker_function,
                        InvocationType="Event",  # Async invocation
                        Payload=payload,
                    )
                pool.add_pending(needed)
                logger.info("Invoked %d new Lambda workers", needed)

            last_connected = connected
            time.sleep(poll_interval)

        except Exception:
            logger.exception("Error in orchestration loop")
            time.sleep(poll_interval)

    logger.info("Orchestrator shutdown complete")


if __name__ == "__main__":
    main()

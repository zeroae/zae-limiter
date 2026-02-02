#!/usr/bin/env python3
"""Sidecar that maintains Lambda workers for Locust stress testing.

This script runs as a non-essential container in the ECS task alongside
the Locust master. It continuously invokes Lambda workers to connect to
the master, replacing them as they timeout (60s Lambda limit).

Environment variables:
    DESIRED_WORKERS: Number of workers to maintain (default: 10)
    WORKER_FUNCTION_NAME: Lambda function name to invoke
    MASTER_PORT: Locust master port (default: 5557)
    POLL_INTERVAL: Seconds between maintenance loops (default: 5)
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


def main() -> None:
    """Run the worker orchestration loop."""
    desired_workers = int(os.environ.get("DESIRED_WORKERS", "10"))
    worker_function = os.environ["WORKER_FUNCTION_NAME"]
    master_port = int(os.environ.get("MASTER_PORT", "5557"))
    poll_interval = int(os.environ.get("POLL_INTERVAL", "5"))

    master_ip = get_task_ip()
    logger.info(
        "Orchestrator started: master=%s:%d, desired_workers=%d",
        master_ip,
        master_port,
        desired_workers,
    )

    lambda_client = boto3.client("lambda")
    payload = json.dumps(
        {
            "config": {
                "mode": "worker",
                "master_host": master_ip,
                "master_port": master_port,
            }
        }
    ).encode()

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

            if connected < 0:
                # Master not ready, invoke full batch
                needed = desired_workers
            else:
                needed = max(0, desired_workers - connected)

            if needed > 0:
                for _ in range(needed):
                    lambda_client.invoke(
                        FunctionName=worker_function,
                        InvocationType="Event",  # Async invocation
                        Payload=payload,
                    )
                logger.info(
                    "Invoked %d workers (connected=%s, desired=%d)",
                    needed,
                    connected if connected >= 0 else "unknown",
                    desired_workers,
                )

            time.sleep(poll_interval)
        except Exception:
            logger.exception("Error in orchestration loop")
            time.sleep(poll_interval)

    logger.info("Orchestrator shutdown complete")


if __name__ == "__main__":
    main()

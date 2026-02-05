#!/usr/bin/env python3
"""Sidecar that maintains Lambda workers for Locust stress testing.

This script runs as a non-essential container in the ECS task alongside
the Locust master. It continuously invokes Lambda workers to connect to
the master, replacing them as they timeout (60s Lambda limit).

Uses a closed-loop control system with auto-scaling:
- Tracks pending invocations (Lambda invoked but not yet connected)
- Dynamically calculates desired workers based on user count and RPS
- Only invokes new workers when connected + pending < desired
- Times out pending invocations after PENDING_TIMEOUT seconds

Auto-scaling formula:
    desired = max(ceil(users / USERS_PER_WORKER), ceil(rps / RPS_PER_WORKER))
    desired = clamp(desired, MIN_WORKERS, MAX_WORKERS)

Environment variables:
    WORKER_FUNCTION_NAME: Lambda function name to invoke
    MASTER_PORT: Locust master port (default: 5557)
    POLL_INTERVAL: Seconds between maintenance loops (default: 5)
    PENDING_TIMEOUT: Seconds before pending invocation expires (default: 30)

    Auto-scaling (when enabled):
    USERS_PER_WORKER: Max users per Lambda worker (default: 20)
    RPS_PER_WORKER: Max RPS per Lambda worker (default: 50)
    MIN_WORKERS: Minimum workers to maintain (default: 1)
    MAX_WORKERS: Maximum workers cap (default: unlimited)

    Fixed scaling (legacy):
    DESIRED_WORKERS: Fixed number of workers (disables auto-scaling)
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import time
import urllib.request
from dataclasses import dataclass

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


@dataclass
class LocustStats:
    """Current Locust master statistics for auto-scaling decisions."""

    user_count: int
    total_rps: float
    worker_count: int
    state: str  # "ready", "spawning", "running", "stopped"


def get_locust_stats() -> LocustStats | None:
    """Query Locust master API for current stats.

    Returns:
        LocustStats with current metrics, or None if master not ready.
    """
    try:
        with urllib.request.urlopen("http://localhost:8089/stats/requests", timeout=5) as resp:
            data = json.loads(resp.read())
            return LocustStats(
                user_count=int(data.get("user_count", 0) or 0),
                total_rps=float(data.get("total_rps", 0) or 0),
                worker_count=int(data.get("worker_count", 0) or 0),
                state=str(data.get("state", "unknown")),
            )
    except Exception:
        return None


@dataclass
class ScalingConfig:
    """Configuration for auto-scaling Lambda workers."""

    users_per_worker: int = 20
    rps_per_worker: int = 50
    min_workers: int = 1
    max_workers: int | None = None  # None = unlimited
    startup_lead_time: float = 20.0  # Seconds to predict ahead for proactive scaling

    @classmethod
    def from_env(cls) -> ScalingConfig:
        """Create config from environment variables."""
        max_workers_str = os.environ.get("MAX_WORKERS")
        return cls(
            users_per_worker=int(os.environ.get("USERS_PER_WORKER", "20")),
            rps_per_worker=int(os.environ.get("RPS_PER_WORKER", "50")),
            min_workers=int(os.environ.get("MIN_WORKERS", "1")),
            max_workers=int(max_workers_str) if max_workers_str else None,
            startup_lead_time=float(os.environ.get("STARTUP_LEAD_TIME", "20")),
        )


def calculate_desired_workers(
    stats: LocustStats | None,
    config: ScalingConfig,
    prev_stats: LocustStats | None = None,
    time_delta: float = 5.0,
) -> int:
    """Calculate desired worker count based on current load with predictive scaling.

    Formula:
        # Current need
        current = max(ceil(users / users_per_worker), ceil(rps / rps_per_worker))

        # Predict future need (startup_lead_time seconds ahead)
        predicted_users = users + (user_rate * startup_lead_time)
        predicted = max(ceil(predicted_users / users_per_worker), ...)

        # Take the higher to proactively scale
        desired = max(current, predicted)
        result = clamp(desired, min_workers, max_workers)

    When test is not running (state != "running"), maintains min_workers.

    Args:
        stats: Current Locust stats, or None if master not ready.
        config: Scaling configuration parameters.
        prev_stats: Previous stats for rate calculation.
        time_delta: Time between prev_stats and stats in seconds.

    Returns:
        Number of workers to maintain.
    """
    if stats is None or stats.state not in ("spawning", "running"):
        # Master not ready or test not running - maintain minimum
        return config.min_workers

    # Calculate workers needed for current users
    workers_for_users = (
        math.ceil(stats.user_count / config.users_per_worker) if stats.user_count > 0 else 0
    )

    # Calculate workers needed for current RPS
    workers_for_rps = (
        math.ceil(stats.total_rps / config.rps_per_worker) if stats.total_rps > 0 else 0
    )

    # Current desired
    current_desired = max(workers_for_users, workers_for_rps)

    # Predictive scaling: estimate need in startup_lead_time seconds
    predicted_desired = current_desired
    if prev_stats is not None and time_delta > 0 and config.startup_lead_time > 0:
        # Calculate rate of change
        user_rate = (stats.user_count - prev_stats.user_count) / time_delta

        # Only predict for increasing load (don't proactively scale down)
        if user_rate > 0:
            # Predict users in startup_lead_time seconds
            predicted_users = stats.user_count + (user_rate * config.startup_lead_time)
            predicted_workers = math.ceil(predicted_users / config.users_per_worker)
            predicted_desired = max(predicted_desired, predicted_workers)

    # Take the higher of current and predicted
    desired = max(current_desired, predicted_desired)

    # Apply bounds
    desired = max(desired, config.min_workers)
    if config.max_workers is not None:
        desired = min(desired, config.max_workers)

    return desired


class WorkerPool:
    """Tracks pending and active Lambda workers for proactive replacement.

    Key improvement: Instead of waiting for workers to timeout and disconnect,
    we proactively invoke replacements when workers approach their timeout.
    This ensures seamless worker rotation without gaps.
    """

    def __init__(
        self,
        pending_timeout: float = 30.0,
        lambda_timeout: float = 300.0,
        replacement_pct: float = 0.8,
    ):
        self.pending_timeout = pending_timeout
        self.lambda_timeout = lambda_timeout
        self.replacement_pct = replacement_pct  # Start replacement at this % of timeout
        self.pending: list[float] = []  # Timestamps of pending invocations
        self.active: list[float] = []  # Timestamps of active worker invocations

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
        """Adjust pending/active based on newly connected workers.

        Note: We only add to active when workers connect, never remove.
        Workers are removed from active only via mark_replaced() when we
        proactively replace them. This ensures replacement workers' timestamps
        aren't accidentally removed when old workers disconnect.
        """
        # If connected count increased, move that many from pending to active
        new_connections = max(0, actual_count - previous_count)
        if new_connections > 0 and self.pending:
            # Move oldest pending entries to active (FIFO)
            moved = min(new_connections, len(self.pending))
            self.active.extend(self.pending[:moved])
            self.pending = self.pending[moved:]

        # Adopt untracked workers: if more workers are connected than we're
        # tracking, add timestamps for the extras. This handles workers that
        # connected before orchestrator started or after a restart.
        untracked = actual_count - len(self.active) - len(self.pending)
        if untracked > 0:
            # Use current time as conservative estimate (may trigger early replacement)
            now = time.time()
            self.active.extend([now] * untracked)

        # Don't remove from active on disconnections - old workers being
        # replaced were already removed via mark_replaced(). Removing here
        # would incorrectly remove replacement workers' timestamps.

    def count_expiring_soon(self) -> int:
        """Count active workers approaching their timeout threshold."""
        now = time.time()
        threshold = self.lambda_timeout * self.replacement_pct
        return sum(1 for t in self.active if now - t >= threshold)

    def mark_replaced(self, count: int) -> None:
        """Mark oldest active workers as being replaced (remove from active)."""
        if count > 0 and self.active:
            self.active = self.active[count:]

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    @property
    def active_count(self) -> int:
        return len(self.active)


def main() -> None:
    """Run the worker orchestration loop."""
    # Check if fixed scaling is enabled (legacy mode)
    fixed_workers_str = os.environ.get("DESIRED_WORKERS")
    fixed_workers = int(fixed_workers_str) if fixed_workers_str else None

    # Load auto-scaling config
    scaling_config = ScalingConfig.from_env()

    worker_function = os.environ["WORKER_FUNCTION_NAME"]
    master_port = int(os.environ.get("MASTER_PORT", "5557"))
    poll_interval = int(os.environ.get("POLL_INTERVAL", "5"))
    pending_timeout = float(os.environ.get("PENDING_TIMEOUT", "30"))
    # Lambda timeout in seconds from CloudFormation (default 300s = 5 min)
    lambda_timeout = float(os.environ.get("LAMBDA_TIMEOUT", "300"))
    # Start replacements at 80% of timeout (before workers quit at 90%)
    replacement_pct = float(os.environ.get("REPLACEMENT_PCT", "0.8"))

    master_ip = get_task_ip()

    if fixed_workers is not None:
        logger.info(
            "Orchestrator started (fixed mode): master=%s:%d, workers=%d, lambda_timeout=%ds",
            master_ip,
            master_port,
            fixed_workers,
            int(lambda_timeout),
        )
    else:
        logger.info(
            "Orchestrator started (auto-scaling): master=%s:%d, "
            "users_per_worker=%d, rps_per_worker=%d, min=%d, max=%s, lambda_timeout=%ds",
            master_ip,
            master_port,
            scaling_config.users_per_worker,
            scaling_config.rps_per_worker,
            scaling_config.min_workers,
            scaling_config.max_workers or "unlimited",
            int(lambda_timeout),
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

    pool = WorkerPool(
        pending_timeout=pending_timeout,
        lambda_timeout=lambda_timeout,
        replacement_pct=replacement_pct,
    )
    last_connected = -1
    prev_stats: LocustStats | None = None
    last_stats_time = time.time()
    shutdown = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal shutdown
        logger.info("Received signal %d, initiating shutdown...", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not shutdown:
        try:
            # Query Locust master for stats (includes worker count, user count, RPS)
            stats = get_locust_stats()
            connected = stats.worker_count if stats else -1

            # Calculate time since last stats for rate calculation
            now = time.time()
            time_delta = now - last_stats_time

            # Calculate desired workers (auto-scaling or fixed)
            if fixed_workers is not None:
                desired_workers = fixed_workers
            else:
                desired_workers = calculate_desired_workers(
                    stats, scaling_config, prev_stats, time_delta
                )

            # Expire old pending invocations
            expired = pool.expire_old()
            if expired > 0:
                logger.warning("Expired %d pending invocations (failed to connect)", expired)

            # Update pending/active based on connection changes
            if connected >= 0 and last_connected >= 0:
                pool.connected(connected, last_connected)

            # Calculate how many workers we need
            if connected < 0:
                # Master not ready - assume 0 connected, keep pending
                effective = pool.pending_count
                needed = max(0, desired_workers - effective)
                expiring = 0
                logger.info(
                    "Master not ready: pending=%d, invoking=%d",
                    pool.pending_count,
                    needed,
                )
            else:
                effective = connected + pool.pending_count
                needed = max(0, desired_workers - effective)

                # Check for workers approaching timeout - invoke replacements proactively
                # BUT only replace if we still need them after they expire (allows scale-down)
                expiring = pool.count_expiring_soon()
                if expiring > 0:
                    # How many workers will we have after expiring ones leave?
                    workers_after_expiry = connected - expiring + pool.pending_count

                    # Only replace enough to reach desired_workers, not all expiring
                    replacements_needed = max(0, desired_workers - workers_after_expiry)

                    # Don't replace more than are expiring
                    replacements_needed = min(replacements_needed, expiring)

                    if replacements_needed > 0:
                        logger.info(
                            "Proactive replacement: %d expiring, replacing %d (desired=%d)",
                            expiring,
                            replacements_needed,
                            desired_workers,
                        )
                        needed += replacements_needed
                        # Mark replaced workers so we don't double-count
                        pool.mark_replaced(replacements_needed)
                    elif expiring > replacements_needed:
                        # Scaling down: let excess workers expire without replacement
                        not_replacing = expiring - replacements_needed
                        logger.info(
                            "Scale-down: %d expiring, not replacing %d (desired=%d)",
                            expiring,
                            not_replacing,
                            desired_workers,
                        )
                        pool.mark_replaced(not_replacing)

                # Log state on every poll for visibility
                if fixed_workers is not None:
                    logger.info(
                        "Workers: connected=%d, pending=%d, active=%d, "
                        "expiring=%d, effective=%d/%d, need=%d",
                        connected,
                        pool.pending_count,
                        pool.active_count,
                        expiring,
                        effective,
                        desired_workers,
                        needed,
                    )
                else:
                    # Include auto-scaling metrics (users, RPS)
                    assert stats is not None  # connected >= 0 means stats is valid
                    logger.info(
                        "Auto-scale: users=%d, rps=%.1f, desired=%d | "
                        "Workers: connected=%d, pending=%d, expiring=%d, need=%d",
                        stats.user_count,
                        stats.total_rps,
                        desired_workers,
                        connected,
                        pool.pending_count,
                        expiring,
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
            prev_stats = stats
            last_stats_time = now
            time.sleep(poll_interval)

        except Exception:
            logger.exception("Error in orchestration loop")
            time.sleep(poll_interval)

    logger.info("Orchestrator shutdown complete")


if __name__ == "__main__":
    main()

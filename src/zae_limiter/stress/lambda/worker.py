"""Lambda handler for Locust worker."""

from __future__ import annotations

import os
from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for Locust worker.

    Can run in two modes:
    - headless: Self-contained test, returns stats
    - worker: Connects to Fargate master

    Args:
        event: Lambda event with config
        context: Lambda context

    Returns:
        Test results or worker status
    """
    config = event.get("config", {})
    mode = config.get("mode", "headless")

    # Set environment for locustfile
    os.environ["TARGET_STACK_NAME"] = config["target_stack_name"]
    os.environ["TARGET_REGION"] = config.get("region", "us-east-1")
    os.environ["BASELINE_RPM"] = str(config.get("baseline_rpm", 400))
    os.environ["SPIKE_RPM"] = str(config.get("spike_rpm", 1500))
    os.environ["SPIKE_PROBABILITY"] = str(config.get("spike_probability", 0.10))

    if mode == "worker":
        return _run_as_worker(config)
    else:
        return _run_headless(config)


def _run_headless(config: dict[str, Any]) -> dict[str, Any]:
    """Run self-contained Locust test, return stats."""
    import gevent
    from locust.env import Environment

    # Import locustfile (copied into Lambda package)
    from locustfile import RateLimiterUser

    env = Environment(user_classes=[RateLimiterUser])
    env.create_local_runner()

    user_count = config.get("users", 10)
    spawn_rate = config.get("spawn_rate", 5)
    duration = config.get("duration_seconds", 60)

    # Start users
    env.runner.start(user_count, spawn_rate=spawn_rate)

    # Run for duration
    gevent.spawn_later(duration, env.runner.quit)
    env.runner.greenlet.join()

    # Collect stats
    stats = env.stats.total

    return {
        "total_requests": stats.num_requests,
        "total_failures": stats.num_failures,
        "avg_response_time": stats.avg_response_time,
        "min_response_time": stats.min_response_time,
        "max_response_time": stats.max_response_time,
        "p50": stats.get_response_time_percentile(0.50),
        "p95": stats.get_response_time_percentile(0.95),
        "p99": stats.get_response_time_percentile(0.99),
        "requests_per_second": stats.total_rps,
        "failure_rate": stats.fail_ratio,
    }


def _run_as_worker(config: dict[str, Any]) -> dict[str, Any]:
    """Connect to Fargate master as distributed worker."""
    from locust.env import Environment
    from locustfile import RateLimiterUser

    master_host = config["master_host"]
    master_port = config.get("master_port", 5557)

    env = Environment(user_classes=[RateLimiterUser])
    env.create_worker_runner(master_host, master_port)

    # Worker runs until master signals stop or Lambda times out
    env.runner.greenlet.join()

    return {"status": "worker_completed"}

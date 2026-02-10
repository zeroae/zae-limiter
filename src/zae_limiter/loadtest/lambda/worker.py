"""Lambda handler for Locust worker."""

from __future__ import annotations

# IMPORTANT: Import gevent FIRST to ensure monkey-patching happens
# before any other libraries (boto3, urllib3) initialize SSL contexts.
# Without this, we get RecursionError in ssl.py when locust is imported later.
import gevent.monkey

gevent.monkey.patch_all()

import importlib  # noqa: E402
import inspect  # noqa: E402
import os  # noqa: E402
from typing import Any  # noqa: E402


def _configure_boto3_pool(max_connections: int = 50) -> None:
    """Configure boto3 to use larger connection pool for DynamoDB.

    Must be called before any boto3 clients are created.
    Only applies the patch once per Lambda container.
    """
    if getattr(_configure_boto3_pool, "_configured", False):
        return

    import boto3
    from botocore.config import Config

    default_config = Config(max_pool_connections=max_connections)
    original_client = boto3.Session.client

    def patched_client(self: Any, service_name: str, **kwargs: Any) -> Any:
        if service_name == "dynamodb" and "config" not in kwargs:
            kwargs["config"] = default_config
        return original_client(self, service_name, **kwargs)  # type: ignore[call-overload]

    boto3.Session.client = patched_client  # type: ignore[assignment]
    _configure_boto3_pool._configured = True  # type: ignore[attr-defined]


def _load_user_classes(config: dict[str, Any]) -> list[type]:
    """Load Locust User classes dynamically.

    Resolution order for class names:
    1. config["user_classes"] — from orchestrator payload
    2. LOCUST_USER_CLASSES env var — from ECS/Lambda env
    3. Auto-discover all non-abstract User subclasses from locustfile module

    Module resolution:
    1. config["locustfile"] — from orchestrator payload
    2. LOCUSTFILE env var — from Lambda env (set via CloudFormation)
    3. Default: "locustfile" (standard Locust convention)
    """
    from locust import User

    # Determine which module to import
    locustfile = str(config.get("locustfile") or os.environ.get("LOCUSTFILE", "locustfile.py"))
    module_path = locustfile.replace("/", ".").removesuffix(".py")
    module = importlib.import_module(module_path)

    # Determine which classes to load
    class_names_str = config.get("user_classes") or os.environ.get("LOCUST_USER_CLASSES", "")

    if class_names_str:
        classes = []
        for name in class_names_str.split(","):
            name = name.strip()
            cls = getattr(module, name, None)
            if cls is None:
                raise ValueError(f"User class '{name}' not found in {module_path}")
            classes.append(cls)
        return classes

    # Auto-discover non-abstract User subclasses
    classes = [
        obj
        for _name, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, User) and obj is not User and not getattr(obj, "abstract", False)
    ]
    if not classes:
        raise ValueError(f"No User subclasses found in {module_path}")
    return classes


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
    # Configure boto3 connection pool for concurrent Locust users (default boto3 is 10)
    # Do this before SyncRateLimiter is created
    _configure_boto3_pool(max_connections=50)

    config = event.get("config", {})
    mode = config.get("mode", "headless")

    # Set environment for locustfile (use config or fall back to Lambda env vars)
    if "target_stack_name" in config:
        os.environ["TARGET_STACK_NAME"] = config["target_stack_name"]
    # TARGET_STACK_NAME and TARGET_REGION are set via Lambda env vars in CloudFormation
    os.environ.setdefault("TARGET_REGION", config.get("region", "us-east-1"))
    os.environ["BASELINE_RPM"] = str(config.get("baseline_rpm", 400))
    os.environ["SPIKE_RPM"] = str(config.get("spike_rpm", 1500))
    os.environ["SPIKE_PROBABILITY"] = str(config.get("spike_probability", 0.10))

    if mode == "worker":
        return _run_as_worker(config, context)
    else:
        return _run_headless(config, context)


def _run_headless(
    config: dict[str, Any], context: Any = None, shutdown_buffer_pct: float = 0.10
) -> dict[str, Any]:
    """Run self-contained Locust test, return stats.

    Args:
        config: Test configuration
        context: Lambda context (for remaining time check)
        shutdown_buffer_pct: Quit when this fraction of time remains (default 10%)
    """
    import gevent
    from locust import events as global_events
    from locust.env import Environment

    # Calculate shutdown buffer as percentage of initial timeout
    shutdown_buffer_ms = 30_000  # fallback if no context
    if context and hasattr(context, "get_remaining_time_in_millis"):
        initial_remaining_ms = context.get_remaining_time_in_millis()
        shutdown_buffer_ms = int(initial_remaining_ms * shutdown_buffer_pct)
        print(
            f"Lambda timeout: {initial_remaining_ms}ms, "
            f"shutdown buffer: {shutdown_buffer_ms}ms ({shutdown_buffer_pct:.0%})",
            flush=True,
        )

    print("Starting headless Locust test...", flush=True)

    user_classes = _load_user_classes(config)
    print(f"Loaded user classes: {[cls.__name__ for cls in user_classes]}")

    # Pass global events so module-level @events.test_start.add_listener decorators
    # in locustfiles fire correctly. Pass host so locustfiles can find the stack name.
    host = os.environ.get("TARGET_STACK_NAME", "")
    env = Environment(user_classes=user_classes, events=global_events, host=host)
    env.create_local_runner()
    assert env.runner is not None

    # Initialize stats BEFORE starting - this sets up the request event listener
    # Without this, stats.entries will be empty because the listener isn't registered
    _ = env.stats
    print(f"Created runner: {env.runner}, stats initialized")

    user_count = config.get("users", 10)
    spawn_rate = config.get("spawn_rate", 5)
    duration = config.get("duration_seconds", 60)

    print(f"Starting {user_count} users at {spawn_rate}/s for {duration}s...", flush=True)

    # Start users
    env.runner.start(user_count, spawn_rate=spawn_rate)
    print(f"Started. Runner state: {env.runner.state}", flush=True)

    # Run with periodic timeout checks
    print(f"Running for {duration}s...", flush=True)
    elapsed = 0
    check_interval = 5  # Check every 5 seconds
    while elapsed < duration:
        # Check if Lambda is about to timeout
        if context and hasattr(context, "get_remaining_time_in_millis"):
            remaining_ms = context.get_remaining_time_in_millis()
            if remaining_ms < shutdown_buffer_ms:
                print(
                    f"Lambda timeout approaching ({remaining_ms}ms remaining), "
                    f"stopping early after {elapsed}s",
                    flush=True,
                )
                break

        sleep_time = min(check_interval, duration - elapsed)
        gevent.sleep(sleep_time)
        elapsed += sleep_time

    print(f"Run complete after {elapsed}s. Runner state: {env.runner.state}", flush=True)

    # Stop the test
    env.runner.quit()
    print(f"Runner stopped. State: {env.runner.state}", flush=True)

    # Collect stats
    stats = env.stats.total
    p95 = stats.get_response_time_percentile(0.95)
    print(
        f"Total: {stats.num_requests} reqs, {stats.num_failures} failures, "
        f"avg={stats.avg_response_time:.1f}ms, p95={p95:.1f}ms",
        flush=True,
    )

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


def _run_as_worker(
    config: dict[str, Any], context: Any = None, shutdown_buffer_pct: float = 0.10
) -> dict[str, Any]:
    """Connect to Fargate master as distributed worker.

    Args:
        config: Worker configuration (master_host, master_port, etc.)
        context: Lambda context (for remaining time check)
        shutdown_buffer_pct: Quit when this fraction of time remains (default 10%)
    """
    import uuid

    from locust import events as global_events
    from locust.env import Environment

    # Calculate shutdown buffer as percentage of initial timeout
    shutdown_buffer_ms = 30_000  # fallback if no context
    if context and hasattr(context, "get_remaining_time_in_millis"):
        initial_remaining_ms = context.get_remaining_time_in_millis()
        shutdown_buffer_ms = int(initial_remaining_ms * shutdown_buffer_pct)

    master_host = config["master_host"]
    master_port = config.get("master_port", 5557)

    # Generate unique worker ID per invocation (Lambda reuses containers)
    # Use request ID if available, otherwise generate UUID
    # Set BEFORE creating runner - Locust uses this during registration
    if context and hasattr(context, "aws_request_id"):
        worker_id = f"lambda_{context.aws_request_id[:8]}"
    else:
        worker_id = f"lambda_{uuid.uuid4().hex[:8]}"

    # Set environment variable that Locust uses for worker identification
    os.environ["LOCUST_UNIQUE_ID"] = worker_id

    host = os.environ.get("TARGET_STACK_NAME", "")
    user_classes = _load_user_classes(config)
    print(
        f"Starting worker {worker_id} connecting to {master_host}:{master_port}, "
        f"classes: {[cls.__name__ for cls in user_classes]}, "
        f"shutdown_buffer: {shutdown_buffer_ms}ms",
        flush=True,
    )

    env = Environment(user_classes=user_classes, events=global_events, host=host)
    env.create_worker_runner(master_host, master_port)
    assert env.runner is not None

    # Worker runs until master signals stop or Lambda times out
    # Periodically check remaining time to quit gracefully before timeout
    check_interval = 5  # Check every 5 seconds
    while True:
        # Check if Lambda is about to timeout
        if context and hasattr(context, "get_remaining_time_in_millis"):
            remaining_ms = context.get_remaining_time_in_millis()
            if remaining_ms < shutdown_buffer_ms:
                print(
                    f"Lambda timeout approaching ({remaining_ms}ms remaining), "
                    f"worker {worker_id} quitting gracefully",
                    flush=True,
                )
                env.runner.quit()
                break

        # Wait for greenlet group to finish or check again after timeout
        # Group.join(timeout) waits for all greenlets or until timeout
        env.runner.greenlet.join(timeout=check_interval)

        # If group is empty (all greenlets finished), runner has stopped
        if len(env.runner.greenlet) == 0:
            break

    return {"status": "worker_completed", "worker_id": worker_id}

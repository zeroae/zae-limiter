"""E2E test fixtures.

Imports shared fixtures from integration conftest and adds E2E-specific fixtures.
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime

import boto3
import pytest
import pytest_asyncio

# Import shared fixtures from integration conftest
from tests.integration.conftest import (
    aggregator_stack_options,
    full_stack_options,
    localstack_endpoint,
    minimal_stack_options,
    unique_entity_prefix,
    unique_name,
    unique_name_class,
    unique_name_module,
)
from zae_limiter import RateLimiter, StackOptions, SyncRateLimiter

__all__ = [
    # Re-exported from integration
    "localstack_endpoint",
    "minimal_stack_options",
    "aggregator_stack_options",
    "full_stack_options",
    "unique_name",
    "unique_name_class",
    "unique_name_module",
    "unique_entity_prefix",
    # E2E-specific fixtures
    "e2e_stack_options",
    "localstack_limiter",
    "localstack_limiter_with_aggregator",
    "localstack_limiter_full",
    "sync_localstack_limiter",
    "e2e_limiter_module",
    "e2e_limiter_minimal_module",
    "cli_stack_module",
    # AWS client fixtures
    "cloudwatch_client",
    "sqs_client",
    "lambda_client",
    "s3_client",
    "dynamodb_client",
    # Polling helpers
    "poll_for_snapshots",
    "wait_for_event_source_mapping",
    "check_lambda_invocations",
]


# E2E-specific StackOptions


@pytest.fixture(scope="session")
def e2e_stack_options():
    """Full stack options for E2E tests with snapshot configuration."""
    return StackOptions(
        enable_aggregator=True,
        enable_alarms=True,
        snapshot_windows="hourly",
        usage_retention_days=7,
    )


# Module-scoped fixtures for E2E tests (issue #253)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def e2e_limiter_module(localstack_endpoint, e2e_stack_options, unique_name_module):
    """Module-scoped RateLimiter with aggregator for E2E tests.

    Creates CloudFormation stack once per test module. Tests MUST use
    unique_entity_prefix for entity ID isolation within the shared table.

    This fixture reduces E2E test time by avoiding repeated stack creation/deletion.
    See issue #253 for details.
    """
    limiter = RateLimiter(
        name=unique_name_module,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=e2e_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Module-scoped E2E stack cleanup failed: {e}")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def e2e_limiter_minimal_module(localstack_endpoint, minimal_stack_options):
    """Module-scoped minimal RateLimiter for error handling tests.

    Creates CloudFormation stack once per test module. Tests MUST use
    unique_entity_prefix for entity ID isolation within the shared table.

    Note: Generates its own unique name because e2e_limiter_module and
    e2e_limiter_minimal_module are different fixtures that need different stacks.
    """
    # Generate unique name for minimal stack (separate from e2e_limiter_module)
    timestamp = int(time.time())
    unique_id = uuid.uuid4().hex[:8]
    name = f"e2e-min-{timestamp}-{unique_id}"

    limiter = RateLimiter(
        name=name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Module-scoped minimal stack cleanup failed: {e}")


@pytest.fixture(scope="module")
def cli_stack_module(localstack_endpoint, minimal_stack_options):
    """Module-scoped CLI stack for CLI subcommand tests.

    Deploys a minimal stack once per module for tests that verify CLI commands
    (audit list, list, usage list) without needing to test deploy/delete lifecycle.

    Yields:
        tuple: (stack_name, endpoint_url) for use in CLI commands

    Note: Tests using this fixture should use unique entity IDs to avoid conflicts.
    """
    from click.testing import CliRunner

    from zae_limiter.cli import cli

    # Generate unique name for CLI stack
    timestamp = int(time.time())
    unique_id = uuid.uuid4().hex[:8]
    stack_name = f"e2e-cli-{timestamp}-{unique_id}"

    runner = CliRunner()

    # Deploy stack
    result = runner.invoke(
        cli,
        [
            "deploy",
            "--name",
            stack_name,
            "--endpoint-url",
            localstack_endpoint,
            "--region",
            "us-east-1",
            "--no-aggregator",
            "--no-alarms",
            "--wait",
        ],
    )
    if result.exit_code != 0:
        raise RuntimeError(f"CLI stack deployment failed: {result.output}")

    yield stack_name, localstack_endpoint

    # Cleanup
    runner.invoke(
        cli,
        [
            "delete",
            "--name",
            stack_name,
            "--endpoint-url",
            localstack_endpoint,
            "--region",
            "us-east-1",
            "--yes",
            "--wait",
        ],
    )


# Function-scoped fixtures (for tests that need fresh infrastructure)


@pytest.fixture
async def localstack_limiter(localstack_endpoint, minimal_stack_options, unique_name):
    """RateLimiter with minimal stack for core rate limiting tests."""
    limiter = RateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


@pytest.fixture
async def localstack_limiter_with_aggregator(
    localstack_endpoint, aggregator_stack_options, unique_name
):
    """RateLimiter with Lambda aggregator for stream testing."""
    limiter = RateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=aggregator_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


@pytest.fixture
async def localstack_limiter_full(localstack_endpoint, full_stack_options, unique_name):
    """RateLimiter with full stack including alarms."""
    limiter = RateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=full_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


@pytest.fixture
def sync_localstack_limiter(localstack_endpoint, minimal_stack_options, unique_name):
    """SyncRateLimiter with minimal stack for sync integration tests."""
    limiter = SyncRateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    with limiter:
        yield limiter

    try:
        limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


# AWS client fixtures for LocalStack


@pytest.fixture
def cloudwatch_client(localstack_endpoint):
    """CloudWatch client for alarm verification."""
    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("cloudwatch", **kwargs)


@pytest.fixture
def sqs_client(localstack_endpoint):
    """SQS client for DLQ verification."""
    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("sqs", **kwargs)


@pytest.fixture
def lambda_client(localstack_endpoint):
    """Lambda client for function inspection."""
    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("lambda", **kwargs)


@pytest.fixture
def s3_client(localstack_endpoint):
    """S3 client for archive verification."""
    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("s3", **kwargs)


@pytest.fixture
def dynamodb_client(localstack_endpoint):
    """DynamoDB client for table inspection."""
    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("dynamodb", **kwargs)


# Polling helpers for E2E tests


async def poll_for_snapshots(
    limiter: RateLimiter,
    entity_id: str,
    max_seconds: int = 180,
    initial_interval: float = 5.0,
) -> list:
    """
    Poll DynamoDB for usage snapshot records.

    Uses exponential backoff to avoid excessive API calls while providing
    fast feedback when snapshots appear.

    Args:
        limiter: RateLimiter instance with repository access
        entity_id: Entity to query snapshots for
        max_seconds: Maximum time to poll before giving up
        initial_interval: Starting poll interval (doubles each iteration, caps at 30s)

    Returns:
        List of DynamoDB items with usage snapshots

    Raises:
        TimeoutError: If no snapshots found within max_seconds
    """
    start_time = time.time()
    interval = initial_interval

    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_seconds:
            break

        repo = limiter._repository
        client = await repo._get_client()

        response = await client.query(
            TableName=repo.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": f"ENTITY#{entity_id}"},
                ":sk_prefix": {"S": "#USAGE#"},
            },
        )

        items = response.get("Items", [])
        if items:
            return items

        # Calculate sleep time: don't exceed remaining time or cap
        remaining = max_seconds - elapsed
        sleep_time = min(interval, 30.0, remaining)
        if sleep_time <= 0:
            break

        await asyncio.sleep(sleep_time)
        interval *= 2

    raise TimeoutError(f"No usage snapshots found for {entity_id} after {max_seconds}s")


async def wait_for_event_source_mapping(
    lambda_client,
    function_name: str,
    max_seconds: int = 60,
) -> bool:
    """
    Wait for Lambda Event Source Mapping to be in Enabled state.

    After CloudFormation stack creation completes, the ESM may still be in
    "Creating" or "Enabling" state. This function polls until it's "Enabled".

    Note: The infrastructure layer (StackManager.deploy_lambda_code) now handles
    the full ESM stabilization wait (~45s). This test helper just verifies the
    ESM reached "Enabled" state.

    Args:
        lambda_client: boto3 Lambda client
        function_name: Lambda function name
        max_seconds: Maximum time to wait

    Returns:
        True if ESM is enabled, False if timeout or no ESM found
    """
    start_time = time.time()
    interval = 5.0

    while time.time() - start_time < max_seconds:
        try:
            response = lambda_client.list_event_source_mappings(
                FunctionName=function_name,
            )
            mappings = response.get("EventSourceMappings", [])

            for mapping in mappings:
                state = mapping.get("State", "")
                if state == "Enabled":
                    return True
                elif state in ("Creating", "Enabling", "Updating"):
                    # Still transitioning, keep waiting
                    break
                elif state in ("Disabled", "Disabling"):
                    # ESM is disabled, won't process events
                    return False

            await asyncio.sleep(interval)
        except Exception:
            await asyncio.sleep(interval)

    return False


def check_lambda_invocations(
    cloudwatch_client,
    function_name: str,
    lookback_seconds: int = 300,
) -> int:
    """
    Query CloudWatch for Lambda invocation count.

    Provides diagnostic information about whether the Lambda aggregator
    is actually being triggered by DynamoDB streams.

    Args:
        cloudwatch_client: boto3 CloudWatch client
        function_name: Lambda function name
        lookback_seconds: Time window to query (seconds)

    Returns:
        Total number of invocations in time window, or -1 if query failed
    """
    from datetime import timedelta

    try:
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(seconds=lookback_seconds)

        response = cloudwatch_client.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Invocations",
            Dimensions=[
                {"Name": "FunctionName", "Value": function_name},
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=60,
            Statistics=["Sum"],
        )

        datapoints = response.get("Datapoints", [])
        return int(sum(dp["Sum"] for dp in datapoints)) if datapoints else 0
    except Exception:
        # Don't crash diagnostic code - return -1 to indicate query failed
        return -1

"""Polling helpers for E2E tests."""

import asyncio
import time
from datetime import UTC, datetime

from zae_limiter import RateLimiter


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
        ns_id = repo._namespace_id
        client = await repo._get_client()

        response = await client.query(
            TableName=repo.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": f"{ns_id}/ENTITY#{entity_id}"},
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
                    break
                elif state in ("Disabled", "Disabling"):
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
        return -1

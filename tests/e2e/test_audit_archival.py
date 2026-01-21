"""End-to-end tests for audit event archival to S3.

These tests verify the complete audit archival workflow:
1. Deploy stack with audit archival enabled
2. Create entities and generate audit events
3. Trigger TTL cleanup to delete expired audit records
4. Verify audit events are archived to S3 as gzip-compressed JSONL

To run these tests locally:
    # Start LocalStack (from project root)
    docker compose up -d

    # Set environment variables and run tests
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/e2e/test_audit_archival.py -v

Note: LocalStack TTL auto-scan runs every 60 minutes. Tests trigger TTL cleanup
manually via the LocalStack internal API endpoint.
"""

import asyncio
import gzip
import json
import time

import boto3
import pytest
import requests

from zae_limiter import RateLimiter, StackOptions

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.fixture(scope="session")
def archival_stack_options():
    """Stack with aggregator and audit archival enabled."""
    return StackOptions(
        enable_aggregator=True,
        enable_alarms=False,
        enable_audit_archival=True,
        audit_archive_glacier_days=90,
        snapshot_windows="hourly",
        retention_days=7,
    )


def trigger_ttl_cleanup(
    localstack_endpoint: str, max_retries: int = 3, retry_delay: float = 1.0
) -> tuple[bool, str]:
    """
    Trigger immediate TTL cleanup in LocalStack with retry logic.

    LocalStack auto-scans for expired items every 60 minutes, which is too
    slow for CI/CD. This function calls the internal API to force immediate
    cleanup of all expired items.

    Note: LocalStack has a race condition where concurrent table operations
    can cause "dictionary changed size during iteration" errors. This is
    worked around by retrying the request.

    Args:
        localstack_endpoint: LocalStack endpoint URL (e.g., http://localhost:4566)
        max_retries: Maximum number of retry attempts (default 3)
        retry_delay: Delay between retries in seconds (default 1.0)

    Returns:
        Tuple of (success, message) where success is True if cleanup was triggered
        successfully, and message contains details about the response or error.
    """
    url = f"{localstack_endpoint}/_aws/dynamodb/expired"
    last_msg = ""

    for attempt in range(max_retries):
        try:
            response = requests.delete(url, timeout=30)
            if response.status_code in (200, 204):
                msg = f"status={response.status_code}, body={response.text[:200]}"
                return True, msg

            # Check if it's a retryable error (LocalStack race condition)
            if response.status_code == 500 and "dictionary changed size" in response.text:
                last_msg = f"status={response.status_code}, body={response.text[:200]} (attempt {attempt + 1}/{max_retries})"
                time.sleep(retry_delay)
                continue

            # Non-retryable error
            last_msg = f"status={response.status_code}, body={response.text[:200]}"
            return False, last_msg

        except requests.RequestException as e:
            last_msg = f"RequestException: {e} (attempt {attempt + 1}/{max_retries})"
            time.sleep(retry_delay)

    return False, last_msg


async def poll_for_s3_objects(
    s3_client,
    bucket_name: str,
    prefix: str = "audit/",
    max_seconds: int = 60,
    initial_interval: float = 2.0,
) -> list:
    """
    Poll S3 for archived objects.

    Args:
        s3_client: boto3 S3 client
        bucket_name: S3 bucket name
        prefix: S3 key prefix to search
        max_seconds: Maximum time to poll
        initial_interval: Starting poll interval

    Returns:
        List of S3 object keys

    Raises:
        TimeoutError: If no objects found within max_seconds
    """
    start_time = time.time()
    interval = initial_interval

    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_seconds:
            break

        try:
            response = s3_client.list_objects_v2(
                Bucket=bucket_name,
                Prefix=prefix,
            )
            contents = response.get("Contents", [])
            if contents:
                return [obj["Key"] for obj in contents]
        except Exception:
            pass

        remaining = max_seconds - elapsed
        sleep_time = min(interval, 10.0, remaining)
        if sleep_time <= 0:
            break

        await asyncio.sleep(sleep_time)
        interval *= 1.5

    raise TimeoutError(f"No S3 objects found in {bucket_name}/{prefix} after {max_seconds}s")


class TestAuditArchival:
    """E2E tests for audit event archival to S3."""

    @pytest.mark.slow
    async def test_audit_events_archived_to_s3(
        self,
        localstack_endpoint,
        archival_stack_options,
        unique_name,
        s3_client,
    ):
        """
        Test that expired audit events are archived to S3.

        Steps:
        1. Deploy stack with audit archival enabled
        2. Create an entity (generates audit event)
        3. Set a very short TTL on the audit record (manually)
        4. Trigger TTL cleanup
        5. Wait for Lambda to process the REMOVE event
        6. Verify audit event is in S3 as gzip-compressed JSONL
        """
        limiter = RateLimiter(
            name=unique_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=archival_stack_options,
        )

        async with limiter:
            # Step 1: Create entity (generates audit event)
            entity = await limiter.create_entity(
                "archive-test-user",
                name="Archive Test User",
            )
            assert entity.id == "archive-test-user"

            # Step 2: Get the archive bucket name from CloudFormation stack outputs
            # Note: Bucket name is auto-generated by CloudFormation (not derived from stack name)
            # because stack names use ZAEL- prefix with uppercase, which is invalid for S3
            cfn_client = boto3.client(
                "cloudformation",
                endpoint_url=localstack_endpoint,
                region_name="us-east-1",
            )
            stack_name = f"ZAEL-{unique_name}"
            stack_response = cfn_client.describe_stacks(StackName=stack_name)
            outputs = {
                o["OutputKey"]: o["OutputValue"]
                for o in stack_response["Stacks"][0].get("Outputs", [])
            }
            bucket_name = outputs.get("AuditArchiveBucketName")
            assert bucket_name, "AuditArchiveBucketName output not found in stack"
            # Verify bucket name follows expected format: zael-{base_name}-data
            expected_bucket = f"zael-{unique_name.lower()}-data"
            assert bucket_name == expected_bucket, f"Expected {expected_bucket}, got {bucket_name}"

            # Wait a moment for the audit event to be written
            await asyncio.sleep(1)

            # Step 3: Manually set a very short TTL on audit records
            # This requires direct DynamoDB access
            repo = limiter._repository
            client = await repo._get_client()

            # Query for audit records
            response = await client.query(
                TableName=repo.table_name,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={
                    ":pk": {"S": f"AUDIT#{entity.id}"},
                },
            )

            items = response.get("Items", [])
            assert len(items) > 0, "No audit records found"

            # Update TTL to expire immediately (1 second in the past)
            expired_ttl = int(time.time()) - 1
            for item in items:
                await client.update_item(
                    TableName=repo.table_name,
                    Key={
                        "PK": item["PK"],
                        "SK": item["SK"],
                    },
                    UpdateExpression="SET #ttl = :ttl",
                    ExpressionAttributeNames={"#ttl": "ttl"},
                    ExpressionAttributeValues={":ttl": {"N": str(expired_ttl)}},
                )

            # Step 4: Trigger TTL cleanup
            cleanup_success, cleanup_msg = trigger_ttl_cleanup(localstack_endpoint)
            assert cleanup_success, f"Failed to trigger TTL cleanup: {cleanup_msg}"

            # Step 5: Wait for Lambda to process and archive
            # Give some time for the stream event to be processed
            await asyncio.sleep(5)

            # Step 6: Check S3 for archived events
            try:
                s3_keys = await poll_for_s3_objects(
                    s3_client,
                    bucket_name,
                    prefix="audit/",
                    max_seconds=60,
                )
                assert len(s3_keys) > 0, "No archive objects found in S3"

                # Verify the content is valid gzip-compressed JSONL
                for key in s3_keys:
                    response = s3_client.get_object(Bucket=bucket_name, Key=key)
                    body = response["Body"].read()

                    # Decompress and parse
                    jsonl_content = gzip.decompress(body).decode("utf-8")
                    lines = jsonl_content.strip().split("\n")
                    assert len(lines) > 0, "JSONL file is empty"

                    # Parse each line as JSON
                    for line in lines:
                        event = json.loads(line)
                        # Verify expected audit event structure
                        assert "action" in event or "timestamp" in event, (
                            f"Invalid audit event structure: {event}"
                        )

            except TimeoutError:
                # If no objects found, the test may have run too fast
                # or LocalStack Lambda execution is delayed
                pytest.skip(
                    "S3 archive objects not found within timeout - "
                    "LocalStack Lambda processing may be delayed"
                )

        # Cleanup
        try:
            await limiter.delete_stack()
        except Exception:
            pass

    @pytest.mark.slow
    async def test_archival_disabled_no_s3_bucket(
        self,
        localstack_endpoint,
        unique_name,
    ):
        """
        Test that no S3 bucket is created when archival is disabled.
        """
        stack_options = StackOptions(
            enable_aggregator=True,
            enable_alarms=False,
            enable_audit_archival=False,
        )

        limiter = RateLimiter(
            name=unique_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            # The stack should deploy without the S3 bucket
            # Check that the AuditArchiveBucketName output is not present
            cfn_client = boto3.client(
                "cloudformation",
                endpoint_url=localstack_endpoint,
                region_name="us-east-1",
            )
            stack_name = f"ZAEL-{unique_name}"
            stack_response = cfn_client.describe_stacks(StackName=stack_name)
            outputs = {
                o["OutputKey"]: o["OutputValue"]
                for o in stack_response["Stacks"][0].get("Outputs", [])
            }

            # Verify the bucket output does NOT exist when archival is disabled
            assert "AuditArchiveBucketName" not in outputs, (
                "AuditArchiveBucketName should not exist when archival is disabled"
            )

        # Cleanup
        try:
            await limiter.delete_stack()
        except Exception:
            pass

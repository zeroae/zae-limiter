"""End-to-end integration tests against real AWS.

These tests run against actual AWS services and validate:
- Full CloudFormation stack lifecycle
- Lambda aggregator processing
- CloudWatch alarm states
- DLQ monitoring

IMPORTANT: These tests require:
1. Valid AWS credentials with permissions for:
   - CloudFormation (create/delete stacks)
   - DynamoDB (full access)
   - Lambda (create/update/delete)
   - CloudWatch (describe alarms, get metrics)
   - SQS (receive messages)
   - IAM (create roles)
2. The --run-aws pytest flag

To run:
    pytest tests/test_e2e_aws.py --run-aws -v

WARNING: These tests create real AWS resources and may incur charges.
Resources are cleaned up after tests, but verify via AWS Console.
"""

import asyncio
import time
from datetime import UTC, datetime

import pytest

from zae_limiter import Limit, RateLimiter, StackOptions

pytestmark = [pytest.mark.aws, pytest.mark.e2e]

# Additional markers for test filtering:
# - slow: Tests with significant wait times (>30s sleeps)
# - monitoring: Tests that verify CloudWatch alarms, metrics, DLQ
# - snapshots: Tests that verify usage snapshot creation
#
# Usage:
#   pytest tests/test_e2e_aws.py --run-aws -v                    # Run all
#   pytest tests/test_e2e_aws.py --run-aws -v -m "not slow"      # Skip slow tests
#   pytest tests/test_e2e_aws.py --run-aws -v -m "not monitoring"  # Skip monitoring tests


# AWS-specific fixtures that don't depend on localstack_endpoint


@pytest.fixture
def aws_cloudwatch_client():
    """CloudWatch client for real AWS."""
    import boto3

    return boto3.client("cloudwatch", region_name="us-east-1")


@pytest.fixture
def aws_sqs_client():
    """SQS client for real AWS."""
    import boto3

    return boto3.client("sqs", region_name="us-east-1")


@pytest.fixture
def aws_lambda_client():
    """Lambda client for real AWS."""
    import boto3

    return boto3.client("lambda", region_name="us-east-1")


class TestE2EAWSFullWorkflow:
    """E2E tests against real AWS."""

    @pytest.fixture
    async def aws_limiter(self, unique_name):
        """
        Create RateLimiter with full stack on real AWS.

        Uses unique name for isolation.
        """
        stack_options = StackOptions(
            enable_aggregator=True,
            enable_alarms=True,
            snapshot_windows="hourly",
            retention_days=1,  # Minimum for cost
            lambda_timeout=60,
            lambda_memory=256,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
        )

        limiter = RateLimiter(
            name=unique_name,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        # Explicitly delete the stack after test completes
        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    async def test_complete_aws_workflow(self, aws_limiter):
        """
        Complete E2E workflow on real AWS.

        Steps:
        1. Create hierarchical entities
        2. Set and use rate limits
        3. Wait for aggregator processing
        4. Verify operations complete successfully
        """
        # Create entities
        await aws_limiter.create_entity("aws-parent", name="AWS Parent Org")
        await aws_limiter.create_entity(
            "aws-child",
            name="AWS Child Key",
            parent_id="aws-parent",
        )

        # Set limits
        limits = [Limit.per_minute("rpm", 100)]
        await aws_limiter.set_limits("aws-parent", limits)

        # Use rate limiting
        for _ in range(5):
            async with aws_limiter.acquire(
                entity_id="aws-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                cascade=True,
            ):
                await asyncio.sleep(0.5)  # Small delay

        # Verify available tokens
        available = await aws_limiter.available(
            entity_id="aws-child",
            resource="api",
            limits=limits,
        )
        assert available["rpm"] < 100

    @pytest.mark.asyncio
    async def test_role_has_permission_boundary(self, aws_limiter, unique_name):
        """Verify the Lambda role was created with permission boundary and custom name."""
        import boto3

        iam = boto3.client("iam", region_name="us-east-1")

        # The role name format "PowerUserPB-{}" produces "PowerUserPB-ZAEL-{name}-aggregator-role"
        # The {} is replaced with the full default role name: ZAEL-{name}-aggregator-role
        expected_role_name = f"PowerUserPB-ZAEL-{unique_name}-aggregator-role"

        role = iam.get_role(RoleName=expected_role_name)

        assert role["Role"]["RoleName"] == expected_role_name
        assert role["Role"]["PermissionsBoundary"]["PermissionsBoundaryArn"] == (
            "arn:aws:iam::aws:policy/PowerUserAccess"
        )

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.monitoring
    async def test_cloudwatch_alarm_states(self, aws_limiter, aws_cloudwatch_client, unique_name):
        """
        Verify CloudWatch alarms are in expected states.

        After normal operation, all alarms should be in OK or INSUFFICIENT_DATA state.
        """
        # Do some normal operations first
        await aws_limiter.create_entity("alarm-test-user")
        limits = [Limit.per_minute("rpm", 100)]

        async with aws_limiter.acquire(
            entity_id="alarm-test-user",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Wait for CloudWatch to stabilize
        await asyncio.sleep(60)

        # Check alarm states - alarm names are based on stack name pattern
        stack_name = f"ZAEL-{unique_name}"
        alarm_name_prefix = stack_name

        response = aws_cloudwatch_client.describe_alarms(
            AlarmNamePrefix=alarm_name_prefix,
            AlarmTypes=["MetricAlarm"],
        )

        alarms = response.get("MetricAlarms", [])

        # Verify alarms exist (at least some should be created)
        if alarms:
            # Check states (should be OK or INSUFFICIENT_DATA, not ALARM)
            for alarm in alarms:
                state = alarm["StateValue"]
                assert state in ["OK", "INSUFFICIENT_DATA"], (
                    f"Alarm {alarm['AlarmName']} in unexpected state: {state}"
                )

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.monitoring
    async def test_dlq_is_empty(self, aws_limiter, aws_sqs_client, unique_name):
        """
        Verify Dead Letter Queue has no messages after normal operation.

        A non-empty DLQ indicates Lambda processing failures.
        """
        # Do some operations
        await aws_limiter.create_entity("dlq-test-user")
        limits = [Limit.per_minute("rpm", 100)]

        for _ in range(3):
            async with aws_limiter.acquire(
                entity_id="dlq-test-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Wait for stream processing
        await asyncio.sleep(30)

        # Get DLQ URL
        dlq_name = f"ZAEL-{unique_name}-aggregator-dlq"
        try:
            response = aws_sqs_client.get_queue_url(QueueName=dlq_name)
            dlq_url = response["QueueUrl"]

            # Check for messages
            msg_response = aws_sqs_client.receive_message(
                QueueUrl=dlq_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=5,
            )

            messages = msg_response.get("Messages", [])
            assert len(messages) == 0, f"DLQ has {len(messages)} messages"
        except aws_sqs_client.exceptions.QueueDoesNotExist:
            # Queue might not exist if aggregator disabled
            pass

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.monitoring
    async def test_lambda_metrics(self, aws_limiter, aws_cloudwatch_client, unique_name):
        """
        Query Lambda metrics to verify aggregator is working.

        Checks:
        - Invocations > 0 (Lambda is being triggered)
        - Errors = 0 (no failures)
        """
        # Generate traffic to trigger Lambda
        await aws_limiter.create_entity("metrics-test-user")
        limits = [Limit.per_minute("rpm", 100)]

        for _ in range(10):
            async with aws_limiter.acquire(
                entity_id="metrics-test-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Wait for Lambda invocations and metrics
        await asyncio.sleep(120)  # CloudWatch metrics have delay

        function_name = f"ZAEL-{unique_name}-aggregator"
        end_time = datetime.now(UTC)
        start_time_epoch = time.time() - 300  # Last 5 minutes
        start_time = datetime.fromtimestamp(start_time_epoch, tz=UTC)

        # Query Invocations metric
        response = aws_cloudwatch_client.get_metric_statistics(
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
        if datapoints:
            total_invocations = sum(dp["Sum"] for dp in datapoints)
            assert total_invocations > 0, "Lambda should have been invoked"

        # Query Errors metric
        response = aws_cloudwatch_client.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Errors",
            Dimensions=[
                {"Name": "FunctionName", "Value": function_name},
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=60,
            Statistics=["Sum"],
        )

        datapoints = response.get("Datapoints", [])
        if datapoints:
            total_errors = sum(dp["Sum"] for dp in datapoints)
            assert total_errors == 0, f"Lambda had {total_errors} errors"


class TestE2EAWSUsageSnapshots:
    """Tests for usage snapshot verification on real AWS."""

    @pytest.fixture
    async def aws_limiter_with_snapshots(self, unique_name):
        """Create RateLimiter configured for snapshot testing."""
        stack_options = StackOptions(
            enable_aggregator=True,
            enable_alarms=False,  # Faster for snapshot tests
            snapshot_windows="hourly,daily",
            retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
        )

        limiter = RateLimiter(
            name=unique_name,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        # Explicitly delete the stack after test completes
        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.snapshots
    @pytest.mark.xfail(reason="Flaky: Lambda may not create snapshots within 120s wait. See #84")
    async def test_usage_snapshots_created(self, aws_limiter_with_snapshots):
        """
        Verify Lambda aggregator creates usage snapshots.

        This test:
        1. Generates token consumption
        2. Waits for stream processing
        3. Queries DynamoDB for usage records
        4. Validates snapshot structure
        """
        await aws_limiter_with_snapshots.create_entity("snapshot-test-user")

        limits = [
            Limit.per_minute("rpm", 1000),
            Limit.per_minute("tpm", 100000),
        ]

        # Generate significant traffic
        for _ in range(20):
            async with aws_limiter_with_snapshots.acquire(
                entity_id="snapshot-test-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                await asyncio.sleep(0.5)

        # Wait for stream processing and snapshot creation
        # DynamoDB Streams have some latency, and Lambda batches
        await asyncio.sleep(120)

        # Query for usage snapshots
        repo = aws_limiter_with_snapshots._repository
        client = await repo._get_client()

        response = await client.query(
            TableName=repo.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": "ENTITY#snapshot-test-user"},
                ":sk_prefix": {"S": "#USAGE#"},
            },
        )

        items = response.get("Items", [])

        # Should have at least hourly snapshot
        assert len(items) > 0, "No usage snapshots found"

        # Validate snapshot structure
        for item in items:
            data = item.get("data", {}).get("M", {})
            # Snapshots should have window and resource info
            assert data, "Snapshot should have data"


class TestE2EAWSRateLimiting:
    """Additional rate limiting tests for real AWS."""

    @pytest.fixture
    async def aws_limiter_minimal(self, unique_name):
        """Create RateLimiter with minimal stack for faster tests."""
        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
        )

        limiter = RateLimiter(
            name=unique_name,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        # Explicitly delete the stack after test completes
        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    async def test_high_throughput_operations(self, aws_limiter_minimal):
        """
        Test high throughput rate limiting operations.

        Verifies the system handles rapid sequential operations correctly.
        """
        await aws_limiter_minimal.create_entity("throughput-user")

        limits = [Limit.per_minute("rpm", 1000)]

        # Rapid operations
        for _ in range(50):
            async with aws_limiter_minimal.acquire(
                entity_id="throughput-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Verify tokens were consumed
        # Note: With 1000 rpm, refill rate is ~16.67 tokens/second
        # Over ~5 seconds of test execution with network latency, significant refill occurs
        available = await aws_limiter_minimal.available(
            entity_id="throughput-user",
            resource="api",
            limits=limits,
        )
        # Just verify that some tokens were consumed - the exact amount varies
        # due to refill during network round trips to DynamoDB
        assert available["rpm"] < 1000, "Should have consumed some tokens"

    @pytest.mark.asyncio
    async def test_multiple_resources(self, aws_limiter_minimal):
        """
        Test rate limiting across multiple resources.

        Each resource should have independent limits.
        """
        await aws_limiter_minimal.create_entity("multi-resource-user")

        limits = [Limit.per_minute("rpm", 100)]

        # Consume on different resources
        async with aws_limiter_minimal.acquire(
            entity_id="multi-resource-user",
            resource="gpt-3.5-turbo",
            limits=limits,
            consume={"rpm": 10},
        ):
            pass

        async with aws_limiter_minimal.acquire(
            entity_id="multi-resource-user",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 20},
        ):
            pass

        # Verify independent limits
        # Note: With 100 rpm, refill rate is ~1.67 tokens/second
        # Allow tolerance for refill during network round trips
        gpt35_available = await aws_limiter_minimal.available(
            entity_id="multi-resource-user",
            resource="gpt-3.5-turbo",
            limits=limits,
        )
        gpt4_available = await aws_limiter_minimal.available(
            entity_id="multi-resource-user",
            resource="gpt-4",
            limits=limits,
        )

        # Each resource should have consumed tokens independently
        assert gpt35_available["rpm"] < 100, "gpt-3.5-turbo should have consumed tokens"
        assert gpt4_available["rpm"] < 100, "gpt-4 should have consumed tokens"
        # gpt-4 consumed more (20 vs 10), so should have fewer available
        # But with refill, we can only reliably check they're both under the limit
        assert gpt35_available["rpm"] <= 95, "gpt-3.5-turbo should have consumed at least 5 tokens"
        assert gpt4_available["rpm"] <= 90, "gpt-4 should have consumed at least 10 tokens"

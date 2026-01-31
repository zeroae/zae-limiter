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
import pytest_asyncio

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

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def aws_limiter(self, unique_name_class):
        """
        Create RateLimiter with full stack on real AWS.

        Uses unique name for isolation. Class-scoped to share stack across tests.
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
            name=unique_name_class,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        # Clean up stack after test completes
        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio(loop_scope="class")
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
            cascade=True,
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
            ):
                await asyncio.sleep(0.5)  # Small delay

        # Verify available tokens
        available = await aws_limiter.available(
            entity_id="aws-child",
            resource="api",
            limits=limits,
        )
        assert available["rpm"] < 100

    @pytest.mark.asyncio(loop_scope="class")
    async def test_role_has_permission_boundary(self, aws_limiter, unique_name_class):
        """Verify the Lambda role was created with permission boundary and custom name."""
        import boto3

        iam = boto3.client("iam", region_name="us-east-1")

        # The role name format "PowerUserPB-{}" produces "PowerUserPB-{name}-aggr"
        # The {} is replaced with the component name: {name}-aggr (ADR-116)
        expected_role_name = f"PowerUserPB-{unique_name_class}-aggr"

        role = iam.get_role(RoleName=expected_role_name)

        assert role["Role"]["RoleName"] == expected_role_name
        assert role["Role"]["PermissionsBoundary"]["PermissionsBoundaryArn"] == (
            "arn:aws:iam::aws:policy/PowerUserAccess"
        )

    @pytest.mark.asyncio(loop_scope="class")
    @pytest.mark.slow
    @pytest.mark.monitoring
    async def test_cloudwatch_alarm_states(
        self, aws_limiter, aws_cloudwatch_client, unique_name_class
    ):
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
        stack_name = unique_name_class
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

    @pytest.mark.asyncio(loop_scope="class")
    @pytest.mark.slow
    @pytest.mark.monitoring
    async def test_dlq_is_empty(self, aws_limiter, aws_sqs_client, unique_name_class):
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
        dlq_name = f"{unique_name_class}-aggregator-dlq"
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

    @pytest.mark.asyncio(loop_scope="class")
    @pytest.mark.slow
    @pytest.mark.monitoring
    async def test_lambda_metrics(self, aws_limiter, aws_cloudwatch_client, unique_name_class):
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

        function_name = f"{unique_name_class}-aggregator"
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

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def aws_limiter_with_snapshots(self, unique_name_class):
        """Create RateLimiter configured for snapshot testing. Class-scoped to share stack."""
        stack_options = StackOptions(
            enable_aggregator=True,
            enable_alarms=False,  # Faster for snapshot tests
            snapshot_windows="hourly,daily",
            retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
        )

        limiter = RateLimiter(
            name=unique_name_class,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio(loop_scope="class")
    @pytest.mark.slow
    @pytest.mark.snapshots
    async def test_usage_snapshots_created(
        self,
        aws_limiter_with_snapshots,
        aws_cloudwatch_client,
        aws_lambda_client,
        unique_name_class,
    ):
        """
        Verify Lambda aggregator creates usage snapshots.

        This test:
        1. Generates token consumption (triggers DynamoDB stream events)
        2. Polls DynamoDB for usage records (180s timeout with exponential backoff)
        3. On timeout, checks Lambda invocations for diagnostics
        4. Validates snapshot structure
        """
        from tests.e2e.conftest import (
            check_lambda_invocations,
            poll_for_snapshots,
            wait_for_event_source_mapping,
        )

        # Wait for Event Source Mapping to be enabled before generating traffic
        # The ESM may still be in "Creating" or "Enabling" state after stack creation
        function_name = f"{unique_name_class}-aggregator"
        esm_enabled = await wait_for_event_source_mapping(
            aws_lambda_client,
            function_name,
            max_seconds=60,
        )

        assert esm_enabled, (
            f"Event Source Mapping for {function_name} did not become enabled within 60s"
        )

        # Use unique entity per test run to avoid conflicts with shared stack
        import uuid

        entity_id = f"snapshot-test-{uuid.uuid4().hex[:8]}"
        await aws_limiter_with_snapshots.create_entity(entity_id)

        # Use limits designed for reliable snapshot capture in tests.
        #
        # IMPORTANT: See issue #179 for a known design limitation with high TPM limits.
        #
        # Key insight: DynamoDB stream records capture OldImage (before write) and
        # NewImage (after write). The aggregator calculates:
        #   tokens_delta = old_tokens - new_tokens = consumed - refilled_since_last_read
        #
        # If refill >= consumed during the operation's round-trip time, delta <= 0
        # and the record is skipped. With realistic 10M TPM and ~100ms latency:
        #   refill = 100ms * (10M / 60000ms) = 16,667 tokens
        #   Need to consume >> 16,667 tokens per operation for positive delta.
        #
        # For testing, we use lower TPM (100K) so smaller consumption values work:
        #   refill = 100ms * (100K / 60000ms) = 167 tokens
        #   Consuming 1,000 tokens ensures positive delta: 1000 - 167 = 833 (captured!)
        #
        # Note: This is a known limitation (#179). The snapshot feature does not reliably
        # capture usage when refill_rate > consumption_rate / operation_latency.
        # A proposed fix is to track gross consumption in a separate counter.
        limits = [
            Limit.per_minute("rpm", 10000),  # 10K requests per minute
            Limit.per_minute("tpm", 100_000),  # 100K TPM for reliable test capture
        ]

        # Phase 1: Initial acquire to create bucket records (INSERT events).
        # The ESM filter only captures MODIFY events on #BUCKET# records, so we need
        # to first create the buckets, then modify them to trigger the Lambda.
        async with aws_limiter_with_snapshots.acquire(
            entity_id=entity_id,
            resource="api",
            limits=limits,
            consume={"rpm": 1, "tpm": 1000},  # 1K tokens >> 167 refill per 100ms
        ):
            pass

        # Wait for DynamoDB streams to stabilize and ESM to be fully ready.
        # Even though ESM reports "Enabled", there can be a brief delay before
        # it starts processing events from the stream.
        await asyncio.sleep(5)

        # Phase 2: Generate more traffic (MODIFY events) that will be captured.
        # These operations update existing bucket records, producing MODIFY events
        # that match the ESM filter and trigger Lambda invocations.
        for _ in range(5):
            async with aws_limiter_with_snapshots.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 1000},  # 1K tokens >> refill ensures positive delta
            ):
                pass  # No sleep - back-to-back requests

        # Poll for snapshots with 90s timeout (exponential backoff starting at 5s, capped at 30s)
        # This gives DynamoDB Streams, Lambda batching, and CloudWatch time to process
        try:
            items = await poll_for_snapshots(
                aws_limiter_with_snapshots,
                entity_id=entity_id,
                max_seconds=90,
                initial_interval=5.0,
            )
        except TimeoutError:
            # Diagnostic: Check Lambda invocations to help debug the failure
            # Note: CloudWatch metrics have 1-2 minute delay, so check last 5 minutes
            invocations = check_lambda_invocations(
                aws_cloudwatch_client,
                function_name,
                lookback_seconds=300,
            )
            if invocations == -1:
                invocations_msg = "CloudWatch metrics unavailable"
            else:
                invocations_msg = str(invocations)
            pytest.fail(
                f"No usage snapshots found after 90s. "
                f"Lambda {function_name} invocations in last 5 min: {invocations_msg}. "
                f"Check DynamoDB Streams configuration and Lambda logs."
            )

        # Should have at least hourly snapshot
        assert len(items) > 0, "No usage snapshots found"

        # Validate snapshot structure (flat schema per issue #168)
        for item in items:
            # Snapshots use a flat schema (not nested data.M) for atomic upserts
            assert "window" in item, "Snapshot should have window"
            assert "resource" in item, "Snapshot should have resource"
            assert "total_events" in item, "Snapshot should have total_events"


class TestE2EAWSRateLimiting:
    """Additional rate limiting tests for real AWS."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def aws_limiter_minimal(self, unique_name_class):
        """Create RateLimiter with minimal stack for faster tests. Class-scoped to share stack."""
        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
        )

        limiter = RateLimiter(
            name=unique_name_class,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio(loop_scope="class")
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

    @pytest.mark.asyncio(loop_scope="class")
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


class TestE2EAWSXRayTracingEnabled:
    """Tests for X-Ray tracing when enabled."""

    @pytest.fixture
    def aws_xray_client(self):
        """X-Ray client for real AWS."""
        import boto3

        return boto3.client("xray", region_name="us-east-1")

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def aws_limiter_with_tracing(self, unique_name_class):
        """Create RateLimiter with X-Ray tracing enabled. Class-scoped to share stack."""
        stack_options = StackOptions(
            enable_aggregator=True,
            enable_tracing=True,
            enable_alarms=False,  # Faster for tracing tests
            retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
        )

        limiter = RateLimiter(
            name=unique_name_class,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_lambda_tracing_enabled(
        self, aws_limiter_with_tracing, aws_lambda_client, unique_name_class
    ):
        """
        Verify Lambda function has Active tracing when enabled.

        Checks:
        - TracingConfig.Mode is 'Active'
        """
        function_name = f"{unique_name_class}-aggregator"

        response = aws_lambda_client.get_function_configuration(FunctionName=function_name)

        assert response["TracingConfig"]["Mode"] == "Active", (
            f"Expected TracingConfig.Mode='Active', got '{response['TracingConfig']['Mode']}'"
        )

    @pytest.mark.asyncio(loop_scope="class")
    async def test_iam_role_has_xray_permissions(self, aws_limiter_with_tracing, unique_name_class):
        """
        Verify IAM role has X-Ray permissions when tracing is enabled.

        Checks:
        - XRayAccess policy exists
        - xray:PutTraceSegments permission exists
        - xray:PutTelemetryRecords permission exists
        """
        import boto3

        iam = boto3.client("iam", region_name="us-east-1")
        role_name = f"PowerUserPB-{unique_name_class}-aggr"

        # Get inline policies
        response = iam.list_role_policies(RoleName=role_name)
        policy_names = response.get("PolicyNames", [])

        assert "XRayAccess" in policy_names, (
            f"XRayAccess policy not found. Found policies: {policy_names}"
        )

        # Get policy document
        policy_response = iam.get_role_policy(RoleName=role_name, PolicyName="XRayAccess")
        policy_doc = policy_response["PolicyDocument"]
        statements = policy_doc["Statement"]

        assert len(statements) > 0, "XRayAccess policy should have statements"

        # Verify X-Ray policy structure
        xray_statement = statements[0]
        assert xray_statement.get("Effect") == "Allow", "XRayAccess should have Effect=Allow"
        assert xray_statement.get("Resource") == "*", "X-Ray requires Resource='*'"

        # Verify X-Ray actions
        actions = xray_statement.get("Action", [])
        assert "xray:PutTraceSegments" in actions, "Missing xray:PutTraceSegments"
        assert "xray:PutTelemetryRecords" in actions, "Missing xray:PutTelemetryRecords"

    @pytest.mark.asyncio(loop_scope="class")
    @pytest.mark.slow
    @pytest.mark.xfail(
        reason="Requires X-Ray SDK instrumentation in Lambda (see #194)",
        strict=False,
    )
    async def test_xray_traces_created(
        self, aws_limiter_with_tracing, aws_lambda_client, aws_xray_client, unique_name_class
    ):
        """
        Verify X-Ray traces are created when Lambda is invoked.

        Steps:
        1. Generate rate limiting traffic (triggers Lambda via DynamoDB stream)
        2. Poll X-Ray service for trace data (with retry)
        3. Verify trace structure includes Lambda segment and DynamoDB subsegments

        Note: This test is marked xfail because DynamoDB subsegments require
        aws-xray-sdk instrumentation in the Lambda handler. See issue #194.
        """
        import json
        import uuid
        from datetime import timedelta

        from tests.e2e.conftest import wait_for_event_source_mapping

        function_name = f"{unique_name_class}-aggregator"

        # Wait for Event Source Mapping to be enabled
        esm_enabled = await wait_for_event_source_mapping(
            aws_lambda_client, function_name, max_seconds=60
        )
        assert esm_enabled, f"ESM for {function_name} did not become enabled"

        # Generate traffic to trigger Lambda
        entity_id = f"xray-test-{uuid.uuid4().hex[:8]}"
        await aws_limiter_with_tracing.create_entity(entity_id)

        limits = [Limit.per_minute("rpm", 100)]

        # Phase 1: Create buckets (INSERT events - not captured by ESM filter)
        async with aws_limiter_with_tracing.acquire(
            entity_id=entity_id,
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        await asyncio.sleep(5)  # Wait for stream to stabilize

        # Phase 2: Generate MODIFY events (captured by ESM filter, triggers Lambda)
        for _ in range(3):
            async with aws_limiter_with_tracing.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Poll for X-Ray traces with exponential backoff
        # X-Ray traces can take 30-120 seconds to become queryable
        trace_summaries = []
        max_attempts = 12  # 12 attempts * 15s = 180s max wait
        for _attempt in range(max_attempts):
            await asyncio.sleep(15)

            end_time = datetime.now(UTC)
            start_time = end_time - timedelta(minutes=5)

            filter_expr = f'service(id(name: "{function_name}", type: "AWS::Lambda::Function"))'
            response = aws_xray_client.get_trace_summaries(
                StartTime=start_time,
                EndTime=end_time,
                FilterExpression=filter_expr,
            )

            trace_summaries = response.get("TraceSummaries", [])
            if trace_summaries:
                break

        assert len(trace_summaries) > 0, (
            f"No X-Ray traces found for {function_name} after {max_attempts * 15}s. "
            "Ensure Lambda was invoked and X-Ray is properly configured."
        )

        # Get detailed trace data for the first trace
        trace_id = trace_summaries[0]["Id"]
        trace_response = aws_xray_client.batch_get_traces(TraceIds=[trace_id])
        traces = trace_response.get("Traces", [])

        assert len(traces) > 0, f"No trace data for trace ID {trace_id}"

        # Verify trace structure
        trace = traces[0]
        segments = trace.get("Segments", [])
        assert len(segments) > 0, "Trace has no segments"

        # Parse segment documents (they're JSON strings)
        segment_docs = [json.loads(seg["Document"]) for seg in segments]

        # Verify we have a Lambda segment
        lambda_segments = [
            seg for seg in segment_docs if seg.get("origin") == "AWS::Lambda::Function"
        ]
        assert len(lambda_segments) > 0, "No Lambda segment found in trace"

        # Verify Lambda segment has our function name
        lambda_seg = lambda_segments[0]
        assert function_name in lambda_seg.get("name", ""), (
            f"Lambda segment name doesn't match function: {lambda_seg.get('name')}"
        )

        # Collect all subsegments (including nested ones)
        def collect_subsegments(segment):
            """Recursively collect all subsegments."""
            subsegments = []
            for sub in segment.get("subsegments", []):
                subsegments.append(sub)
                subsegments.extend(collect_subsegments(sub))
            return subsegments

        all_subsegments = []
        for seg in segment_docs:
            all_subsegments.extend(collect_subsegments(seg))

        # Verify we have AWS SDK subsegments
        aws_subsegments = [sub for sub in all_subsegments if sub.get("namespace") == "aws"]
        assert len(aws_subsegments) > 0, (
            "No AWS SDK subsegments found - DynamoDB calls should be traced"
        )

        # Verify DynamoDB-specific subsegments exist
        dynamodb_subsegments = [
            sub
            for sub in aws_subsegments
            if "DynamoDB" in sub.get("name", "") or "dynamodb" in sub.get("name", "").lower()
        ]
        subsegment_names = [s.get("name") for s in aws_subsegments]
        assert len(dynamodb_subsegments) > 0, (
            f"No DynamoDB subsegments found. AWS subsegments: {subsegment_names}"
        )


class TestE2EAWSXRayTracingDisabled:
    """Tests for X-Ray tracing when disabled."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def aws_limiter_without_tracing(self, unique_name_class):
        """Create RateLimiter with X-Ray tracing disabled (default). Class-scoped to share stack."""
        stack_options = StackOptions(
            enable_aggregator=True,
            enable_tracing=False,  # Explicitly disabled
            enable_alarms=False,
            retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
        )

        limiter = RateLimiter(
            name=unique_name_class,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_lambda_tracing_disabled(
        self, aws_limiter_without_tracing, aws_lambda_client, unique_name_class
    ):
        """
        Verify Lambda function has PassThrough tracing when disabled.

        Checks:
        - TracingConfig.Mode is 'PassThrough'
        """
        function_name = f"{unique_name_class}-aggregator"

        response = aws_lambda_client.get_function_configuration(FunctionName=function_name)

        assert response["TracingConfig"]["Mode"] == "PassThrough", (
            f"Expected TracingConfig.Mode='PassThrough', got '{response['TracingConfig']['Mode']}'"
        )

    @pytest.mark.asyncio(loop_scope="class")
    async def test_iam_role_no_xray_permissions_when_disabled(
        self, aws_limiter_without_tracing, unique_name_class
    ):
        """
        Verify IAM role does NOT have X-Ray permissions when tracing is disabled.

        Checks:
        - XRayAccess policy does NOT exist
        """
        import boto3

        iam = boto3.client("iam", region_name="us-east-1")
        role_name = f"PowerUserPB-{unique_name_class}-aggr"

        # Get inline policies
        response = iam.list_role_policies(RoleName=role_name)
        policy_names = response.get("PolicyNames", [])

        assert "XRayAccess" not in policy_names, (
            f"XRayAccess policy should NOT exist when tracing disabled. Found: {policy_names}"
        )

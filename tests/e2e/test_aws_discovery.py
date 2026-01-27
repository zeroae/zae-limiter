"""End-to-end tests for tag-based discovery on real AWS.

These tests validate:
- Tag-based discovery via Resource Groups Tagging API
- describe_stacks fallback discovery
- Dual discovery de-duplication
- New-style (non-prefixed) stacks are discoverable
- ensure_tags applies ManagedBy and zae-limiter:name tags

IMPORTANT: These tests require:
1. Valid AWS credentials (AWS_PROFILE=zeroae-code/AWSPowerUserAccess)
2. The --run-aws pytest flag

To run:
    AWS_PROFILE=zeroae-code/AWSPowerUserAccess \
      uv run pytest tests/e2e/test_aws_discovery.py --run-aws -v

WARNING: These tests create real AWS resources and may incur charges.
Resources are cleaned up after tests, but verify via AWS Console.
"""

import pytest

from zae_limiter import RateLimiter, StackOptions
from zae_limiter.infra.discovery import InfrastructureDiscovery
from zae_limiter.infra.stack_manager import (
    MANAGED_BY_TAG_KEY,
    MANAGED_BY_TAG_VALUE,
    NAME_TAG_KEY,
    VERSION_TAG_KEY,
)

pytestmark = [pytest.mark.aws, pytest.mark.e2e]

REGION = "us-east-1"
PERMISSION_BOUNDARY = "arn:aws:iam::aws:policy/PowerUserAccess"
ROLE_NAME_FORMAT = "PowerUserPB-{}"


@pytest.fixture(scope="class")
def discovery_name():
    """Short unique name that fits IAM role name limits with PowerUserPB- prefix."""
    import time
    import uuid

    unique_id = uuid.uuid4().hex[:6]
    return f"disc-{int(time.time()) % 100000}-{unique_id}"


class TestTagBasedDiscovery:
    """E2E tests for tag-based resource discovery on real AWS."""

    @pytest.fixture(scope="class")
    async def deployed_limiter(self, discovery_name):
        """Deploy a minimal stack (no aggregator) for discovery tests."""
        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            permission_boundary=PERMISSION_BOUNDARY,
            role_name_format=ROLE_NAME_FORMAT,
        )

        limiter = RateLimiter(
            name=discovery_name,
            region=REGION,
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    async def test_stack_has_managed_by_tag(self, deployed_limiter, discovery_name):
        """Deployed stack has ManagedBy=zae-limiter tag."""
        import boto3

        cfn = boto3.client("cloudformation", region_name=REGION)
        response = cfn.describe_stacks(StackName=discovery_name)
        tags = {t["Key"]: t["Value"] for t in response["Stacks"][0].get("Tags", [])}

        assert tags.get(MANAGED_BY_TAG_KEY) == MANAGED_BY_TAG_VALUE

    @pytest.mark.asyncio
    async def test_stack_has_name_tag(self, deployed_limiter, discovery_name):
        """Deployed stack has zae-limiter:name tag matching user name."""
        import boto3

        cfn = boto3.client("cloudformation", region_name=REGION)
        response = cfn.describe_stacks(StackName=discovery_name)
        tags = {t["Key"]: t["Value"] for t in response["Stacks"][0].get("Tags", [])}

        assert tags.get(NAME_TAG_KEY) == discovery_name

    @pytest.mark.asyncio
    async def test_stack_has_version_tag(self, deployed_limiter, discovery_name):
        """Deployed stack has zae-limiter:version tag."""
        import boto3

        cfn = boto3.client("cloudformation", region_name=REGION)
        response = cfn.describe_stacks(StackName=discovery_name)
        tags = {t["Key"]: t["Value"] for t in response["Stacks"][0].get("Tags", [])}

        assert tags.get(VERSION_TAG_KEY) is not None
        assert len(tags[VERSION_TAG_KEY]) > 0

    @pytest.mark.asyncio
    async def test_tagging_api_discovers_stack(self, deployed_limiter, discovery_name):
        """Resource Groups Tagging API finds the deployed stack."""
        async with InfrastructureDiscovery(region=REGION) as discovery:
            limiters = await discovery._discover_via_tagging_api()

        stack_names = [lim.stack_name for lim in limiters]
        assert discovery_name in stack_names

    @pytest.mark.asyncio
    async def test_describe_stacks_discovers_stack(self, deployed_limiter, discovery_name):
        """describe_stacks fallback finds the deployed stack via tags."""
        async with InfrastructureDiscovery(region=REGION) as discovery:
            limiters = await discovery._discover_via_describe_stacks()

        stack_names = [lim.stack_name for lim in limiters]
        assert discovery_name in stack_names

    @pytest.mark.asyncio
    async def test_list_limiters_discovers_stack(self, deployed_limiter, discovery_name):
        """list_limiters (dual discovery) finds the deployed stack."""
        async with InfrastructureDiscovery(region=REGION) as discovery:
            limiters = await discovery.list_limiters()

        found = [lim for lim in limiters if lim.stack_name == discovery_name]
        assert len(found) == 1

        info = found[0]
        assert info.user_name == discovery_name
        assert info.stack_status == "CREATE_COMPLETE"
        assert info.version is not None
        assert info.region == REGION

    @pytest.mark.asyncio
    async def test_list_deployed_class_method(self, deployed_limiter, discovery_name):
        """RateLimiter.list_deployed() finds the deployed stack."""
        limiters = await RateLimiter.list_deployed(region=REGION)

        found = [lim for lim in limiters if lim.stack_name == discovery_name]
        assert len(found) == 1
        assert found[0].user_name == discovery_name

    @pytest.mark.asyncio
    async def test_deduplication_returns_single_result(self, deployed_limiter, discovery_name):
        """list_limiters de-duplicates when both methods find the same stack."""
        async with InfrastructureDiscovery(region=REGION) as discovery:
            limiters = await discovery.list_limiters()

        matches = [lim for lim in limiters if lim.stack_name == discovery_name]
        assert len(matches) == 1


class TestDiscoveryWithUserTags:
    """E2E tests for discovery with user-defined tags."""

    @pytest.fixture(scope="class")
    async def tagged_limiter(self, discovery_name):
        """Deploy a stack with user-defined tags."""
        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            permission_boundary=PERMISSION_BOUNDARY,
            role_name_format=ROLE_NAME_FORMAT,
            tags={"env": "test", "team": "platform"},
        )

        limiter = RateLimiter(
            name=discovery_name,
            region=REGION,
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    async def test_user_tags_applied_alongside_managed_tags(self, tagged_limiter, discovery_name):
        """User-defined tags coexist with managed discovery tags."""
        import boto3

        cfn = boto3.client("cloudformation", region_name=REGION)
        response = cfn.describe_stacks(StackName=discovery_name)
        tags = {t["Key"]: t["Value"] for t in response["Stacks"][0].get("Tags", [])}

        # Managed tags present
        assert tags.get(MANAGED_BY_TAG_KEY) == MANAGED_BY_TAG_VALUE
        assert tags.get(NAME_TAG_KEY) == discovery_name

        # User tags present
        assert tags.get("env") == "test"
        assert tags.get("team") == "platform"

    @pytest.mark.asyncio
    async def test_tagged_stack_discoverable(self, tagged_limiter, discovery_name):
        """Stack with user tags is discoverable via list_limiters."""
        async with InfrastructureDiscovery(region=REGION) as discovery:
            limiters = await discovery.list_limiters()

        found = [lim for lim in limiters if lim.stack_name == discovery_name]
        assert len(found) == 1

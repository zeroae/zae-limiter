"""Tests for SyncRateLimiter."""

import pytest
from botocore.exceptions import ClientError

from zae_limiter import Limit, OnUnavailable, RateLimiterUnavailable, RateLimitExceeded


class TestSyncRateLimiter:
    """Tests for synchronous rate limiter."""

    def test_create_entity(self, sync_limiter):
        """Test creating an entity."""
        entity = sync_limiter.create_entity(
            entity_id="proj-1",
            name="Test Project",
        )
        assert entity.id == "proj-1"

    def test_acquire_success(self, sync_limiter):
        """Test successful rate limit acquisition."""
        limits = [Limit.per_minute("rpm", 100)]

        with sync_limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            assert lease.consumed == {"rpm": 1}

    def test_acquire_exceeds_limit(self, sync_limiter):
        """Test that exceeding limit raises exception."""
        limits = [Limit.per_minute("rpm", 10)]

        with pytest.raises(RateLimitExceeded):
            with sync_limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 20},
            ):
                pass

    def test_lease_adjust(self, sync_limiter):
        """Test adjusting consumption in sync lease."""
        limits = [Limit.per_minute("tpm", 1000)]

        with sync_limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 500},
        ) as lease:
            lease.adjust(tpm=200)
            assert lease.consumed == {"tpm": 700}

    def test_available(self, sync_limiter):
        """Test checking available capacity."""
        limits = [Limit.per_minute("rpm", 100)]

        available = sync_limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        assert available["rpm"] == 100

    def test_set_and_get_limits(self, sync_limiter):
        """Test storing and retrieving limits."""
        limits = [Limit.per_minute("rpm", 100)]
        sync_limiter.set_limits("key-1", limits)

        retrieved = sync_limiter.get_limits("key-1")
        assert len(retrieved) == 1
        assert retrieved[0].name == "rpm"


class TestSyncRateLimiterIsAvailable:
    """Tests for sync is_available() health check method."""

    def test_is_available_returns_true_when_table_exists(self, sync_limiter):
        """is_available should return True when DynamoDB table is reachable."""
        result = sync_limiter.is_available()
        assert result is True

    def test_is_available_returns_false_on_client_error(self, sync_limiter, monkeypatch):
        """is_available should return False when DynamoDB returns error."""

        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
                "GetItem",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "ping", mock_error)
        result = sync_limiter.is_available()
        assert result is False

    def test_is_available_custom_timeout(self, sync_limiter):
        """is_available should respect custom timeout parameter."""
        result = sync_limiter.is_available(timeout=5.0)
        assert result is True

    def test_is_available_returns_false_on_event_loop_error(self, sync_limiter):
        """is_available should return False when event loop fails."""
        original_run = sync_limiter._run

        def mock_run_error(coro):
            # Close the coroutine to avoid warning, then raise
            coro.close()
            raise RuntimeError("Event loop is closed")

        try:
            sync_limiter._run = mock_run_error
            result = sync_limiter.is_available()
            assert result is False
        finally:
            # Restore original _run for fixture cleanup
            sync_limiter._run = original_run


class TestSyncRateLimiterOnUnavailable:
    """Tests for ALLOW vs BLOCK behavior when DynamoDB is unavailable."""

    def test_allow_returns_noop_lease_on_dynamodb_error(self, sync_limiter, monkeypatch):
        """ALLOW should return no-op lease on infrastructure error."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "DynamoDB down"}},
                "GetItem",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set on_unavailable to ALLOW
        sync_limiter._limiter.on_unavailable = OnUnavailable.ALLOW

        # Should not raise, should return no-op lease
        limits = [Limit.per_minute("rpm", 100)]
        with sync_limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            # No-op lease has no entries
            assert len(lease._lease.entries) == 0
            assert lease.consumed == {}

    def test_block_raises_unavailable_on_dynamodb_error(self, sync_limiter, monkeypatch):
        """BLOCK should reject requests when DynamoDB is down."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "Query",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set on_unavailable to BLOCK (default)
        sync_limiter._limiter.on_unavailable = OnUnavailable.BLOCK

        # Should raise RateLimiterUnavailable
        limits = [Limit.per_minute("rpm", 100)]
        with pytest.raises(RateLimiterUnavailable) as exc_info:
            with sync_limiter.acquire(
                entity_id="test-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Verify exception details
        assert exc_info.value.cause is not None
        assert "ProvisionedThroughputExceededException" in str(exc_info.value.cause)

    def test_allow_override_in_acquire_call(self, sync_limiter, monkeypatch):
        """on_unavailable parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError"}},
                "TransactWriteItems",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set limiter to BLOCK, but override in acquire
        sync_limiter._limiter.on_unavailable = OnUnavailable.BLOCK

        limits = [Limit.per_minute("rpm", 100)]
        with sync_limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
            on_unavailable=OnUnavailable.ALLOW,  # Override to ALLOW
        ) as lease:
            # Should get no-op lease due to override
            assert len(lease._lease.entries) == 0

    def test_block_override_in_acquire_call(self, sync_limiter, monkeypatch):
        """on_unavailable parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise Exception("DynamoDB timeout")

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set limiter to ALLOW, but override in acquire
        sync_limiter._limiter.on_unavailable = OnUnavailable.ALLOW

        limits = [Limit.per_minute("rpm", 100)]
        with pytest.raises(RateLimiterUnavailable):
            with sync_limiter.acquire(
                entity_id="test-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                on_unavailable=OnUnavailable.BLOCK,  # Override to BLOCK
            ):
                pass


class TestSyncRateLimiterAudit:
    """Tests for sync audit functionality."""

    def test_get_audit_events_after_create_entity(self, sync_limiter):
        """Test that create_entity logs an audit event."""
        sync_limiter.create_entity(
            entity_id="proj-1",
            name="Test Project",
            principal="admin@example.com",
        )

        events = sync_limiter.get_audit_events("proj-1")
        assert len(events) == 1
        assert events[0].action == "entity_created"
        assert events[0].principal == "admin@example.com"

    def test_get_audit_events_with_principal(self, sync_limiter):
        """Test principal parameter on sync methods."""
        sync_limiter.create_entity(entity_id="proj-1")
        sync_limiter.set_limits(
            "proj-1",
            [Limit.per_minute("rpm", 100)],
            principal="admin",
        )
        sync_limiter.delete_limits("proj-1", principal="admin")

        events = sync_limiter.get_audit_events("proj-1")
        assert len(events) >= 2

    def test_delete_entity_with_principal(self, sync_limiter):
        """Test delete_entity logs with principal."""
        sync_limiter.create_entity(entity_id="proj-1")
        sync_limiter.delete_entity("proj-1", principal="admin")

        events = sync_limiter.get_audit_events("proj-1")
        delete_events = [e for e in events if e.action == "entity_deleted"]
        assert len(delete_events) == 1
        assert delete_events[0].principal == "admin"


class TestSyncRateLimiterResourceLimits:
    """Tests for sync resource-level limit configs."""

    def test_set_and_get_resource_limits(self, sync_limiter):
        """Test storing and retrieving resource-level limits."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        sync_limiter.set_resource_limits("gpt-4", limits)

        retrieved = sync_limiter.get_resource_limits("gpt-4")
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

    def test_delete_resource_limits(self, sync_limiter):
        """Test deleting resource-level limits."""
        limits = [Limit.per_minute("rpm", 100)]
        sync_limiter.set_resource_limits("gpt-4", limits)

        sync_limiter.delete_resource_limits("gpt-4")

        retrieved = sync_limiter.get_resource_limits("gpt-4")
        assert len(retrieved) == 0

    def test_list_resources_with_limits(self, sync_limiter):
        """Test listing resources with configured limits."""
        limits = [Limit.per_minute("rpm", 100)]
        sync_limiter.set_resource_limits("gpt-4", limits)
        sync_limiter.set_resource_limits("claude-3", limits)

        resources = sync_limiter.list_resources_with_limits()
        assert "gpt-4" in resources
        assert "claude-3" in resources


class TestSyncRateLimiterSystemLimits:
    """Tests for sync system-level limit configs."""

    def test_set_and_get_system_limits(self, sync_limiter):
        """Test storing and retrieving system-level limits."""
        limits = [
            Limit.per_minute("rpm", 50),
            Limit.per_minute("tpm", 5_000),
        ]
        sync_limiter.set_system_limits("gpt-4", limits)

        retrieved = sync_limiter.get_system_limits("gpt-4")
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

    def test_delete_system_limits(self, sync_limiter):
        """Test deleting system-level limits."""
        limits = [Limit.per_minute("rpm", 50)]
        sync_limiter.set_system_limits("gpt-4", limits)

        sync_limiter.delete_system_limits("gpt-4")

        retrieved = sync_limiter.get_system_limits("gpt-4")
        assert len(retrieved) == 0

    def test_list_system_resources_with_limits(self, sync_limiter):
        """Test listing resources with system-level defaults."""
        limits = [Limit.per_minute("rpm", 50)]
        sync_limiter.set_system_limits("gpt-4", limits)
        sync_limiter.set_system_limits("claude-3", limits)

        resources = sync_limiter.list_system_resources_with_limits()
        assert "gpt-4" in resources
        assert "claude-3" in resources


class TestSyncRateLimiterUsageSnapshots:
    """Tests for sync usage snapshot queries."""

    @pytest.fixture
    def sync_limiter_with_snapshots(self, sync_limiter):
        """Sync limiter with test usage snapshots."""
        import asyncio

        from zae_limiter import schema

        async def setup():
            repo = sync_limiter._limiter._repository
            client = await repo._get_client()

            snapshots_data = [
                ("entity-1", "gpt-4", "hourly", "2024-01-15T10:00:00Z", {"tpm": 1000}),
                ("entity-1", "gpt-4", "hourly", "2024-01-15T11:00:00Z", {"tpm": 2000}),
            ]

            for entity_id, resource, window_type, window_start, counters in snapshots_data:
                item = {
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_usage(resource, window_start)},
                    "entity_id": {"S": entity_id},
                    "resource": {"S": resource},
                    "window": {"S": window_type},
                    "window_start": {"S": window_start},
                    "total_events": {"N": str(sum(counters.values()))},
                    "GSI2PK": {"S": schema.gsi2_pk_resource(resource)},
                    "GSI2SK": {"S": f"USAGE#{window_start}#{entity_id}"},
                }
                for name, value in counters.items():
                    item[name] = {"N": str(value)}

                await client.put_item(TableName=repo.table_name, Item=item)

        asyncio.get_event_loop().run_until_complete(setup())
        yield sync_limiter

    def test_get_usage_snapshots_sync(self, sync_limiter_with_snapshots):
        """Test sync get_usage_snapshots."""
        snapshots, next_key = sync_limiter_with_snapshots.get_usage_snapshots(entity_id="entity-1")

        assert len(snapshots) == 2
        assert all(s.entity_id == "entity-1" for s in snapshots)
        assert next_key is None

    def test_get_usage_snapshots_with_datetime(self, sync_limiter_with_snapshots):
        """Test sync get_usage_snapshots with datetime parameters."""
        from datetime import datetime

        snapshots, _ = sync_limiter_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            start_time=datetime(2024, 1, 15, 10, 0, 0),
            end_time=datetime(2024, 1, 15, 10, 0, 0),
        )

        assert len(snapshots) == 1
        assert snapshots[0].window_start == "2024-01-15T10:00:00Z"

    def test_get_usage_summary_sync(self, sync_limiter_with_snapshots):
        """Test sync get_usage_summary."""
        summary = sync_limiter_with_snapshots.get_usage_summary(
            entity_id="entity-1",
            resource="gpt-4",
        )

        assert summary.snapshot_count == 2
        assert summary.total["tpm"] == 3000  # 1000 + 2000

    def test_get_usage_snapshots_requires_entity_or_resource(self, sync_limiter_with_snapshots):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            sync_limiter_with_snapshots.get_usage_snapshots()

    def test_get_usage_summary_requires_entity_or_resource(self, sync_limiter_with_snapshots):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            sync_limiter_with_snapshots.get_usage_summary()


class TestSyncRateLimiterListDeployed:
    """Tests for SyncRateLimiter.list_deployed()."""

    def test_list_deployed_returns_list(self):
        """Test that list_deployed returns a list of LimiterInfo."""
        from unittest.mock import AsyncMock, patch

        from zae_limiter import LimiterInfo, SyncRateLimiter

        # Mock the async RateLimiter.list_deployed to return test data
        mock_limiters = [
            LimiterInfo(
                stack_name="ZAEL-test-app",
                user_name="test-app",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:00:00Z",
                version="0.5.0",
                lambda_version="0.5.0",
                schema_version="1.0.0",
            ),
        ]

        with patch(
            "zae_limiter.limiter.RateLimiter.list_deployed",
            new_callable=AsyncMock,
            return_value=mock_limiters,
        ):
            # Call sync wrapper
            result = SyncRateLimiter.list_deployed(region="us-east-1")

        # Verify result
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].user_name == "test-app"
        assert result[0].stack_status == "CREATE_COMPLETE"

    def test_list_deployed_passes_parameters(self):
        """Test that parameters are passed through to async version."""
        from unittest.mock import AsyncMock, patch

        from zae_limiter import SyncRateLimiter

        with patch(
            "zae_limiter.limiter.RateLimiter.list_deployed",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_list:
            # Call with specific parameters
            SyncRateLimiter.list_deployed(
                region="eu-west-1",
                endpoint_url="http://localhost:4566",
            )

            # Verify parameters were passed
            mock_list.assert_called_once_with(
                region="eu-west-1",
                endpoint_url="http://localhost:4566",
            )

    def test_list_deployed_empty_result(self):
        """Test that empty result is handled correctly."""
        from unittest.mock import AsyncMock, patch

        from zae_limiter import SyncRateLimiter

        with patch(
            "zae_limiter.limiter.RateLimiter.list_deployed",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = SyncRateLimiter.list_deployed()

        assert result == []

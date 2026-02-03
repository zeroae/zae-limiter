"""Tests for SyncRateLimiter."""

import pytest
from botocore.exceptions import ClientError

from zae_limiter import (
    Limit,
    RateLimiterUnavailable,
    RateLimitExceeded,
    SyncRateLimiter,
)

# Import OnUnavailable from sync_limiter to avoid type mismatch
# (sync_limiter has its own OnUnavailable enum definition)
from zae_limiter.sync_limiter import OnUnavailable


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

    def test_name_property(self, sync_limiter):
        """Test that the name property returns the stack name."""
        # The name should match the identifier passed to the constructor
        assert sync_limiter.name == "test-rate-limits"


class TestSyncRateLimiterCascade:
    """Tests for cascade functionality (entity-level cascade) via sync API."""

    def test_cascade_consumes_parent(self, sync_limiter):
        """Test that entity with cascade=True consumes from parent too."""
        sync_limiter.create_entity(entity_id="proj-1")
        sync_limiter.create_entity(entity_id="key-1", parent_id="proj-1", cascade=True)

        limits = [Limit.per_minute("rpm", 100)]

        with sync_limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Check both entities have consumed
        child_available = sync_limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        parent_available = sync_limiter.available(
            entity_id="proj-1",
            resource="gpt-4",
            limits=limits,
        )

        assert child_available["rpm"] == 99
        assert parent_available["rpm"] == 99

    def test_no_cascade_by_default(self, sync_limiter):
        """Test that entities without cascade=True do NOT cascade."""
        sync_limiter.create_entity(entity_id="proj-1")
        sync_limiter.create_entity(entity_id="key-1", parent_id="proj-1")

        limits = [Limit.per_minute("rpm", 100)]

        with sync_limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Child consumed, parent should NOT have consumed
        child_available = sync_limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        parent_available = sync_limiter.available(
            entity_id="proj-1",
            resource="gpt-4",
            limits=limits,
        )

        assert child_available["rpm"] == 99
        assert parent_available["rpm"] == 100  # Parent untouched


class TestSyncRateLimiterIsAvailable:
    """Tests for sync is_available() health check method."""

    def test_is_available_returns_true_when_table_exists(self, sync_limiter):
        """is_available should return True when DynamoDB table is reachable."""
        result = sync_limiter.is_available()
        assert result is True

    def test_is_available_returns_false_on_client_error(self, sync_limiter, monkeypatch):
        """is_available should return False when DynamoDB returns error."""

        def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
                "GetItem",
            )

        monkeypatch.setattr(sync_limiter._repository, "ping", mock_error)
        result = sync_limiter.is_available()
        assert result is False

    def test_is_available_custom_timeout(self, sync_limiter):
        """is_available should respect custom timeout parameter."""
        result = sync_limiter.is_available(timeout=5.0)
        assert result is True

    def test_is_available_returns_false_on_ping_exception(self, sync_limiter, monkeypatch):
        """is_available should return False when ping raises exception."""

        def mock_error(*args, **kwargs):
            raise RuntimeError("Connection failed")

        monkeypatch.setattr(sync_limiter._repository, "ping", mock_error)
        result = sync_limiter.is_available()
        assert result is False


class TestSyncRateLimiterOnUnavailable:
    """Tests for ALLOW vs BLOCK behavior when DynamoDB is unavailable."""

    def test_allow_returns_noop_lease_on_dynamodb_error(self, sync_limiter, monkeypatch):
        """ALLOW should return no-op lease on infrastructure error."""

        # Mock repository method to raise error
        def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "DynamoDB down"}},
                "BatchGetItem",
            )

        monkeypatch.setattr(sync_limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Set on_unavailable to ALLOW
        sync_limiter.on_unavailable = OnUnavailable.ALLOW

        # Should not raise, should return no-op lease
        limits = [Limit.per_minute("rpm", 100)]
        with sync_limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            # No-op lease has no entries
            assert len(lease.entries) == 0
            assert lease.consumed == {}

    def test_block_raises_unavailable_on_dynamodb_error(self, sync_limiter, monkeypatch):
        """BLOCK should reject requests when DynamoDB is down."""

        # Mock repository method to raise error
        def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "BatchGetItem",
            )

        monkeypatch.setattr(sync_limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Set on_unavailable to BLOCK (default)
        sync_limiter.on_unavailable = OnUnavailable.BLOCK

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
        def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError"}},
                "BatchGetItem",
            )

        monkeypatch.setattr(sync_limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Set limiter to BLOCK, but override in acquire
        sync_limiter.on_unavailable = OnUnavailable.BLOCK

        limits = [Limit.per_minute("rpm", 100)]
        with sync_limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
            on_unavailable=OnUnavailable.ALLOW,  # Override to ALLOW
        ) as lease:
            # Should get no-op lease due to override
            assert len(lease.entries) == 0

    def test_block_override_in_acquire_call(self, sync_limiter, monkeypatch):
        """on_unavailable parameter should override limiter default."""

        # Mock error
        def mock_error(*args, **kwargs):
            raise Exception("DynamoDB timeout")

        monkeypatch.setattr(sync_limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Set limiter to ALLOW, but override in acquire
        sync_limiter.on_unavailable = OnUnavailable.ALLOW

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


class TestSyncRateLimiterResourceDefaults:
    """Tests for sync resource-level default configs."""

    def test_set_and_get_resource_defaults(self, sync_limiter):
        """Test storing and retrieving resource-level defaults."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        sync_limiter.set_resource_defaults("gpt-4", limits)

        retrieved = sync_limiter.get_resource_defaults("gpt-4")
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

    def test_delete_resource_defaults(self, sync_limiter):
        """Test deleting resource-level defaults."""
        limits = [Limit.per_minute("rpm", 100)]
        sync_limiter.set_resource_defaults("gpt-4", limits)

        sync_limiter.delete_resource_defaults("gpt-4")

        retrieved = sync_limiter.get_resource_defaults("gpt-4")
        assert len(retrieved) == 0

    def test_list_resources_with_defaults(self, sync_limiter):
        """Test listing resources with configured defaults."""
        limits = [Limit.per_minute("rpm", 100)]
        sync_limiter.set_resource_defaults("gpt-4", limits)
        sync_limiter.set_resource_defaults("claude-3", limits)

        resources = sync_limiter.list_resources_with_defaults()
        assert "gpt-4" in resources
        assert "claude-3" in resources


class TestSyncRateLimiterSystemDefaults:
    """Tests for sync system-level default configs."""

    def test_set_and_get_system_defaults(self, sync_limiter):
        """Test storing and retrieving system-level defaults."""
        limits = [
            Limit.per_minute("rpm", 50),
            Limit.per_minute("tpm", 5_000),
        ]
        sync_limiter.set_system_defaults(limits)

        retrieved, on_unavailable = sync_limiter.get_system_defaults()
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}
        assert on_unavailable is None

    def test_set_system_defaults_with_on_unavailable(self, sync_limiter):
        """Test storing system defaults with on_unavailable config."""
        limits = [Limit.per_minute("rpm", 50)]
        sync_limiter.set_system_defaults(limits, on_unavailable=OnUnavailable.ALLOW)

        retrieved, on_unavailable = sync_limiter.get_system_defaults()
        assert len(retrieved) == 1
        # Compare by value since sync_limiter returns its own OnUnavailable type
        assert on_unavailable.value == "allow"

    def test_delete_system_defaults(self, sync_limiter):
        """Test deleting system-level defaults."""
        limits = [Limit.per_minute("rpm", 50)]
        sync_limiter.set_system_defaults(limits)

        sync_limiter.delete_system_defaults()

        retrieved, on_unavailable = sync_limiter.get_system_defaults()
        assert len(retrieved) == 0
        assert on_unavailable is None


class TestSyncRateLimiterUsageSnapshots:
    """Tests for sync usage snapshot queries."""

    @pytest.fixture
    def sync_limiter_with_snapshots(self, sync_limiter):
        """Sync limiter with test usage snapshots."""
        from zae_limiter import schema

        repo = sync_limiter._repository
        client = repo._get_client()

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

            client.put_item(TableName=repo.table_name, Item=item)

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
        from unittest.mock import MagicMock, patch

        from zae_limiter import LimiterInfo

        # Mock data
        mock_limiters = [
            LimiterInfo(
                stack_name="test-app",
                user_name="test-app",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:00:00Z",
                version="0.5.0",
                lambda_version="0.5.0",
                schema_version="1.0.0",
            ),
        ]

        # Mock the SyncInfrastructureDiscovery - use full path to where it's imported
        mock_discovery = MagicMock()
        mock_discovery.list_limiters.return_value = mock_limiters
        mock_discovery.__enter__ = MagicMock(return_value=mock_discovery)
        mock_discovery.__exit__ = MagicMock(return_value=False)

        with patch(
            "zae_limiter.infra.sync_discovery.SyncInfrastructureDiscovery",
            return_value=mock_discovery,
        ):
            # Call sync method
            result = SyncRateLimiter.list_deployed(region="us-east-1")

        # Verify result
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].user_name == "test-app"
        assert result[0].stack_status == "CREATE_COMPLETE"

    def test_list_deployed_passes_parameters(self):
        """Test that parameters are passed through to discovery."""
        from unittest.mock import MagicMock, patch

        # Mock the SyncInfrastructureDiscovery
        mock_discovery = MagicMock()
        mock_discovery.list_limiters.return_value = []
        mock_discovery.__enter__ = MagicMock(return_value=mock_discovery)
        mock_discovery.__exit__ = MagicMock(return_value=False)

        with patch(
            "zae_limiter.infra.sync_discovery.SyncInfrastructureDiscovery",
            return_value=mock_discovery,
        ) as mock_class:
            # Call with specific parameters
            SyncRateLimiter.list_deployed(
                region="eu-west-1",
                endpoint_url="http://localhost:4566",
            )

            # Verify parameters were passed to constructor
            mock_class.assert_called_once_with(
                region="eu-west-1",
                endpoint_url="http://localhost:4566",
            )

    def test_list_deployed_empty_result(self):
        """Test that empty result is handled correctly."""
        from unittest.mock import MagicMock, patch

        # Mock the SyncInfrastructureDiscovery
        mock_discovery = MagicMock()
        mock_discovery.list_limiters.return_value = []
        mock_discovery.__enter__ = MagicMock(return_value=mock_discovery)
        mock_discovery.__exit__ = MagicMock(return_value=False)

        with patch(
            "zae_limiter.infra.sync_discovery.SyncInfrastructureDiscovery",
            return_value=mock_discovery,
        ):
            result = SyncRateLimiter.list_deployed()

        assert result == []


class TestSyncRateLimiterEntityManagement:
    """Tests for SyncRateLimiter entity management methods."""

    def test_get_entity(self, sync_limiter):
        """Test getting an entity by ID."""
        # Create entity first
        sync_limiter.create_entity(entity_id="ent-1", name="Test Entity")

        # Get it back
        entity = sync_limiter.get_entity("ent-1")
        assert entity is not None
        assert entity.id == "ent-1"
        assert entity.name == "Test Entity"

    def test_get_entity_not_found(self, sync_limiter):
        """Test getting a non-existent entity returns None."""
        entity = sync_limiter.get_entity("nonexistent")
        assert entity is None

    def test_delete_entity(self, sync_limiter):
        """Test deleting an entity."""
        sync_limiter.create_entity(entity_id="ent-del", name="To Delete")

        # Verify exists
        assert sync_limiter.get_entity("ent-del") is not None

        # Delete
        sync_limiter.delete_entity("ent-del")

        # Verify gone
        assert sync_limiter.get_entity("ent-del") is None

    def test_get_children(self, sync_limiter):
        """Test getting children of a parent entity."""
        # Create parent
        sync_limiter.create_entity(entity_id="parent-1", name="Parent")

        # Create children
        sync_limiter.create_entity(entity_id="child-1", name="Child 1", parent_id="parent-1")
        sync_limiter.create_entity(entity_id="child-2", name="Child 2", parent_id="parent-1")

        # Get children
        children = sync_limiter.get_children("parent-1")
        assert len(children) == 2
        child_ids = {c.id for c in children}
        assert child_ids == {"child-1", "child-2"}


class TestSyncRateLimiterCapacity:
    """Tests for SyncRateLimiter capacity methods."""

    def test_time_until_available(self, sync_limiter):
        """Test calculating time until capacity is available."""
        limits = [Limit.per_minute("rpm", 100)]

        # Full capacity - should return 0
        wait = sync_limiter.time_until_available(
            entity_id="cap-1",
            resource="api",
            needed={"rpm": 50},
            limits=limits,
        )
        assert wait == 0.0

    def test_time_until_available_with_consumption(self, sync_limiter):
        """Test time_until_available after consuming tokens."""
        limits = [Limit.per_minute("rpm", 10)]

        # Consume all capacity
        with sync_limiter.acquire(
            entity_id="cap-2",
            resource="api",
            limits=limits,
            consume={"rpm": 10},
        ):
            pass

        # Should need to wait for more capacity
        wait = sync_limiter.time_until_available(
            entity_id="cap-2",
            resource="api",
            needed={"rpm": 5},
            limits=limits,
        )
        # Wait time should be positive since we consumed all tokens
        assert wait > 0


class TestSyncRateLimiterRollback:
    """Tests for SyncRateLimiter rollback behavior."""

    def test_acquire_rollback_on_exception(self, sync_limiter):
        """Test that rollback is called when exception occurs in context."""
        # Use per_day to avoid timing flakiness (per_minute refills ~1.67 tokens/sec)
        limits = [Limit.per_day("rpd", 100)]

        # First acquire
        with sync_limiter.acquire(
            entity_id="rollback-1",
            resource="api",
            limits=limits,
            consume={"rpd": 10},
        ):
            pass

        # Check available after first acquire
        available_after_first = sync_limiter.available(
            entity_id="rollback-1",
            resource="api",
            limits=limits,
        )

        # Second acquire that raises exception
        try:
            with sync_limiter.acquire(
                entity_id="rollback-1",
                resource="api",
                limits=limits,
                consume={"rpd": 20},
            ):
                raise ValueError("Simulated error")
        except ValueError:
            pass

        # Check available after rollback - should be same as after first (rollback restores)
        available_after_rollback = sync_limiter.available(
            entity_id="rollback-1",
            resource="api",
            limits=limits,
        )
        # After rollback, the tokens from the failed acquire should be restored
        assert available_after_rollback["rpd"] == available_after_first["rpd"]


class TestSyncRateLimiterThreeTierResolution:
    """Tests for SyncRateLimiter three-tier limit resolution."""

    def test_resolution_system_level(self, sync_limiter):
        """Test that system-level limits are resolved by sync limiter."""
        # Set system defaults
        sync_limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Acquire should resolve limits automatically
        with sync_limiter.acquire(
            entity_id="sync-sys-1",
            resource="api",
            limits=None,  # Auto-resolve
            consume={"rpm": 1},
        ):
            pass

    def test_resolution_resource_level(self, sync_limiter):
        """Test that resource-level limits are resolved by sync limiter."""
        # Set resource defaults
        sync_limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 50)])

        # Acquire should resolve limits from resource config
        with sync_limiter.acquire(
            entity_id="sync-res-1",
            resource="gpt-4",
            limits=None,  # Auto-resolve
            consume={"rpm": 1},
        ):
            pass

    def test_available_uses_resolution(self, sync_limiter):
        """Test that sync available() uses three-tier resolution."""
        # Set resource defaults
        sync_limiter.set_resource_defaults("api", [Limit.per_minute("rpm", 200)])

        # available() should resolve limits
        available = sync_limiter.available(
            entity_id="sync-avail-1",
            resource="api",
            limits=None,  # Auto-resolve
        )
        assert available["rpm"] == 200

    def test_time_until_available_uses_resolution(self, sync_limiter):
        """Test that sync time_until_available() uses three-tier resolution."""
        # Set resource defaults
        sync_limiter.set_resource_defaults("api2", [Limit.per_minute("rpm", 100)])

        # time_until_available() should resolve limits
        wait = sync_limiter.time_until_available(
            entity_id="sync-wait-1",
            resource="api2",
            needed={"rpm": 50},
            limits=None,  # Auto-resolve
        )
        assert wait == 0.0


class TestSyncRateLimiterConfigCache:
    """Tests for sync config cache management methods."""

    def test_get_cache_stats_returns_cache_stats(self, sync_limiter):
        """Test get_cache_stats() returns CacheStats object."""
        from zae_limiter.sync_config_cache import CacheStats as SyncCacheStats

        stats = sync_limiter.get_cache_stats()

        # Use sync CacheStats type since sync limiter uses sync config cache
        assert isinstance(stats, SyncCacheStats)
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.size == 0
        assert stats.ttl_seconds == 60  # Default TTL

    def test_invalidate_config_cache(self, sync_limiter):
        """Test invalidate_config_cache() clears cache entries."""
        from zae_limiter.sync_config_cache import CacheEntry

        # Manually populate the cache to verify invalidation
        entry = CacheEntry(value=[], expires_at=9999999999.0)
        sync_limiter._config_cache._resource_defaults["gpt-4"] = entry

        assert sync_limiter.get_cache_stats().size == 1

        sync_limiter.invalidate_config_cache()

        assert sync_limiter.get_cache_stats().size == 0


class TestSyncListEntitiesWithCustomLimits:
    """Tests for sync list_entities_with_custom_limits."""

    def test_list_entities_with_custom_limits(self, sync_limiter):
        """Test listing entities with custom limits."""
        # Set entity-level limits
        sync_limiter.set_limits("entity-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        sync_limiter.set_limits("entity-2", [Limit.per_minute("rpm", 200)], resource="gpt-4")
        sync_limiter.set_limits("entity-3", [Limit.per_minute("rpm", 50)], resource="claude-3")

        # Query for gpt-4 resource
        entities, cursor = sync_limiter.list_entities_with_custom_limits("gpt-4")

        assert "entity-1" in entities
        assert "entity-2" in entities
        assert "entity-3" not in entities  # Different resource
        assert cursor is None  # No more results

    def test_list_entities_with_custom_limits_empty(self, sync_limiter):
        """Test listing entities when no custom limits exist."""
        entities, cursor = sync_limiter.list_entities_with_custom_limits("nonexistent-resource")

        assert entities == []
        assert cursor is None

    def test_list_entities_with_custom_limits_pagination(self, sync_limiter):
        """Test pagination for listing entities."""
        # Set entity-level limits
        sync_limiter.set_limits("entity-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        sync_limiter.set_limits("entity-2", [Limit.per_minute("rpm", 200)], resource="gpt-4")

        # Query with limit=1
        entities, cursor = sync_limiter.list_entities_with_custom_limits("gpt-4", limit=1)

        assert len(entities) == 1
        # cursor may or may not be None depending on moto behavior


class TestSyncRateLimiterListResourcesWithEntityConfigs:
    """Tests for SyncRateLimiter.list_resources_with_entity_configs (issue #288)."""

    def test_list_resources_with_entity_configs_empty(self, sync_limiter):
        """Returns empty list when no entity configs exist."""
        resources = sync_limiter.list_resources_with_entity_configs()
        assert resources == []

    def test_list_resources_with_entity_configs_returns_resources(self, sync_limiter):
        """Returns resources that have entity-level configs."""
        # Set entity-level limits for multiple resources
        sync_limiter.set_limits("entity-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        sync_limiter.set_limits("entity-2", [Limit.per_minute("rpm", 200)], resource="claude-3")

        resources = sync_limiter.list_resources_with_entity_configs()
        assert set(resources) == {"gpt-4", "claude-3"}

    def test_list_resources_with_entity_configs_after_delete(self, sync_limiter):
        """Resource removed from list after all entity configs deleted."""
        sync_limiter.set_limits("entity-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")

        # Verify resource is listed
        assert "gpt-4" in sync_limiter.list_resources_with_entity_configs()

        # Delete the config
        sync_limiter.delete_limits("entity-1", resource="gpt-4")

        # Verify resource is no longer listed
        assert "gpt-4" not in sync_limiter.list_resources_with_entity_configs()


class TestSyncRateLimiterBucketTTL:
    """Tests for sync bucket TTL configuration (Issue #271)."""

    def test_bucket_ttl_multiplier_default_is_seven(self, sync_limiter):
        """Default bucket_ttl_refill_multiplier is 7 for SyncRateLimiter."""
        assert sync_limiter._bucket_ttl_refill_multiplier == 7

    def test_bucket_ttl_multiplier_custom_value(self, mock_dynamodb):
        """Custom bucket_ttl_refill_multiplier is passed through."""
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(name="test", region="us-east-1")
        repo.create_table()
        limiter = SyncRateLimiter(repository=repo, bucket_ttl_refill_multiplier=14)
        try:
            assert limiter._bucket_ttl_refill_multiplier == 14
        finally:
            limiter.close()

    def test_bucket_ttl_multiplier_zero_disables(self, mock_dynamodb):
        """Setting bucket_ttl_refill_multiplier=0 disables TTL."""
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(name="test", region="us-east-1")
        repo.create_table()
        limiter = SyncRateLimiter(repository=repo, bucket_ttl_refill_multiplier=0)
        try:
            assert limiter._bucket_ttl_refill_multiplier == 0
        finally:
            limiter.close()


class TestSyncBucketLimitSync:
    """Tests for sync bucket synchronization when limits are updated (Issue #294)."""

    def test_bucket_updated_when_limit_changed(self, sync_limiter):
        """Bucket capacity is synced when entity limit is changed via set_limits().

        Behavior (issue #294):
        1. Create entity with rpm=100
        2. Use bucket (creates bucket with capacity=100)
        3. Update limit to rpm=200 - set_limits() syncs bucket
        4. Bucket capacity is now 200
        """
        from zae_limiter.schema import pk_entity, sk_bucket

        # Step 1: Set initial limit (rpm=100)
        sync_limiter.set_limits("user-sync-1", [Limit.per_minute("rpm", 100)], resource="api")

        # Step 2: Use the bucket (creates it with capacity=100)
        with sync_limiter.acquire(
            entity_id="user-sync-1",
            resource="api",
            consume={"rpm": 10},
        ):
            pass

        # Verify bucket was created with capacity=100
        item = sync_limiter._repository._get_item(pk_entity("user-sync-1"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 100000, "Initial capacity should be 100 RPM"

        # Step 3: Update limit to rpm=200 - bucket synced immediately
        sync_limiter.set_limits("user-sync-1", [Limit.per_minute("rpm", 200)], resource="api")

        # Verify bucket capacity was updated immediately (no acquire needed)
        item = sync_limiter._repository._get_item(pk_entity("user-sync-1"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "Bucket capacity should be synced to 200 RPM"

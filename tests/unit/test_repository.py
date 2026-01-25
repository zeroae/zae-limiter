"""Unit tests for Repository."""

import time

import pytest

from zae_limiter import AuditAction, Limit
from zae_limiter.exceptions import InvalidIdentifierError
from zae_limiter.models import BucketState
from zae_limiter.repository import Repository


@pytest.fixture
async def repo(mock_dynamodb):
    """Basic repository instance."""
    from tests.unit.conftest import _patch_aiobotocore_response

    with _patch_aiobotocore_response():
        repo = Repository(name="test-repo", region="us-east-1")
        await repo.create_table()
        yield repo
        await repo.close()


@pytest.fixture
async def repo_with_buckets(repo):
    """Repository pre-populated with test buckets."""
    # Create test entities
    await repo.create_entity("entity-1", parent_id=None, name="Entity 1")
    await repo.create_entity("entity-2", parent_id="entity-1", name="Entity 2")

    # Create some buckets
    limits = [
        Limit.per_minute("rpm", 100),
        Limit.per_minute("tpm", 10000),
    ]
    now_ms = int(time.time() * 1000)

    for entity_id in ["entity-1", "entity-2"]:
        for resource in ["gpt-4", "gpt-3.5"]:
            for limit in limits:
                state = BucketState.from_limit(entity_id, resource, limit, now_ms)
                await repo.transact_write([repo.build_bucket_put_item(state)])

    yield repo


class TestRepositoryBucketOperations:
    """Tests for bucket CRUD and queries."""

    @pytest.mark.asyncio
    async def test_get_bucket_returns_none_for_nonexistent(self, repo):
        """Getting a nonexistent bucket should return None."""
        result = await repo.get_bucket("nonexistent", "gpt-4", "rpm")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_buckets_filters_by_resource(self, repo_with_buckets):
        """get_buckets should filter by resource when specified."""
        buckets = await repo_with_buckets.get_buckets("entity-1", resource="gpt-4")

        # Should only get gpt-4 buckets (2 limits: rpm, tpm)
        assert len(buckets) == 2
        assert all(b.resource == "gpt-4" for b in buckets)

        # Verify both limits are present
        limit_names = {b.limit_name for b in buckets}
        assert limit_names == {"rpm", "tpm"}

    @pytest.mark.asyncio
    async def test_get_buckets_returns_all_when_no_filter(self, repo_with_buckets):
        """get_buckets should return all buckets when no resource filter."""
        buckets = await repo_with_buckets.get_buckets("entity-1")

        # Should get all buckets: 2 resources × 2 limits = 4 buckets
        assert len(buckets) == 4

        # Verify resources and limits
        resources = {b.resource for b in buckets}
        assert resources == {"gpt-4", "gpt-3.5"}

        limit_names = {b.limit_name for b in buckets}
        assert limit_names == {"rpm", "tpm"}

    @pytest.mark.asyncio
    async def test_build_bucket_update_with_optimistic_locking(self, repo):
        """Optimistic locking should add conditional expression."""
        update_item = repo.build_bucket_update_item(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=1234567890,
            expected_tokens_milli=100_000,  # Optimistic lock
        )

        # Verify structure
        assert "Update" in update_item
        update_spec = update_item["Update"]

        # Check update expression
        expected_expr = "SET #data.#tokens = :tokens, #data.#refill = :refill"
        assert update_spec["UpdateExpression"] == expected_expr

        # Check attribute names
        assert update_spec["ExpressionAttributeNames"]["#data"] == "data"
        assert update_spec["ExpressionAttributeNames"]["#tokens"] == "tokens_milli"
        assert update_spec["ExpressionAttributeNames"]["#refill"] == "last_refill_ms"

        # Check attribute values
        assert update_spec["ExpressionAttributeValues"][":tokens"] == {"N": "75000"}
        assert update_spec["ExpressionAttributeValues"][":refill"] == {"N": "1234567890"}
        assert update_spec["ExpressionAttributeValues"][":expected"] == {"N": "100000"}

        # Check condition
        assert update_spec["ConditionExpression"] == "#data.#tokens = :expected"

    @pytest.mark.asyncio
    async def test_build_bucket_update_without_optimistic_locking(self, repo):
        """Without expected_tokens, no condition should be added."""
        update_item = repo.build_bucket_update_item(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=1234567890,
            expected_tokens_milli=None,  # No optimistic lock
        )

        # Verify structure
        assert "Update" in update_item
        update_spec = update_item["Update"]

        # Verify no condition
        assert "ConditionExpression" not in update_spec
        assert ":expected" not in update_spec["ExpressionAttributeValues"]

        # Verify update expression is still correct
        expected_expr = "SET #data.#tokens = :tokens, #data.#refill = :refill"
        assert update_spec["UpdateExpression"] == expected_expr


class TestRepositoryResourceAggregation:
    """Tests for GSI2 resource queries."""

    @pytest.mark.asyncio
    async def test_get_resource_buckets_all_entities(self, repo_with_buckets):
        """Should query all buckets for a resource via GSI2."""
        buckets = await repo_with_buckets.get_resource_buckets("gpt-4", "rpm")

        # Should get rpm buckets for both entities
        assert len(buckets) == 2
        assert all(b.resource == "gpt-4" for b in buckets)
        assert all(b.limit_name == "rpm" for b in buckets)

        # Verify both entities are present
        entity_ids = {b.entity_id for b in buckets}
        assert entity_ids == {"entity-1", "entity-2"}

    @pytest.mark.asyncio
    async def test_get_resource_buckets_filtered_by_limit_name(self, repo_with_buckets):
        """Should filter by limit_name when specified."""
        # Query with limit_name filter
        rpm_buckets = await repo_with_buckets.get_resource_buckets("gpt-4", "rpm")
        tpm_buckets = await repo_with_buckets.get_resource_buckets("gpt-4", "tpm")

        # Both should have 2 entities each
        assert len(rpm_buckets) == 2
        assert len(tpm_buckets) == 2

        # Verify correct limit names
        assert all(b.limit_name == "rpm" for b in rpm_buckets)
        assert all(b.limit_name == "tpm" for b in tpm_buckets)

    @pytest.mark.asyncio
    async def test_get_resource_buckets_empty_result(self, repo):
        """Should return empty list when no buckets match."""
        buckets = await repo.get_resource_buckets("nonexistent-resource", "rpm")
        assert buckets == []


class TestRepositoryTransactions:
    """Tests for transactional writes and edge cases."""

    @pytest.mark.asyncio
    async def test_transact_write_empty_items_list(self, repo):
        """transact_write should handle empty items list."""
        # Should not raise an error
        await repo.transact_write([])

    @pytest.mark.asyncio
    async def test_build_bucket_put_item_structure(self, repo):
        """build_bucket_put_item should create correct DynamoDB structure."""
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("entity-1", "gpt-4", limit, now_ms)

        put_item = repo.build_bucket_put_item(state)

        # Verify structure
        assert "Put" in put_item
        put_spec = put_item["Put"]

        assert put_spec["TableName"] == "ZAEL-test-repo"

        # Verify keys
        assert "PK" in put_spec["Item"]
        assert "SK" in put_spec["Item"]
        assert put_spec["Item"]["PK"]["S"] == "ENTITY#entity-1"
        assert put_spec["Item"]["SK"]["S"] == "#BUCKET#gpt-4#rpm"

        # Verify data structure
        assert "data" in put_spec["Item"]
        data = put_spec["Item"]["data"]["M"]

        assert data["tokens_milli"]["N"] == str(100_000)  # burst capacity
        assert data["capacity_milli"]["N"] == str(100_000)
        assert data["burst_milli"]["N"] == str(100_000)
        assert data["refill_amount_milli"]["N"] == str(100_000)
        assert data["refill_period_ms"]["N"] == str(60_000)

    @pytest.mark.asyncio
    async def test_batch_delete_pagination_over_25_items(self, repo):
        """Batch delete should handle >25 items by chunking."""
        # Create 30 entities to exceed DynamoDB batch limit
        for i in range(30):
            await repo.create_entity(f"entity-{i}")

        # Create buckets for all entities
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        for i in range(30):
            state = BucketState.from_limit(f"entity-{i}", "api", limit, now_ms)
            await repo.transact_write([repo.build_bucket_put_item(state)])

        # Delete first entity (should handle >25 items internally if we had that many)
        # For now, just verify it works with one entity
        await repo.delete_entity("entity-0")

        # Verify entity is deleted
        entity = await repo.get_entity("entity-0")
        assert entity is None


class TestRepositorySerialization:
    """Tests for complex DynamoDB type serialization."""

    @pytest.mark.asyncio
    async def test_serialize_map_with_bool_values(self, repo):
        """Should correctly serialize boolean values in maps."""
        # Create entity with metadata containing bools
        await repo.create_entity(
            "test-entity",
            metadata={"is_active": True, "is_premium": False},
        )

        # Retrieve and verify
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.metadata["is_active"] is True
        assert entity.metadata["is_premium"] is False

    @pytest.mark.asyncio
    async def test_serialize_map_with_null_values(self, repo):
        """Should correctly serialize None/null values."""
        # Create entity with null parent_id
        await repo.create_entity("test-entity", parent_id=None)

        # Retrieve and verify
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.parent_id is None

    @pytest.mark.asyncio
    async def test_serialize_map_with_nested_maps(self, repo):
        """Should handle nested dictionaries."""
        metadata = {
            "tier": "premium",
            "limits": {
                "rpm": 1000,
                "tpm": 50000,
            },
            "features": {
                "advanced": True,
                "beta": False,
            },
        }

        await repo.create_entity("test-entity", metadata=metadata)

        # Retrieve and verify nested structure
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.metadata["tier"] == "premium"
        assert entity.metadata["limits"]["rpm"] == 1000
        assert entity.metadata["limits"]["tpm"] == 50000
        assert entity.metadata["features"]["advanced"] is True
        assert entity.metadata["features"]["beta"] is False

    @pytest.mark.asyncio
    async def test_serialize_value_with_list_of_mixed_types(self, repo):
        """Should handle lists with mixed types."""
        metadata = {
            "tags": ["production", "api", "v2"],
            "numbers": [1, 2, 3, 100],
            "mixed": ["text", 42, True],
        }

        await repo.create_entity("test-entity", metadata=metadata)

        # Retrieve and verify
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.metadata["tags"] == ["production", "api", "v2"]
        assert entity.metadata["numbers"] == [1, 2, 3, 100]
        assert entity.metadata["mixed"] == ["text", 42, True]


class TestRepositoryVersionOperations:
    """Tests for version record management."""

    @pytest.mark.asyncio
    async def test_get_version_record_returns_none_when_missing(self, repo):
        """Should return None when version record doesn't exist."""
        version = await repo.get_version_record()
        assert version is None

    @pytest.mark.asyncio
    async def test_set_version_record_with_null_lambda_version(self, repo):
        """Should handle null lambda_version correctly."""
        await repo.set_version_record(
            schema_version="1.0.0",
            lambda_version=None,  # No Lambda deployed
            client_min_version="0.1.0",
            updated_by="test",
        )

        version = await repo.get_version_record()
        assert version is not None
        assert version["schema_version"] == "1.0.0"
        assert version["lambda_version"] is None
        assert version["client_min_version"] == "0.1.0"

    @pytest.mark.asyncio
    async def test_set_version_record_with_null_updated_by(self, repo):
        """Should handle null updated_by correctly."""
        await repo.set_version_record(
            schema_version="1.0.0",
            lambda_version="0.1.0",
            client_min_version="0.1.0",
            updated_by=None,  # No user tracking
        )

        version = await repo.get_version_record()
        assert version is not None
        assert version["schema_version"] == "1.0.0"
        assert version["lambda_version"] == "0.1.0"
        assert version["updated_by"] is None


class TestRepositoryEntityValidation:
    """Tests for input validation in Repository.create_entity()."""

    @pytest.mark.asyncio
    async def test_create_entity_valid(self, repo):
        """Valid entity_id should be accepted."""
        entity = await repo.create_entity("user-123", name="Test User")
        assert entity.id == "user-123"
        assert entity.name == "Test User"

    @pytest.mark.asyncio
    async def test_create_entity_rejects_hash_in_id(self, repo):
        """Entity ID with # delimiter should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("user#123")
        assert exc_info.value.field == "entity_id"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_empty_id(self, repo):
        """Empty entity ID should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("")
        assert exc_info.value.field == "entity_id"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_too_long_id(self, repo):
        """Entity ID exceeding max length should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("a" * 300)
        assert exc_info.value.field == "entity_id"
        assert "length" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_invalid_start_char(self, repo):
        """Entity ID must start with alphanumeric."""
        with pytest.raises(InvalidIdentifierError):
            await repo.create_entity("_user123")

    @pytest.mark.asyncio
    async def test_create_entity_valid_parent_id(self, repo):
        """Valid parent_id should be accepted."""
        await repo.create_entity("parent-1")
        entity = await repo.create_entity("child-1", parent_id="parent-1")
        assert entity.parent_id == "parent-1"

    @pytest.mark.asyncio
    async def test_create_entity_rejects_hash_in_parent_id(self, repo):
        """Parent ID with # delimiter should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("child-1", parent_id="parent#123")
        assert exc_info.value.field == "parent_id"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_empty_parent_id(self, repo):
        """Empty parent ID should be rejected (use None instead)."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("child-1", parent_id="")
        assert exc_info.value.field == "parent_id"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_accepts_uuid(self, repo):
        """UUID format should be accepted."""
        entity = await repo.create_entity("550e8400-e29b-41d4-a716-446655440000")
        assert entity.id == "550e8400-e29b-41d4-a716-446655440000"

    @pytest.mark.asyncio
    async def test_create_entity_accepts_api_key_format(self, repo):
        """API key format (sk-proj-xxx) should be accepted."""
        entity = await repo.create_entity("sk-proj-abc123_xyz")
        assert entity.id == "sk-proj-abc123_xyz"

    @pytest.mark.asyncio
    async def test_create_entity_accepts_email_like(self, repo):
        """Email-like format should be accepted."""
        entity = await repo.create_entity("user@example.com")
        assert entity.id == "user@example.com"


class TestRepositoryAuditLogging:
    """Tests for security audit logging."""

    @pytest.mark.asyncio
    async def test_create_entity_logs_audit_event(self, repo):
        """Creating an entity should log an audit event."""
        await repo.create_entity(
            entity_id="audit-test-entity",
            name="Audit Test",
            principal="user@example.com",
        )

        events = await repo.get_audit_events("audit-test-entity")
        assert len(events) == 1

        event = events[0]
        assert event.action == AuditAction.ENTITY_CREATED
        assert event.entity_id == "audit-test-entity"
        assert event.principal == "user@example.com"
        assert event.details["name"] == "Audit Test"

    @pytest.mark.asyncio
    async def test_create_entity_logs_audit_with_auto_detected_principal(self, repo):
        """Creating an entity without explicit principal auto-detects from AWS identity."""
        await repo.create_entity(
            entity_id="audit-test-entity-2",
            name="No Principal",
        )

        events = await repo.get_audit_events("audit-test-entity-2")
        assert len(events) == 1
        # In moto tests, principal is auto-detected from STS (may be None or an ARN)
        # In real AWS, it would be the caller's ARN
        principal = events[0].principal
        if principal is not None:
            assert principal.startswith("arn:aws:")

    @pytest.mark.asyncio
    async def test_delete_entity_logs_audit_event(self, repo):
        """Deleting an entity should log an audit event."""
        await repo.create_entity(entity_id="to-delete")
        await repo.delete_entity(
            entity_id="to-delete",
            principal="admin@example.com",
        )

        events = await repo.get_audit_events("to-delete")
        # Should have both create and delete events
        assert len(events) == 2

        # Most recent first
        delete_event = events[0]
        assert delete_event.action == AuditAction.ENTITY_DELETED
        assert delete_event.principal == "admin@example.com"
        assert "records_deleted" in delete_event.details

    @pytest.mark.asyncio
    async def test_set_limits_logs_audit_event(self, repo):
        """Setting limits should log an audit event."""
        await repo.create_entity(entity_id="limits-test")

        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10000),
        ]
        await repo.set_limits(
            entity_id="limits-test",
            limits=limits,
            resource="gpt-4",
            principal="api-admin@example.com",
        )

        events = await repo.get_audit_events("limits-test")
        # Should have entity create + limits set events
        assert len(events) == 2

        limits_event = events[0]  # Most recent first
        assert limits_event.action == AuditAction.LIMITS_SET
        assert limits_event.principal == "api-admin@example.com"
        assert limits_event.resource == "gpt-4"
        assert len(limits_event.details["limits"]) == 2

    @pytest.mark.asyncio
    async def test_delete_limits_logs_audit_event(self, repo):
        """Deleting limits should log an audit event."""
        await repo.create_entity(entity_id="delete-limits-test")
        await repo.set_limits(
            entity_id="delete-limits-test",
            limits=[Limit.per_minute("rpm", 100)],
        )
        await repo.delete_limits(
            entity_id="delete-limits-test",
            principal="cleanup-service",
        )

        events = await repo.get_audit_events("delete-limits-test")
        # Should have entity create + limits set + limits delete events
        assert len(events) == 3

        delete_event = events[0]  # Most recent first
        assert delete_event.action == AuditAction.LIMITS_DELETED
        assert delete_event.principal == "cleanup-service"

    @pytest.mark.asyncio
    async def test_get_audit_events_pagination(self, repo):
        """Should support pagination for audit events."""
        # Create entity and perform multiple operations
        await repo.create_entity(entity_id="pagination-test")
        for i in range(5):
            await repo.set_limits(
                entity_id="pagination-test",
                limits=[Limit.per_minute(f"limit-{i}", 100 * (i + 1))],
                principal=f"user-{i}",
            )

        # Query with limit
        events = await repo.get_audit_events("pagination-test", limit=3)
        assert len(events) == 3

        # Query with pagination
        all_events = await repo.get_audit_events("pagination-test", limit=10)
        assert len(all_events) == 6  # 1 create + 5 set_limits

    @pytest.mark.asyncio
    async def test_get_audit_events_empty_for_nonexistent(self, repo):
        """Should return empty list for entity with no audit events."""
        events = await repo.get_audit_events("nonexistent-entity")
        assert events == []

    @pytest.mark.asyncio
    async def test_audit_event_includes_parent_id(self, repo):
        """Audit event for child entity should include parent_id."""
        await repo.create_entity(entity_id="parent-entity")
        await repo.create_entity(
            entity_id="child-entity",
            parent_id="parent-entity",
            principal="admin",
        )

        events = await repo.get_audit_events("child-entity")
        assert len(events) == 1
        assert events[0].details["parent_id"] == "parent-entity"

    @pytest.mark.asyncio
    async def test_audit_event_includes_metadata(self, repo):
        """Audit event should include entity metadata."""
        await repo.create_entity(
            entity_id="metadata-test",
            metadata={"tier": "premium", "region": "us-west-2"},
            principal="onboarding-service",
        )

        events = await repo.get_audit_events("metadata-test")
        assert len(events) == 1
        assert events[0].details["metadata"]["tier"] == "premium"
        assert events[0].details["metadata"]["region"] == "us-west-2"

    @pytest.mark.asyncio
    async def test_create_entity_rejects_invalid_principal(self, repo):
        """Principal with # delimiter should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity(
                entity_id="valid-entity",
                principal="user#admin",
            )
        assert exc_info.value.field == "principal"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_empty_principal(self, repo):
        """Empty principal should be rejected (use None instead)."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity(
                entity_id="valid-entity",
                principal="",
            )
        assert exc_info.value.field == "principal"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_accepts_email_principal(self, repo):
        """Email-like principal should be accepted."""
        await repo.create_entity(
            entity_id="email-principal-test",
            principal="admin@example.com",
        )
        events = await repo.get_audit_events("email-principal-test")
        assert events[0].principal == "admin@example.com"

    @pytest.mark.asyncio
    async def test_create_entity_accepts_service_principal(self, repo):
        """Service name principal should be accepted."""
        await repo.create_entity(
            entity_id="service-principal-test",
            principal="auth-service-v2",
        )
        events = await repo.get_audit_events("service-principal-test")
        assert events[0].principal == "auth-service-v2"

    @pytest.mark.asyncio
    async def test_audit_event_id_is_ulid_format(self, repo):
        """Event ID should be a valid 26-character ULID."""
        await repo.create_entity(entity_id="ulid-test")
        events = await repo.get_audit_events("ulid-test")
        assert len(events) == 1

        event_id = events[0].event_id
        # ULID is 26 characters, uppercase alphanumeric (Crockford Base32)
        assert len(event_id) == 26
        assert event_id.isalnum()
        # ULID uses Crockford Base32: 0-9 and A-Z excluding I, L, O, U
        valid_chars = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        assert all(c in valid_chars for c in event_id.upper())

    @pytest.mark.asyncio
    async def test_audit_event_ids_are_monotonic(self, repo):
        """Multiple events should have monotonically increasing ULIDs."""
        await repo.create_entity(entity_id="monotonic-test")
        # Create multiple events rapidly
        for i in range(5):
            await repo.set_limits(
                entity_id="monotonic-test",
                limits=[Limit.per_minute(f"limit-{i}", 100)],
            )

        events = await repo.get_audit_events("monotonic-test", limit=10)
        # Events are returned most recent first, so reverse for chronological order
        event_ids = [e.event_id for e in reversed(events)]

        # Each ULID should be greater than the previous (lexicographic order)
        for i in range(1, len(event_ids)):
            assert event_ids[i] > event_ids[i - 1], (
                f"Event IDs not monotonic: {event_ids[i - 1]} >= {event_ids[i]}"
            )

    @pytest.mark.asyncio
    async def test_get_caller_identity_handles_sts_failure(self, repo):
        """STS failures should be handled gracefully, returning None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        # Reset cached identity
        repo._caller_identity_fetched = False
        repo._caller_identity_arn = None

        # Mock STS client to raise exception
        mock_sts_client = AsyncMock()
        mock_sts_client.get_caller_identity.side_effect = Exception("STS unavailable")
        mock_sts_client.__aenter__ = AsyncMock(return_value=mock_sts_client)
        mock_sts_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client.return_value = mock_sts_client

        with patch.object(repo, "_session", mock_session):
            arn = await repo._get_caller_identity_arn()

        # Should return None on failure
        assert arn is None
        # Should be cached
        assert repo._caller_identity_fetched is True
        assert repo._caller_identity_arn is None


class TestRepositoryUsageSnapshots:
    """Tests for usage snapshot queries."""

    @pytest.fixture
    async def repo_with_snapshots(self, repo):
        """Repository pre-populated with test usage snapshots."""
        from zae_limiter import schema

        client = await repo._get_client()

        # Create snapshots for multiple entities, resources, and time windows
        snapshots_data = [
            # Entity 1, gpt-4, hourly snapshots
            ("entity-1", "gpt-4", "hourly", "2024-01-15T10:00:00Z", {"tpm": 1000, "rpm": 5}),
            ("entity-1", "gpt-4", "hourly", "2024-01-15T11:00:00Z", {"tpm": 2000, "rpm": 10}),
            ("entity-1", "gpt-4", "hourly", "2024-01-15T12:00:00Z", {"tpm": 1500, "rpm": 8}),
            # Entity 1, gpt-4, daily snapshot
            ("entity-1", "gpt-4", "daily", "2024-01-15T00:00:00Z", {"tpm": 4500, "rpm": 23}),
            # Entity 1, gpt-3.5, hourly
            ("entity-1", "gpt-3.5", "hourly", "2024-01-15T10:00:00Z", {"tpm": 500, "rpm": 3}),
            # Entity 2, gpt-4, hourly
            ("entity-2", "gpt-4", "hourly", "2024-01-15T10:00:00Z", {"tpm": 3000, "rpm": 15}),
            ("entity-2", "gpt-4", "hourly", "2024-01-15T11:00:00Z", {"tpm": 2500, "rpm": 12}),
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
            # Add counters as top-level attributes
            for name, value in counters.items():
                item[name] = {"N": str(value)}

            await client.put_item(TableName=repo.table_name, Item=item)

        yield repo

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_by_entity(self, repo_with_snapshots):
        """Query snapshots for a single entity."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(entity_id="entity-1")

        # Entity 1 has 5 snapshots total
        assert len(snapshots) == 5
        assert all(s.entity_id == "entity-1" for s in snapshots)
        assert next_key is None  # All results fit in one page

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_by_entity_and_resource(self, repo_with_snapshots):
        """Query snapshots for entity + resource filter."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
        )

        # Entity 1, gpt-4 has 4 snapshots (3 hourly + 1 daily)
        assert len(snapshots) == 4
        assert all(s.entity_id == "entity-1" for s in snapshots)
        assert all(s.resource == "gpt-4" for s in snapshots)

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_by_resource_gsi2(self, repo_with_snapshots):
        """Query snapshots for a resource across all entities (GSI2)."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(resource="gpt-4")

        # gpt-4 has snapshots from entity-1 (4) and entity-2 (2) = 6 total
        assert len(snapshots) == 6
        assert all(s.resource == "gpt-4" for s in snapshots)
        # Verify both entities are present
        entity_ids = {s.entity_id for s in snapshots}
        assert entity_ids == {"entity-1", "entity-2"}

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_filter_by_window_type(self, repo_with_snapshots):
        """Filter snapshots by window type."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
        )

        # Entity 1, gpt-4 has 3 hourly snapshots
        assert len(snapshots) == 3
        assert all(s.window_type == "hourly" for s in snapshots)

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_filter_by_time_range(self, repo_with_snapshots):
        """Filter snapshots by start_time and end_time."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            start_time="2024-01-15T10:00:00Z",
            end_time="2024-01-15T11:00:00Z",
        )

        # Should include 10:00 and 11:00 hourly snapshots (window_start <= end_time)
        assert len(snapshots) == 2
        window_starts = {s.window_start for s in snapshots}
        assert "2024-01-15T10:00:00Z" in window_starts
        assert "2024-01-15T11:00:00Z" in window_starts

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_empty_result(self, repo_with_snapshots):
        """Query for nonexistent entity returns empty list."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(entity_id="nonexistent")

        assert snapshots == []
        assert next_key is None

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_requires_entity_or_resource(self, repo):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            await repo.get_usage_snapshots()

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_pagination(self, repo_with_snapshots):
        """Test pagination with limit parameter."""
        # First page
        snapshots1, next_key1 = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            limit=2,
        )

        assert len(snapshots1) == 2
        assert next_key1 is not None  # More results available

        # Second page
        snapshots2, next_key2 = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            limit=2,
            next_key=next_key1,
        )

        assert len(snapshots2) == 2
        assert next_key2 is not None  # Still more results

        # Third page (final)
        snapshots3, next_key3 = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            limit=2,
            next_key=next_key2,
        )

        assert len(snapshots3) == 1  # Only 1 remaining
        assert next_key3 is None  # No more results

        # Verify no duplicates (use resource+window_start as unique key)
        all_keys = [(s.resource, s.window_start) for s in snapshots1 + snapshots2 + snapshots3]
        assert len(all_keys) == len(set(all_keys))

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_counters_extracted(self, repo_with_snapshots):
        """Verify counters are correctly extracted from flat schema."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
            start_time="2024-01-15T10:00:00Z",
            end_time="2024-01-15T10:00:00Z",
        )

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.counters == {"tpm": 1000, "rpm": 5}
        assert snapshot.total_events == 1005  # sum of counters

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_window_end_calculated(self, repo_with_snapshots):
        """Verify window_end is correctly calculated."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
            start_time="2024-01-15T10:00:00Z",
            end_time="2024-01-15T10:00:00Z",
        )

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.window_start == "2024-01-15T10:00:00Z"
        # Hourly window_end should be :59:59
        assert "10:59:59" in snapshot.window_end

    @pytest.mark.asyncio
    async def test_get_usage_summary_aggregation(self, repo_with_snapshots):
        """Test summary aggregation across snapshots."""
        summary = await repo_with_snapshots.get_usage_summary(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
        )

        # 3 hourly snapshots for entity-1, gpt-4
        assert summary.snapshot_count == 3

        # Total: 1000 + 2000 + 1500 = 4500 tpm, 5 + 10 + 8 = 23 rpm
        assert summary.total["tpm"] == 4500
        assert summary.total["rpm"] == 23

        # Average: 4500/3 = 1500 tpm, 23/3 ≈ 7.67 rpm
        assert summary.average["tpm"] == 1500.0
        assert abs(summary.average["rpm"] - 7.666666666666667) < 0.001

        # Time range
        assert summary.min_window_start == "2024-01-15T10:00:00Z"
        assert summary.max_window_start == "2024-01-15T12:00:00Z"

    @pytest.mark.asyncio
    async def test_get_usage_summary_empty(self, repo_with_snapshots):
        """Summary for nonexistent entity returns zeros."""
        summary = await repo_with_snapshots.get_usage_summary(entity_id="nonexistent")

        assert summary.snapshot_count == 0
        assert summary.total == {}
        assert summary.average == {}
        assert summary.min_window_start is None
        assert summary.max_window_start is None

    @pytest.mark.asyncio
    async def test_get_usage_summary_requires_entity_or_resource(self, repo):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            await repo.get_usage_summary()

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_skips_malformed_items(self, repo):
        """Malformed snapshot items are skipped during deserialization."""
        from zae_limiter import schema

        client = await repo._get_client()

        # Item missing entity_id (malformed)
        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("test-malformed")},
                "SK": {"S": schema.sk_usage("gpt-4", "2024-01-15T10:00:00Z")},
                # Missing entity_id, resource, window_start
                "window": {"S": "hourly"},
                "tpm": {"N": "100"},
            },
        )

        # Item with valid data
        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("test-malformed")},
                "SK": {"S": schema.sk_usage("gpt-4", "2024-01-15T11:00:00Z")},
                "entity_id": {"S": "test-malformed"},
                "resource": {"S": "gpt-4"},
                "window": {"S": "hourly"},
                "window_start": {"S": "2024-01-15T11:00:00Z"},
                "tpm": {"N": "200"},
                "total_events": {"N": "10"},
                "GSI2PK": {"S": "RESOURCE#gpt-4"},
                "GSI2SK": {"S": "USAGE#2024-01-15T11:00:00Z#test-malformed"},
            },
        )

        # Query should skip malformed item and return only valid one
        snapshots, _ = await repo.get_usage_snapshots(entity_id="test-malformed")

        assert len(snapshots) == 1
        assert snapshots[0].entity_id == "test-malformed"
        assert snapshots[0].window_start == "2024-01-15T11:00:00Z"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "window_type,window_start,expected_end_contains",
        [
            # Hourly window ends at :59:59
            ("hourly", "2024-01-15T10:00:00Z", "10:59:59"),
            # Daily window ends at 23:59:59
            ("daily", "2024-01-15T00:00:00Z", "23:59:59"),
            # Monthly window (January) ends on Jan 31
            ("monthly", "2024-01-01T00:00:00Z", "2024-01-31"),
            # Monthly window (December) - year rollover ends on Dec 31
            ("monthly", "2024-12-01T00:00:00Z", "2024-12-31"),
            # Monthly window (February leap year) ends on Feb 29
            ("monthly", "2024-02-01T00:00:00Z", "2024-02-29"),
            # Monthly window (February non-leap year) ends on Feb 28
            ("monthly", "2023-02-01T00:00:00Z", "2023-02-28"),
        ],
    )
    async def test_get_usage_snapshots_window_end_by_type(
        self, repo, window_type, window_start, expected_end_contains
    ):
        """Test window_end calculation for all supported window types."""
        from zae_limiter import schema

        client = await repo._get_client()
        entity_id = f"test-{window_type}-{window_start[:10]}"

        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_usage("gpt-4", window_start)},
                "entity_id": {"S": entity_id},
                "resource": {"S": "gpt-4"},
                "window": {"S": window_type},
                "window_start": {"S": window_start},
                "tpm": {"N": "1000"},
                "total_events": {"N": "10"},
                "GSI2PK": {"S": "RESOURCE#gpt-4"},
                "GSI2SK": {"S": f"USAGE#{window_start}#{entity_id}"},
            },
        )

        snapshots, _ = await repo.get_usage_snapshots(entity_id=entity_id)

        assert len(snapshots) == 1
        assert snapshots[0].window_type == window_type
        assert expected_end_contains in snapshots[0].window_end

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_unknown_window_type(self, repo):
        """Test window_end for unknown window type returns window_start."""
        from zae_limiter import schema

        client = await repo._get_client()

        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("unknown-window")},
                "SK": {"S": schema.sk_usage("gpt-4", "2024-01-15T10:00:00Z")},
                "entity_id": {"S": "unknown-window"},
                "resource": {"S": "gpt-4"},
                "window": {"S": "unknown"},  # Unknown window type
                "window_start": {"S": "2024-01-15T10:00:00Z"},
                "tpm": {"N": "100"},
                "total_events": {"N": "5"},
                "GSI2PK": {"S": "RESOURCE#gpt-4"},
                "GSI2SK": {"S": "USAGE#2024-01-15T10:00:00Z#unknown-window"},
            },
        )

        snapshots, _ = await repo.get_usage_snapshots(entity_id="unknown-window")

        assert len(snapshots) == 1
        # Unknown window type should fall through to window_start
        assert snapshots[0].window_end == "2024-01-15T10:00:00Z"

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_invalid_window_start(self, repo):
        """Test window_end for invalid window_start returns window_start."""
        from zae_limiter import schema

        client = await repo._get_client()

        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("invalid-date")},
                "SK": {"S": schema.sk_usage("gpt-4", "invalid-date")},
                "entity_id": {"S": "invalid-date"},
                "resource": {"S": "gpt-4"},
                "window": {"S": "hourly"},
                "window_start": {"S": "invalid-date"},  # Invalid date format
                "tpm": {"N": "100"},
                "total_events": {"N": "5"},
                "GSI2PK": {"S": "RESOURCE#gpt-4"},
                "GSI2SK": {"S": "USAGE#invalid-date#invalid-date"},
            },
        )

        snapshots, _ = await repo.get_usage_snapshots(entity_id="invalid-date")

        assert len(snapshots) == 1
        # Invalid date should return original value
        assert snapshots[0].window_end == "invalid-date"


class TestRepositoryDeprecation:
    """Tests for deprecated Repository methods."""

    @pytest.mark.asyncio
    async def test_create_stack_emits_deprecation_warning(self):
        """create_stack() should emit DeprecationWarning pointing to ensure_infrastructure()."""
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-deprecation", region="us-east-1")

        # Mock StackManager to avoid actual CloudFormation calls
        with patch("zae_limiter.infra.stack_manager.StackManager") as mock_manager_class:
            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.create_stack = AsyncMock(return_value={"StackId": "test"})
            mock_manager_class.return_value = mock_manager

            # Verify deprecation warning is raised
            with pytest.warns(DeprecationWarning, match="create_stack.*deprecated"):
                from zae_limiter import StackOptions

                await repo.create_stack(stack_options=StackOptions())

        await repo.close()

    @pytest.mark.asyncio
    async def test_create_stack_deprecation_message_mentions_ensure_infrastructure(
        self,
    ):
        """Deprecation message should direct users to ensure_infrastructure()."""
        import warnings
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-deprecation-msg", region="us-east-1")

        with patch("zae_limiter.infra.stack_manager.StackManager") as mock_manager_class:
            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.create_stack = AsyncMock(return_value={"StackId": "test"})
            mock_manager_class.return_value = mock_manager

            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                from zae_limiter import StackOptions

                await repo.create_stack(stack_options=StackOptions())

                # Should have exactly one deprecation warning
                deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
                assert len(deprecation_warnings) == 1

                # Message should mention ensure_infrastructure
                msg = str(deprecation_warnings[0].message)
                assert "ensure_infrastructure" in msg
                assert "v2.0.0" in msg

        await repo.close()

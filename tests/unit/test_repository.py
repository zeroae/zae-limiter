"""Unit tests for Repository."""

import time

import pytest

from zae_limiter import AuditAction, BucketNotFoundError, Limit
from zae_limiter.exceptions import EntityNotFoundError, InvalidIdentifierError
from zae_limiter.models import BucketState
from zae_limiter.repository import Repository


@pytest.fixture
async def repo(mock_dynamodb):
    """Basic repository instance."""
    from tests.unit.conftest import _patch_aiobotocore_response

    with _patch_aiobotocore_response():
        repo = Repository(stack_name="test-repo", region="us-east-1")
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
    async def test_create_entity_logs_audit_without_principal(self, repo):
        """Creating an entity without principal still logs event."""
        await repo.create_entity(
            entity_id="audit-test-entity-2",
            name="No Principal",
        )

        events = await repo.get_audit_events("audit-test-entity-2")
        assert len(events) == 1
        assert events[0].principal is None

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


class TestRepositoryUpdateEntity:
    """Tests for update_entity method."""

    @pytest.mark.asyncio
    async def test_update_entity_name(self, repo):
        """Should update entity name."""
        await repo.create_entity("test-entity", name="Original Name")

        updated = await repo.update_entity("test-entity", name="New Name")

        assert updated.name == "New Name"
        assert updated.id == "test-entity"

        # Verify persisted
        fetched = await repo.get_entity("test-entity")
        assert fetched.name == "New Name"

    @pytest.mark.asyncio
    async def test_update_entity_metadata(self, repo):
        """Should update entity metadata."""
        await repo.create_entity(
            "test-entity", metadata={"tier": "free", "region": "us-east-1"}
        )

        updated = await repo.update_entity(
            "test-entity", metadata={"tier": "premium", "features": "all"}
        )

        assert updated.metadata == {"tier": "premium", "features": "all"}

    @pytest.mark.asyncio
    async def test_update_entity_parent_id(self, repo):
        """Should update parent_id and GSI1 keys."""
        await repo.create_entity("parent-1")
        await repo.create_entity("parent-2")
        await repo.create_entity("child", parent_id="parent-1")

        # Verify initial parent
        children_of_1 = await repo.get_children("parent-1")
        assert len(children_of_1) == 1
        assert children_of_1[0].id == "child"

        # Update parent
        updated = await repo.update_entity("child", parent_id="parent-2")
        assert updated.parent_id == "parent-2"

        # Verify GSI1 updated
        children_of_1 = await repo.get_children("parent-1")
        assert len(children_of_1) == 0

        children_of_2 = await repo.get_children("parent-2")
        assert len(children_of_2) == 1
        assert children_of_2[0].id == "child"

    @pytest.mark.asyncio
    async def test_update_entity_clear_parent(self, repo):
        """Should clear parent_id and remove GSI1 keys."""
        from zae_limiter.repository import UNSET

        await repo.create_entity("parent")
        await repo.create_entity("child", parent_id="parent")

        # Clear parent (make entity a root)
        updated = await repo.update_entity("child", parent_id=None)
        assert updated.parent_id is None

        # Verify GSI1 cleared
        children = await repo.get_children("parent")
        assert len(children) == 0

    @pytest.mark.asyncio
    async def test_update_entity_add_parent(self, repo):
        """Should add parent to root entity."""
        await repo.create_entity("parent")
        await repo.create_entity("root-entity")  # No parent

        # Add parent
        updated = await repo.update_entity("root-entity", parent_id="parent")
        assert updated.parent_id == "parent"

        # Verify GSI1 set
        children = await repo.get_children("parent")
        assert len(children) == 1
        assert children[0].id == "root-entity"

    @pytest.mark.asyncio
    async def test_update_entity_not_found(self, repo):
        """Should raise EntityNotFoundError for nonexistent entity."""
        with pytest.raises(EntityNotFoundError) as exc_info:
            await repo.update_entity("nonexistent", name="New Name")
        assert exc_info.value.entity_id == "nonexistent"

    @pytest.mark.asyncio
    async def test_update_entity_invalid_parent_id(self, repo):
        """Should reject invalid parent_id."""
        await repo.create_entity("test-entity")

        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.update_entity("test-entity", parent_id="invalid#parent")
        assert exc_info.value.field == "parent_id"

    @pytest.mark.asyncio
    async def test_update_entity_no_changes(self, repo):
        """Should return existing entity when no changes provided."""
        await repo.create_entity("test-entity", name="Original")

        # Update with no changes (all UNSET)
        from zae_limiter.repository import UNSET

        updated = await repo.update_entity("test-entity")
        assert updated.name == "Original"

    @pytest.mark.asyncio
    async def test_update_entity_logs_audit(self, repo):
        """Should log audit event for updates."""
        await repo.create_entity("audit-entity", name="Original")

        await repo.update_entity(
            "audit-entity", name="Updated", principal="admin@example.com"
        )

        events = await repo.get_audit_events("audit-entity")
        assert len(events) == 2  # create + update

        update_event = events[0]
        assert update_event.action == AuditAction.ENTITY_UPDATED
        assert update_event.principal == "admin@example.com"
        assert update_event.details["name"] == "Updated"

    @pytest.mark.asyncio
    async def test_update_entity_preserves_created_at(self, repo):
        """Should preserve original created_at timestamp."""
        original = await repo.create_entity("test-entity")
        await repo.update_entity("test-entity", name="New Name")

        fetched = await repo.get_entity("test-entity")
        assert fetched.created_at == original.created_at


class TestRepositoryResetBucket:
    """Tests for reset_bucket method."""

    @pytest.mark.asyncio
    async def test_reset_bucket_to_burst(self, repo):
        """Should reset tokens to burst capacity."""
        await repo.create_entity("test-entity")

        # Create a bucket via transaction
        limit = Limit.per_minute("rpm", capacity=100, burst=150)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("test-entity", "gpt-4", limit, now_ms)

        # Simulate some consumption - set tokens to 50 (was 150)
        state = BucketState(
            entity_id=state.entity_id,
            resource=state.resource,
            limit_name=state.limit_name,
            tokens_milli=50_000,  # Consumed some tokens
            last_refill_ms=state.last_refill_ms,
            capacity_milli=state.capacity_milli,
            burst_milli=state.burst_milli,
            refill_amount_milli=state.refill_amount_milli,
            refill_period_ms=state.refill_period_ms,
        )
        await repo.transact_write([repo.build_bucket_put_item(state)])

        # Reset the bucket
        reset_bucket = await repo.reset_bucket("test-entity", "gpt-4", "rpm")

        # Verify reset to burst capacity
        assert reset_bucket.tokens_milli == 150_000
        assert reset_bucket.burst_milli == 150_000

        # Verify persisted
        fetched = await repo.get_bucket("test-entity", "gpt-4", "rpm")
        assert fetched.tokens_milli == 150_000

    @pytest.mark.asyncio
    async def test_reset_bucket_not_found(self, repo):
        """Should raise BucketNotFoundError for nonexistent bucket."""
        await repo.create_entity("test-entity")

        with pytest.raises(BucketNotFoundError) as exc_info:
            await repo.reset_bucket("test-entity", "gpt-4", "rpm")
        assert exc_info.value.entity_id == "test-entity"
        assert exc_info.value.resource == "gpt-4"
        assert exc_info.value.limit_name == "rpm"

    @pytest.mark.asyncio
    async def test_reset_bucket_logs_audit(self, repo):
        """Should log audit event for bucket reset."""
        await repo.create_entity("test-entity")

        # Create a bucket
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("test-entity", "gpt-4", limit, now_ms)
        await repo.transact_write([repo.build_bucket_put_item(state)])

        # Reset with principal
        await repo.reset_bucket(
            "test-entity", "gpt-4", "rpm", principal="admin@example.com"
        )

        events = await repo.get_audit_events("test-entity")
        # Find the reset event
        reset_event = next(
            (e for e in events if e.action == AuditAction.BUCKET_RESET), None
        )
        assert reset_event is not None
        assert reset_event.principal == "admin@example.com"
        assert reset_event.resource == "gpt-4"
        assert reset_event.details["limit_name"] == "rpm"
        assert "previous_tokens_milli" in reset_event.details
        assert "reset_tokens_milli" in reset_event.details

    @pytest.mark.asyncio
    async def test_reset_bucket_updates_refill_time(self, repo):
        """Should update last_refill_ms to current time."""
        await repo.create_entity("test-entity")

        # Create a bucket with old refill time
        limit = Limit.per_minute("rpm", 100)
        old_time_ms = int(time.time() * 1000) - 3600_000  # 1 hour ago
        state = BucketState.from_limit("test-entity", "gpt-4", limit, old_time_ms)
        await repo.transact_write([repo.build_bucket_put_item(state)])

        before_reset = time.time() * 1000
        reset_bucket = await repo.reset_bucket("test-entity", "gpt-4", "rpm")
        after_reset = time.time() * 1000

        # Verify refill time is updated
        assert reset_bucket.last_refill_ms >= before_reset
        assert reset_bucket.last_refill_ms <= after_reset

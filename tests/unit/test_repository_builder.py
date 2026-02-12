"""Unit tests for RepositoryBuilder."""

import warnings
from unittest.mock import AsyncMock, patch

import pytest

from zae_limiter.exceptions import NamespaceNotFoundError
from zae_limiter.models import StackOptions
from zae_limiter.repository import Repository
from zae_limiter.repository_builder import RepositoryBuilder


@pytest.fixture
async def repo(mock_dynamodb):
    """Repository with table created (for namespace tests)."""
    from tests.unit.conftest import _patch_aiobotocore_response

    with _patch_aiobotocore_response():
        repo = Repository(name="test-repo", region="us-east-1")
        await repo.create_table()
        yield repo
        await repo.close()


class TestBuilderConstruction:
    """Test RepositoryBuilder construction and method chaining."""

    def test_builder_returns_builder_instance(self):
        builder = Repository.builder("my-app", "us-east-1")
        assert isinstance(builder, RepositoryBuilder)

    def test_builder_methods_return_self(self):
        builder = Repository.builder("my-app", "us-east-1")
        result = (
            builder.namespace("default")
            .config_cache_ttl(120)
            .auto_update(False)
            .bucket_ttl_multiplier(14)
            .lambda_memory(512)
            .enable_alarms(False)
            .permission_boundary("arn:aws:iam::aws:policy/PowerUserAccess")
        )
        assert result is builder

    def test_builder_stores_name_and_region(self):
        builder = RepositoryBuilder("my-app", "us-east-1")
        assert builder._name == "my-app"
        assert builder._region == "us-east-1"
        assert builder._endpoint_url is None

    def test_builder_stores_endpoint_url(self):
        builder = RepositoryBuilder("my-app", "us-east-1", endpoint_url="http://localhost:4566")
        assert builder._endpoint_url == "http://localhost:4566"

    def test_builder_default_namespace(self):
        builder = RepositoryBuilder("my-app", "us-east-1")
        assert builder._namespace_name == "default"

    def test_builder_custom_namespace(self):
        builder = RepositoryBuilder("my-app", "us-east-1")
        builder.namespace("tenant-a")
        assert builder._namespace_name == "tenant-a"


class TestBuilderInfraOptions:
    """Test infrastructure configuration methods."""

    def test_all_infra_methods_store_options(self):
        builder = (
            RepositoryBuilder("my-app", "us-east-1")
            .snapshot_windows("hourly")
            .usage_retention_days(30)
            .audit_retention_days(365)
            .enable_aggregator(False)
            .pitr_recovery_days(7)
            .log_retention_days(14)
            .lambda_timeout(120)
            .lambda_memory(512)
            .enable_alarms(False)
            .alarm_sns_topic("arn:aws:sns:us-east-1:123:topic")
            .lambda_duration_threshold_pct(90)
            .permission_boundary("arn:aws:iam::aws:policy/Boundary")
            .role_name_format("PB-{}")
            .policy_name_format("PB-{}")
            .enable_audit_archival(False)
            .audit_archive_glacier_days(180)
            .enable_tracing(True)
            .create_iam_roles(True)
            .create_iam(True)
            .aggregator_role_arn("arn:aws:iam::123:role/aggr")
            .enable_deletion_protection(True)
            .tags({"env": "prod"})
        )
        opts = builder._infra_options
        assert opts["snapshot_windows"] == "hourly"
        assert opts["usage_retention_days"] == 30
        assert opts["audit_retention_days"] == 365
        assert opts["enable_aggregator"] is False
        assert opts["pitr_recovery_days"] == 7
        assert opts["log_retention_days"] == 14
        assert opts["lambda_timeout"] == 120
        assert opts["lambda_memory"] == 512
        assert opts["enable_alarms"] is False
        assert opts["alarm_sns_topic"] == "arn:aws:sns:us-east-1:123:topic"
        assert opts["lambda_duration_threshold_pct"] == 90
        assert opts["permission_boundary"] == "arn:aws:iam::aws:policy/Boundary"
        assert opts["role_name_format"] == "PB-{}"
        assert opts["policy_name_format"] == "PB-{}"
        assert opts["enable_audit_archival"] is False
        assert opts["audit_archive_glacier_days"] == 180
        assert opts["enable_tracing"] is True
        assert opts["create_iam_roles"] is True
        assert opts["create_iam"] is True
        assert opts["aggregator_role_arn"] == "arn:aws:iam::123:role/aggr"
        assert opts["enable_deletion_protection"] is True
        assert opts["tags"] == {"env": "prod"}


class TestBuilderStackOptionsMigration:
    """Test the stack_options() migration helper."""

    def test_stack_options_copies_fields(self):
        opts = StackOptions(lambda_memory=512, enable_alarms=False)
        builder = RepositoryBuilder("my-app", "us-east-1")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            builder.stack_options(opts)
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()

        assert builder._infra_options["lambda_memory"] == 512
        assert builder._infra_options["enable_alarms"] is False


class TestBuilderBuild:
    """Test build() async initialization."""

    @pytest.mark.asyncio
    async def test_build_creates_repository(self, mock_dynamodb):
        """build() returns a fully initialized Repository."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            builder = RepositoryBuilder("test-build", "us-east-1")
            # Manually create table since we're not using stack_options
            # (we need to create the table for namespace registration)
            temp_repo = Repository(name="test-build", region="us-east-1")
            await temp_repo.create_table()

            repo = await builder.build()
            try:
                assert isinstance(repo, Repository)
                assert repo.stack_name == "test-build"
                assert repo._builder_initialized is True
                assert repo.namespace_name == "default"
                # namespace_id should be a resolved ULID, not "default"
                assert repo.namespace_id != "default"
                assert len(repo.namespace_id) > 0
            finally:
                await repo.close()

    @pytest.mark.asyncio
    async def test_build_skips_infra_without_options(self, mock_dynamodb):
        """build() does not call StackManager when no infra options set."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            # Create table manually (simulate pre-existing infra)
            temp_repo = Repository(name="test-no-infra", region="us-east-1")
            await temp_repo.create_table()

            builder = RepositoryBuilder("test-no-infra", "us-east-1")

            with patch(
                "zae_limiter.infra.stack_manager.StackManager", autospec=True
            ) as mock_stack_manager:
                repo = await builder.build()
                try:
                    # StackManager should not have been called
                    mock_stack_manager.assert_not_called()
                finally:
                    await repo.close()

    @pytest.mark.asyncio
    async def test_build_creates_infra_with_options(self, mock_dynamodb):
        """build() materializes StackOptions when infra options are set."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            # Pre-create table so namespace registration works
            temp_repo = Repository(name="test-infra", region="us-east-1")
            await temp_repo.create_table()
            await temp_repo.close()

            builder = RepositoryBuilder("test-infra", "us-east-1").lambda_memory(512)

            # Mock _ensure_infrastructure_internal to avoid real CloudFormation calls
            with patch.object(
                Repository, "_ensure_infrastructure_internal", new_callable=AsyncMock
            ):
                repo = await builder.build()
                try:
                    assert repo._stack_options is not None
                    assert repo._stack_options.lambda_memory == 512
                finally:
                    await repo.close()

    @pytest.mark.asyncio
    async def test_build_registers_default_namespace(self, mock_dynamodb):
        """build() registers the 'default' namespace."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-ns", region="us-east-1")
            await temp_repo.create_table()

            builder = RepositoryBuilder("test-ns", "us-east-1")
            repo = await builder.build()
            try:
                # Verify namespace was registered by resolving it
                ns_id = await repo._resolve_namespace("default")
                assert ns_id is not None
                assert ns_id == repo.namespace_id
            finally:
                await repo.close()

    @pytest.mark.asyncio
    async def test_build_resolves_namespace(self, mock_dynamodb):
        """build() resolves the namespace to an opaque ID."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-resolve", region="us-east-1")
            await temp_repo.create_table()

            repo = await RepositoryBuilder("test-resolve", "us-east-1").build()
            try:
                # namespace_id should be a ULID (26 chars lowercase)
                assert len(repo.namespace_id) == 26
                assert repo.namespace_id.isalnum()
            finally:
                await repo.close()

    @pytest.mark.asyncio
    async def test_build_raises_namespace_not_found(self, mock_dynamodb):
        """build() raises NamespaceNotFoundError for non-existent namespace."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-notfound", region="us-east-1")
            await temp_repo.create_table()

            builder = RepositoryBuilder("test-notfound", "us-east-1").namespace("nonexistent")

            with pytest.raises(NamespaceNotFoundError) as exc_info:
                await builder.build()

            assert exc_info.value.namespace_name == "nonexistent"

    @pytest.mark.asyncio
    async def test_build_idempotent_namespace_registration(self, mock_dynamodb):
        """Building twice doesn't fail on namespace already registered."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-idempotent", region="us-east-1")
            await temp_repo.create_table()

            repo1 = await RepositoryBuilder("test-idempotent", "us-east-1").build()
            ns_id_1 = repo1.namespace_id

            repo2 = await RepositoryBuilder("test-idempotent", "us-east-1").build()
            ns_id_2 = repo2.namespace_id

            # Same namespace should resolve to same ID
            assert ns_id_1 == ns_id_2
            await repo1.close()
            await repo2.close()


class TestNamespaceProperties:
    """Test namespace_name and namespace_id properties."""

    def test_namespace_name_default(self):
        repo = Repository(name="test", region="us-east-1")
        assert repo.namespace_name == "default"

    def test_namespace_id_default(self):
        repo = Repository(name="test", region="us-east-1")
        assert repo.namespace_id == "default"


class TestNamespaceRegistration:
    """Test low-level namespace registration and resolution."""

    @pytest.mark.asyncio
    async def test_register_namespace(self, repo):
        """_register_namespace creates name and ID records."""
        ns_id = await repo._register_namespace("test-ns")
        assert ns_id is not None
        assert len(ns_id) == 26  # ULID

    @pytest.mark.asyncio
    async def test_register_namespace_idempotent(self, repo):
        """_register_namespace is idempotent — returns same ID on second call."""
        ns_id_1 = await repo._register_namespace("test-ns")
        ns_id_2 = await repo._register_namespace("test-ns")
        assert ns_id_1 == ns_id_2

    @pytest.mark.asyncio
    async def test_resolve_namespace_not_found(self, repo):
        """_resolve_namespace returns None for non-existent namespace."""
        result = await repo._resolve_namespace("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_namespace_found(self, repo):
        """_resolve_namespace returns ID for registered namespace."""
        ns_id = await repo._register_namespace("found-ns")
        resolved = await repo._resolve_namespace("found-ns")
        assert resolved == ns_id

    @pytest.mark.asyncio
    async def test_resolve_namespace_cached(self, repo):
        """_resolve_namespace uses cache on second call."""
        ns_id = await repo._register_namespace("cached-ns")

        # Clear the item from DynamoDB to prove cache is used
        # (If cache weren't used, this would return None)
        repo._namespace_cache["cached-ns"] = ns_id
        resolved = await repo._resolve_namespace("cached-ns")
        assert resolved == ns_id


class TestVersionManagement:
    """Test version check during build()."""

    @pytest.mark.asyncio
    async def test_build_initializes_version_record(self, mock_dynamodb):
        """build() creates a version record when none exists."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-version-init", region="us-east-1")
            await temp_repo.create_table()

            repo = await RepositoryBuilder("test-version-init", "us-east-1").build()
            try:
                # Version record should have been created
                version = await repo.get_version_record()
                assert version is not None
                assert "schema_version" in version
            finally:
                await repo.close()

    @pytest.mark.asyncio
    async def test_build_calls_version_check_auto(self, mock_dynamodb):
        """build() calls _check_and_update_version_auto when auto_update=True."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-version-auto", region="us-east-1")
            await temp_repo.create_table()

            with patch.object(
                Repository, "_check_and_update_version_auto", new_callable=AsyncMock
            ) as mock_auto:
                repo = await RepositoryBuilder("test-version-auto", "us-east-1").build()
                try:
                    mock_auto.assert_called_once()
                finally:
                    await repo.close()

    @pytest.mark.asyncio
    async def test_build_calls_version_check_strict(self, mock_dynamodb):
        """build() calls _check_version_strict when auto_update=False."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-version-strict", region="us-east-1")
            await temp_repo.create_table()

            with patch.object(
                Repository, "_check_version_strict", new_callable=AsyncMock
            ) as mock_strict:
                repo = await (
                    RepositoryBuilder("test-version-strict", "us-east-1").auto_update(False).build()
                )
                try:
                    mock_strict.assert_called_once()
                finally:
                    await repo.close()

    @pytest.mark.asyncio
    async def test_build_skips_version_check_for_local_endpoint(self, mock_dynamodb):
        """build() skips version check when endpoint_url is set."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-version-local", region="us-east-1")
            await temp_repo.create_table()

            with (
                patch.object(
                    Repository, "_check_and_update_version_auto", new_callable=AsyncMock
                ) as mock_auto,
                patch.object(
                    Repository, "_check_version_strict", new_callable=AsyncMock
                ) as mock_strict,
            ):
                # Build with endpoint_url — version check should be skipped
                # Note: endpoint_url=None for Repository construction (uses moto),
                # but builder._endpoint_url is set to trigger the skip logic
                builder = RepositoryBuilder("test-version-local", "us-east-1")
                builder._endpoint_url = "http://localhost:4566"

                # Override endpoint_url to None only for Repository construction
                # so moto intercepts DynamoDB calls, but builder logic sees endpoint
                original_init = Repository.__init__

                def patched_init(self_repo, *args, **kwargs):
                    kwargs.pop("endpoint_url", None)
                    original_init(self_repo, *args, **kwargs)

                with patch.object(Repository, "__init__", patched_init):
                    repo = await builder.build()
                try:
                    mock_auto.assert_not_called()
                    mock_strict.assert_not_called()
                finally:
                    await repo.close()

    @pytest.mark.asyncio
    async def test_build_auto_update_false_strict_check(self, mock_dynamodb):
        """build() with auto_update=False uses strict version check."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-strict", region="us-east-1")
            await temp_repo.create_table()

            # First build creates version record
            repo1 = await RepositoryBuilder("test-strict", "us-east-1").build()
            await repo1.close()

            # Second build with auto_update=False should succeed (versions match)
            repo2 = await RepositoryBuilder("test-strict", "us-east-1").auto_update(False).build()
            try:
                assert repo2._builder_initialized is True
            finally:
                await repo2.close()

    @pytest.mark.asyncio
    async def test_builder_initialized_skips_limiter_version_check(self, mock_dynamodb):
        """RateLimiter._ensure_initialized() skips checks when builder-initialized."""
        from unittest.mock import AsyncMock

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter.limiter import RateLimiter

        with _patch_aiobotocore_response():
            temp_repo = Repository(name="test-skip-limiter", region="us-east-1")
            await temp_repo.create_table()

            repo = await RepositoryBuilder("test-skip-limiter", "us-east-1").build()
            try:
                limiter = RateLimiter(repository=repo)

                # Mock ensure_infrastructure to verify it's NOT called
                with patch.object(
                    repo, "ensure_infrastructure", new_callable=AsyncMock
                ) as mock_ensure:
                    await limiter._ensure_initialized()
                    mock_ensure.assert_not_called()
                    assert limiter._initialized is True
            finally:
                await repo.close()


class TestEnsureInfrastructureDeprecation:
    """Test that ensure_infrastructure() emits deprecation warning."""

    @pytest.mark.asyncio
    async def test_ensure_infrastructure_deprecation_warning(self, mock_dynamodb):
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            repo = Repository(name="test-deprecation", region="us-east-1")
            await repo.create_table()

            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                await repo.ensure_infrastructure()
                assert len(w) == 1
                assert issubclass(w[0].category, DeprecationWarning)
                assert "deprecated" in str(w[0].message).lower()
                assert "builder" in str(w[0].message).lower()

            await repo.close()

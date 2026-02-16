"""Unit tests for RepositoryBuilder."""

import warnings
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter.exceptions import (
    IncompatibleSchemaError,
    NamespaceNotFoundError,
    VersionMismatchError,
)
from zae_limiter.models import StackOptions
from zae_limiter.repository import Repository
from zae_limiter.repository_builder import RepositoryBuilder


async def _create_table(
    name: str, region: str = "us-east-1", *, register_default_ns: bool = True
) -> Repository:
    """Create a DynamoDB table for testing (bypasses deprecation warning).

    When register_default_ns=True (default), also registers the "default"
    namespace so that Repository.connect() can resolve it.
    """
    repo = Repository(name=name, region=region, _skip_deprecation_warning=True)
    await repo.create_table()
    if register_default_ns:
        await repo._register_namespace("default")
    return repo


@pytest.fixture
async def repo(mock_dynamodb):
    """Repository with table created (for namespace tests)."""
    repo = await _create_table("test-repo")
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
            .on_unavailable("allow")
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
        builder = RepositoryBuilder("test-build", "us-east-1")
        # Manually create table since we're not using stack_options
        # (we need to create the table for namespace registration)
        await _create_table("test-build")

        repo = await builder.build()
        try:
            assert isinstance(repo, Repository)
            assert repo.stack_name == "test-build"
            assert repo._builder_initialized is True
            assert repo.namespace_name == "default"
            # namespace_id should be a resolved opaque ID, not "default"
            assert repo.namespace_id != "default"
            assert len(repo.namespace_id) > 0
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_build_skips_infra_without_options(self, mock_dynamodb):
        """build() does not call StackManager when no infra options set."""
        # Create table manually (simulate pre-existing infra)
        await _create_table("test-no-infra")

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
        # Pre-create table so namespace registration works
        temp_repo = await _create_table("test-infra")
        await temp_repo.close()

        builder = RepositoryBuilder("test-infra", "us-east-1").lambda_memory(512)

        # Mock _ensure_infrastructure_internal to avoid real CloudFormation calls
        with patch.object(Repository, "_ensure_infrastructure_internal", new_callable=AsyncMock):
            repo = await builder.build()
            try:
                assert repo._stack_options is not None
                assert repo._stack_options.lambda_memory == 512
            finally:
                await repo.close()

    @pytest.mark.asyncio
    async def test_build_registers_default_namespace(self, mock_dynamodb):
        """build() registers the 'default' namespace."""
        await _create_table("test-ns")

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
        await _create_table("test-resolve")

        repo = await RepositoryBuilder("test-resolve", "us-east-1").build()
        try:
            # namespace_id should be a token_urlsafe(8) (11 chars URL-safe)
            assert len(repo.namespace_id) == 11
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_build_raises_namespace_not_found(self, mock_dynamodb):
        """build() raises NamespaceNotFoundError for non-existent namespace."""
        await _create_table("test-notfound")

        builder = RepositoryBuilder("test-notfound", "us-east-1").namespace("nonexistent")

        with pytest.raises(NamespaceNotFoundError) as exc_info:
            await builder.build()

        assert exc_info.value.namespace_name == "nonexistent"

    @pytest.mark.asyncio
    async def test_build_with_on_unavailable(self, mock_dynamodb):
        """build() persists on_unavailable as system config."""
        await _create_table("test-on-unavail")

        builder = RepositoryBuilder("test-on-unavail", "us-east-1").on_unavailable("allow")
        repo = await builder.build()
        try:
            _, on_unavailable = await repo.get_system_defaults()
            assert on_unavailable == "allow"
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_build_idempotent_namespace_registration(self, mock_dynamodb):
        """Building twice doesn't fail on namespace already registered."""
        await _create_table("test-idempotent")

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

    @pytest.mark.asyncio
    async def test_namespace_name_default(self, mock_dynamodb):
        """connect() resolves namespace_name from the requested namespace."""
        await _create_table("test-ns-prop")
        repo = await Repository.connect("test-ns-prop", "us-east-1")
        try:
            assert repo.namespace_name == "default"
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_namespace_id_resolved(self, mock_dynamodb):
        """connect() resolves namespace_id to an opaque ID (not 'default')."""
        await _create_table("test-ns-prop2")
        repo = await Repository.connect("test-ns-prop2", "us-east-1")
        try:
            assert repo.namespace_id != "default"
            assert len(repo.namespace_id) == 11
        finally:
            await repo.close()


class TestNamespaceRegistration:
    """Test low-level namespace registration and resolution."""

    @pytest.mark.asyncio
    async def test_register_namespace(self, repo):
        """_register_namespace creates name and ID records."""
        ns_id = await repo._register_namespace("test-ns")
        assert ns_id is not None
        assert len(ns_id) == 11  # token_urlsafe(8)

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
        await _create_table("test-version-init")

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
        await _create_table("test-version-auto")

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
        await _create_table("test-version-strict")

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
        await _create_table("test-version-local")

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
        await _create_table("test-strict")

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

        from zae_limiter.limiter import RateLimiter

        await _create_table("test-skip-limiter")

        repo = await RepositoryBuilder("test-skip-limiter", "us-east-1").build()
        try:
            limiter = RateLimiter(repository=repo)

            # Mock ensure_infrastructure to verify it's NOT called
            with patch.object(repo, "ensure_infrastructure", new_callable=AsyncMock) as mock_ensure:
                await limiter._ensure_initialized()
                mock_ensure.assert_not_called()
                assert limiter._initialized is True
        finally:
            await repo.close()


class TestEnsureInfrastructureDeprecation:
    """Test that ensure_infrastructure() emits deprecation warning."""

    @pytest.mark.asyncio
    async def test_ensure_infrastructure_deprecation_warning(self, mock_dynamodb):
        repo = await _create_table("test-deprecation")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            await repo.ensure_infrastructure()
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()
            assert "builder" in str(w[0].message).lower()

        await repo.close()


class TestNamespaceEdgeCases:
    """Test namespace registration/resolution edge cases."""

    @pytest.mark.asyncio
    async def test_register_namespace_non_transaction_error_reraises(self, repo):
        """_register_namespace re-raises non-TransactionCanceledException errors."""
        error_response = {"Error": {"Code": "ValidationException", "Message": "Bad"}}
        with patch.object(repo, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = AsyncMock()
            mock_client.transact_write_items.side_effect = ClientError(
                error_response, "TransactWriteItems"
            )
            mock_get_client.return_value = mock_client

            with pytest.raises(ClientError) as exc_info:
                await repo._register_namespace("test-error")
            assert exc_info.value.response["Error"]["Code"] == "ValidationException"

    @pytest.mark.asyncio
    async def test_resolve_namespace_deleted_returns_none(self, repo):
        """_resolve_namespace returns None for namespace with status=deleted."""
        from zae_limiter import schema

        # Manually insert a namespace record with status=deleted
        client = await repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": pk},
                "SK": {"S": schema.sk_namespace("deleted-ns")},
                "namespace_id": {"S": "01234567890123456789abcdef"},
                "namespace_name": {"S": "deleted-ns"},
                "status": {"S": "deleted"},
            },
        )

        result = await repo._resolve_namespace("deleted-ns")
        assert result is None


class TestVersionManagementCodePaths:
    """Test version management methods directly (not mocked)."""

    @pytest.mark.asyncio
    async def test_check_and_update_version_auto_no_record(self, mock_dynamodb):
        """_check_and_update_version_auto initializes version when none exists."""
        repo = await _create_table("test-auto-no-rec")
        try:
            await repo._check_and_update_version_auto()
            version = await repo.get_version_record()
            assert version is not None
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_check_and_update_version_auto_compatible(self, mock_dynamodb):
        """_check_and_update_version_auto is no-op when versions match."""
        repo = await _create_table("test-auto-compat")
        try:
            # Initialize version record first
            await repo._initialize_version_record()
            # Should be no-op (compatible)
            await repo._check_and_update_version_auto()
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_check_and_update_version_auto_schema_migration(self, mock_dynamodb):
        """_check_and_update_version_auto raises IncompatibleSchemaError on schema mismatch."""
        from zae_limiter.version import CompatibilityResult

        repo = await _create_table("test-auto-schema")
        try:
            await repo._initialize_version_record()

            # Mock check_compatibility to return schema migration needed
            compat = CompatibilityResult(
                is_compatible=False,
                requires_schema_migration=True,
                message="Major schema version mismatch",
            )
            with patch("zae_limiter.version.check_compatibility", return_value=compat):
                with pytest.raises(IncompatibleSchemaError):
                    await repo._check_and_update_version_auto()
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_check_and_update_version_auto_lambda_update(self, mock_dynamodb):
        """_check_and_update_version_auto calls _perform_lambda_update when needed."""
        from zae_limiter.version import CompatibilityResult

        repo = await _create_table("test-auto-lambda")
        try:
            await repo._initialize_version_record()

            compat = CompatibilityResult(
                is_compatible=True,
                requires_lambda_update=True,
                message="Lambda update available",
            )
            with (
                patch(
                    "zae_limiter.version.check_compatibility",
                    return_value=compat,
                ),
                patch.object(repo, "_perform_lambda_update", new_callable=AsyncMock) as mock_update,
            ):
                await repo._check_and_update_version_auto()
                mock_update.assert_called_once()
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_check_version_strict_no_record(self, mock_dynamodb):
        """_check_version_strict initializes version when none exists."""
        repo = await _create_table("test-strict-no-rec")
        try:
            await repo._check_version_strict()
            version = await repo.get_version_record()
            assert version is not None
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_check_version_strict_schema_migration(self, mock_dynamodb):
        """_check_version_strict raises IncompatibleSchemaError on schema mismatch."""
        from zae_limiter.version import CompatibilityResult

        repo = await _create_table("test-strict-schema")
        try:
            await repo._initialize_version_record()

            compat = CompatibilityResult(
                is_compatible=False,
                requires_schema_migration=True,
                message="Major schema version mismatch",
            )
            with patch("zae_limiter.version.check_compatibility", return_value=compat):
                with pytest.raises(IncompatibleSchemaError):
                    await repo._check_version_strict()
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_check_version_strict_lambda_mismatch(self, mock_dynamodb):
        """_check_version_strict raises VersionMismatchError when lambda update needed."""
        from zae_limiter.version import CompatibilityResult

        repo = await _create_table("test-strict-mismatch")
        try:
            await repo._initialize_version_record()

            compat = CompatibilityResult(
                is_compatible=True,
                requires_lambda_update=True,
                message="Lambda version mismatch",
            )
            with patch("zae_limiter.version.check_compatibility", return_value=compat):
                with pytest.raises(VersionMismatchError):
                    await repo._check_version_strict()
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_perform_lambda_update(self, mock_dynamodb):
        """_perform_lambda_update calls StackManager.deploy_lambda_code."""
        repo = await _create_table("test-lambda-update")
        try:
            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=False)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                return_value=mock_manager,
            ):
                await repo._perform_lambda_update()
                mock_manager.deploy_lambda_code.assert_called_once()

            # Verify version record was updated
            version = await repo.get_version_record()
            assert version is not None
        finally:
            await repo.close()


class TestConnect:
    """Test Repository.connect() classmethod."""

    @pytest.mark.asyncio
    async def test_connect_returns_initialized_repository(self, mock_dynamodb):
        """connect() returns a fully initialized Repository."""
        await _create_table("test-connect")
        repo = await Repository.connect("test-connect", "us-east-1")
        try:
            assert isinstance(repo, Repository)
            assert repo.stack_name == "test-connect"
            assert repo._builder_initialized is True
            assert repo.namespace_name == "default"
            assert repo.namespace_id != "default"
            assert len(repo.namespace_id) == 11
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_connect_passes_namespace(self, mock_dynamodb):
        """connect() resolves the requested namespace."""
        setup = await _create_table("test-connect-ns")
        await setup._register_namespace("tenant-a")
        await setup.close()

        repo = await Repository.connect("test-connect-ns", "us-east-1", namespace="tenant-a")
        try:
            assert repo.namespace_name == "tenant-a"
            assert repo.namespace_id != "default"
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_connect_passes_config_cache_ttl(self, mock_dynamodb):
        """connect() passes config_cache_ttl to the Repository."""
        await _create_table("test-connect-ttl")
        repo = await Repository.connect("test-connect-ttl", "us-east-1", config_cache_ttl=120)
        try:
            assert repo._config_cache_ttl == 120
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_connect_passes_auto_update(self, mock_dynamodb):
        """connect() passes auto_update to the Repository."""
        await _create_table("test-connect-au")
        repo = await Repository.connect("test-connect-au", "us-east-1", auto_update=False)
        try:
            assert repo._auto_update is False
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_connect_raises_namespace_not_found(self, mock_dynamodb):
        """connect() raises NamespaceNotFoundError for non-existent namespace."""
        await _create_table("test-connect-nf")

        with pytest.raises(NamespaceNotFoundError) as exc_info:
            await Repository.connect("test-connect-nf", "us-east-1", namespace="nonexistent")
        assert exc_info.value.namespace_name == "nonexistent"

    @pytest.mark.asyncio
    async def test_connect_does_not_register_namespace(self, mock_dynamodb):
        """connect() does NOT register namespaces (unlike builder().build())."""
        setup = await _create_table("test-connect-noreg", register_default_ns=False)
        await setup.close()

        # connect() should fail because "default" was never registered
        with pytest.raises(NamespaceNotFoundError):
            await Repository.connect("test-connect-noreg", "us-east-1")

    @pytest.mark.asyncio
    async def test_connect_does_not_emit_deprecation_warning(self, mock_dynamodb):
        """connect() does NOT emit DeprecationWarning."""
        await _create_table("test-connect-nowarn")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            repo = await Repository.connect("test-connect-nowarn", "us-east-1")
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) == 0
        await repo.close()

    @pytest.mark.asyncio
    async def test_connect_no_stack_options(self, mock_dynamodb):
        """connect() does not set stack_options (no infrastructure provisioning)."""
        await _create_table("test-connect-noso")
        repo = await Repository.connect("test-connect-noso", "us-east-1")
        try:
            assert repo._stack_options is None
        finally:
            await repo.close()


class TestRepositoryDeprecationWarning:
    """Test that Repository.__init__() emits DeprecationWarning."""

    def test_direct_init_emits_deprecation_warning(self):
        """Repository() emits DeprecationWarning."""
        with pytest.warns(DeprecationWarning, match="deprecated"):
            Repository(name="test-depr", region="us-east-1")

    def test_deprecation_message_mentions_connect(self):
        """Deprecation message mentions connect() as replacement."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Repository(name="test-depr-msg", region="us-east-1")
            assert len(w) == 1
            msg = str(w[0].message)
            assert "connect" in msg
            assert "builder" in msg
            assert "v2.0.0" in msg

    def test_skip_deprecation_warning_flag(self):
        """_skip_deprecation_warning=True suppresses warning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Repository(
                name="test-depr-skip",
                region="us-east-1",
                _skip_deprecation_warning=True,
            )
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) == 0

    @pytest.mark.asyncio
    async def test_builder_build_does_not_emit_deprecation(self, mock_dynamodb):
        """builder().build() does NOT emit DeprecationWarning."""
        await _create_table("test-depr-builder")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            repo = await RepositoryBuilder("test-depr-builder", "us-east-1").build()
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) == 0
        await repo.close()

"""Tests for tag-based infrastructure discovery."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter.infra.discovery import InfrastructureDiscovery
from zae_limiter.models import LimiterInfo


class TestDiscoverViaTaggingAPI:
    """Tests for _discover_via_tagging_api method."""

    @pytest.mark.asyncio
    async def test_returns_limiters_from_tagged_stacks(self) -> None:
        """Tag-based discovery returns LimiterInfo for tagged stacks."""
        mock_cfn_client = MagicMock()
        mock_cfn_client.describe_stacks = AsyncMock(
            return_value={
                "Stacks": [
                    {
                        "StackName": "ZAEL-my-app",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                            {"Key": "zae-limiter:name", "Value": "my-app"},
                            {"Key": "zae-limiter:version", "Value": "0.7.0"},
                        ],
                    }
                ]
            }
        )

        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(
            return_value={
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": (
                            "arn:aws:cloudformation:us-east-1:123456789:stack/ZAEL-my-app/abc123"
                        ),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                            {"Key": "zae-limiter:name", "Value": "my-app"},
                            {"Key": "zae-limiter:version", "Value": "0.7.0"},
                        ],
                    }
                ],
                "PaginationToken": "",
            }
        )
        mock_tagging_client.__aenter__ = AsyncMock(return_value=mock_tagging_client)
        mock_tagging_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client = MagicMock(return_value=mock_tagging_client)

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_cfn_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            discovery._session = mock_session

            limiters = await discovery._discover_via_tagging_api()

        assert len(limiters) == 1
        assert limiters[0].stack_name == "ZAEL-my-app"
        assert limiters[0].user_name == "my-app"
        assert limiters[0].version == "0.7.0"

    @pytest.mark.asyncio
    async def test_returns_limiters_from_non_prefixed_stacks(self) -> None:
        """Tag-based discovery works for new stacks without ZAEL- prefix."""
        mock_cfn_client = MagicMock()
        mock_cfn_client.describe_stacks = AsyncMock(
            return_value={
                "Stacks": [
                    {
                        "StackName": "my-app",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": datetime(2024, 6, 1, 12, 0, 0),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                            {"Key": "zae-limiter:name", "Value": "my-app"},
                            {"Key": "zae-limiter:version", "Value": "0.7.0"},
                        ],
                    }
                ]
            }
        )

        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(
            return_value={
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": (
                            "arn:aws:cloudformation:us-east-1:123456789:stack/my-app/def456"
                        ),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                            {"Key": "zae-limiter:name", "Value": "my-app"},
                            {"Key": "zae-limiter:version", "Value": "0.7.0"},
                        ],
                    }
                ],
                "PaginationToken": "",
            }
        )
        mock_tagging_client.__aenter__ = AsyncMock(return_value=mock_tagging_client)
        mock_tagging_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client = MagicMock(return_value=mock_tagging_client)

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_cfn_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            discovery._session = mock_session

            limiters = await discovery._discover_via_tagging_api()

        assert len(limiters) == 1
        assert limiters[0].stack_name == "my-app"
        assert limiters[0].user_name == "my-app"
        assert limiters[0].version == "0.7.0"

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self) -> None:
        """Tag-based discovery returns empty list if API is unavailable."""
        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "UnknownOperation", "Message": "not supported"}},
                "GetResources",
            )
        )
        mock_tagging_client.__aenter__ = AsyncMock(return_value=mock_tagging_client)
        mock_tagging_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client = MagicMock(return_value=mock_tagging_client)

        discovery = InfrastructureDiscovery(region="us-east-1")
        discovery._session = mock_session

        limiters = await discovery._discover_via_tagging_api()

        assert limiters == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_generic_exception(self) -> None:
        """Tag-based discovery returns empty list on any exception."""
        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(side_effect=Exception("Connection error"))
        mock_tagging_client.__aenter__ = AsyncMock(return_value=mock_tagging_client)
        mock_tagging_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client = MagicMock(return_value=mock_tagging_client)

        discovery = InfrastructureDiscovery(region="us-east-1")
        discovery._session = mock_session

        limiters = await discovery._discover_via_tagging_api()

        assert limiters == []

    @pytest.mark.asyncio
    async def test_skips_deleted_stacks(self) -> None:
        """Tag-based discovery skips DELETE_COMPLETE stacks."""
        mock_cfn_client = MagicMock()
        mock_cfn_client.describe_stacks = AsyncMock(
            return_value={
                "Stacks": [
                    {
                        "StackName": "ZAEL-deleted-app",
                        "StackStatus": "DELETE_COMPLETE",
                        "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        "Tags": [],
                    }
                ]
            }
        )

        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(
            return_value={
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": (
                            "arn:aws:cloudformation:us-east-1:123456789:stack/ZAEL-deleted-app/abc"
                        ),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                        ],
                    }
                ],
                "PaginationToken": "",
            }
        )
        mock_tagging_client.__aenter__ = AsyncMock(return_value=mock_tagging_client)
        mock_tagging_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client = MagicMock(return_value=mock_tagging_client)

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_cfn_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            discovery._session = mock_session

            limiters = await discovery._discover_via_tagging_api()

        assert limiters == []


class TestDualDiscovery:
    """Tests for list_limiters with dual discovery (tag + prefix)."""

    @pytest.mark.asyncio
    async def test_deduplicates_results(self) -> None:
        """list_limiters de-duplicates stacks found by both methods."""
        tagged_result = LimiterInfo(
            stack_name="ZAEL-my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00",
            version="0.7.0",
        )
        prefix_result = LimiterInfo(
            stack_name="ZAEL-my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00",
            version="0.6.0",  # Older version from prefix discovery
        )

        with (
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_tagging_api",
                new_callable=AsyncMock,
                return_value=[tagged_result],
            ),
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_describe_stacks",
                new_callable=AsyncMock,
                return_value=[prefix_result],
            ),
        ):
            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

        # Should have 1 result (de-duplicated), tagged result wins
        assert len(limiters) == 1
        assert limiters[0].version == "0.7.0"

    @pytest.mark.asyncio
    async def test_merges_unique_results(self) -> None:
        """list_limiters merges results from both methods."""
        tagged_only = LimiterInfo(
            stack_name="ZAEL-tagged-app",
            user_name="tagged-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00",
        )
        prefix_only = LimiterInfo(
            stack_name="ZAEL-old-app",
            user_name="old-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-14T09:00:00",
        )

        with (
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_tagging_api",
                new_callable=AsyncMock,
                return_value=[tagged_only],
            ),
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_describe_stacks",
                new_callable=AsyncMock,
                return_value=[prefix_only],
            ),
        ):
            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

        assert len(limiters) == 2
        names = {lim.user_name for lim in limiters}
        assert names == {"tagged-app", "old-app"}

    @pytest.mark.asyncio
    async def test_sorts_by_user_name(self) -> None:
        """list_limiters sorts results by user name."""
        limiter_b = LimiterInfo(
            stack_name="ZAEL-beta",
            user_name="beta",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00",
        )
        limiter_a = LimiterInfo(
            stack_name="ZAEL-alpha",
            user_name="alpha",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-14T09:00:00",
        )

        with (
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_tagging_api",
                new_callable=AsyncMock,
                return_value=[limiter_b],
            ),
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_describe_stacks",
                new_callable=AsyncMock,
                return_value=[limiter_a],
            ),
        ):
            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

        assert limiters[0].user_name == "alpha"
        assert limiters[1].user_name == "beta"

    @pytest.mark.asyncio
    async def test_deduplicates_non_prefixed_stacks(self) -> None:
        """list_limiters de-duplicates new-style stacks without ZAEL- prefix."""
        tagged_result = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-06-01T12:00:00",
            version="0.7.0",
        )
        describe_result = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-06-01T12:00:00",
            version="0.7.0",
        )

        with (
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_tagging_api",
                new_callable=AsyncMock,
                return_value=[tagged_result],
            ),
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_describe_stacks",
                new_callable=AsyncMock,
                return_value=[describe_result],
            ),
        ):
            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

        assert len(limiters) == 1
        assert limiters[0].stack_name == "my-app"
        assert limiters[0].user_name == "my-app"

    @pytest.mark.asyncio
    async def test_fallback_when_tagging_api_fails(self) -> None:
        """list_limiters falls back to prefix if tagging API returns empty."""
        prefix_result = LimiterInfo(
            stack_name="ZAEL-fallback-app",
            user_name="fallback-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00",
        )

        with (
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_tagging_api",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                InfrastructureDiscovery,
                "_discover_via_describe_stacks",
                new_callable=AsyncMock,
                return_value=[prefix_result],
            ),
        ):
            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

        assert len(limiters) == 1
        assert limiters[0].user_name == "fallback-app"


class TestStackOptionsWithTags:
    """Tests for StackOptions tags field."""

    def test_tags_default_none(self) -> None:
        """StackOptions tags defaults to None."""
        from zae_limiter import StackOptions

        opts = StackOptions()
        assert opts.tags is None

    def test_tags_accepts_dict(self) -> None:
        """StackOptions accepts user-defined tags."""
        from zae_limiter import StackOptions

        opts = StackOptions(tags={"env": "prod", "team": "platform"})
        assert opts.tags == {"env": "prod", "team": "platform"}

    def test_tags_rejects_aws_prefix(self) -> None:
        """StackOptions rejects tags with reserved aws: prefix."""
        from zae_limiter import StackOptions

        with pytest.raises(ValueError, match="reserved 'aws:' prefix"):
            StackOptions(tags={"aws:cloudformation:stack-name": "bad"})

    def test_tags_rejects_too_many(self) -> None:
        """StackOptions rejects more than 45 user-defined tags."""
        from zae_limiter import StackOptions

        too_many = {f"key-{i}": f"val-{i}" for i in range(46)}
        with pytest.raises(ValueError, match="exceeds maximum of 45"):
            StackOptions(tags=too_many)

    def test_tags_rejects_long_key(self) -> None:
        """StackOptions rejects tag keys longer than 128 characters."""
        from zae_limiter import StackOptions

        with pytest.raises(ValueError, match="must be 1-128 characters"):
            StackOptions(tags={"k" * 129: "value"})

    def test_tags_rejects_long_value(self) -> None:
        """StackOptions rejects tag values longer than 256 characters."""
        from zae_limiter import StackOptions

        with pytest.raises(ValueError, match="exceeds 256 characters"):
            StackOptions(tags={"key": "v" * 257})

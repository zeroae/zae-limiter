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


class TestDiscoverViaTaggingAPIPagination:
    """Tests for pagination and edge cases in _discover_via_tagging_api."""

    @pytest.mark.asyncio
    async def test_handles_pagination(self) -> None:
        """Tag-based discovery handles multiple pages of results."""
        mock_cfn_client = MagicMock()
        mock_cfn_client.describe_stacks = AsyncMock(
            return_value={
                "Stacks": [
                    {
                        "StackName": "my-app",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                            {"Key": "zae-limiter:name", "Value": "my-app"},
                        ],
                    }
                ]
            }
        )

        # First call returns a pagination token, second call returns empty
        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(
            side_effect=[
                {
                    "ResourceTagMappingList": [
                        {
                            "ResourceARN": (
                                "arn:aws:cloudformation:us-east-1:123456789:stack/my-app/abc123"
                            ),
                            "Tags": [
                                {"Key": "ManagedBy", "Value": "zae-limiter"},
                                {"Key": "zae-limiter:name", "Value": "my-app"},
                            ],
                        }
                    ],
                    "PaginationToken": "next-page-token",
                },
                {
                    "ResourceTagMappingList": [],
                    "PaginationToken": "",
                },
            ]
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
        # Verify pagination token was passed in second call
        calls = mock_tagging_client.get_resources.call_args_list
        assert len(calls) == 2
        assert "PaginationToken" not in calls[0].kwargs
        assert calls[1].kwargs["PaginationToken"] == "next-page-token"

    @pytest.mark.asyncio
    async def test_skips_short_arn(self) -> None:
        """Tag-based discovery skips resources with malformed ARNs."""
        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(
            return_value={
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": "arn:aws:cloudformation:us-east-1:123456789",
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
            mock_get_client.return_value = MagicMock()

            discovery = InfrastructureDiscovery(region="us-east-1")
            discovery._session = mock_session

            limiters = await discovery._discover_via_tagging_api()

        assert limiters == []

    @pytest.mark.asyncio
    async def test_passes_endpoint_url_to_tagging_client(self) -> None:
        """Tag-based discovery passes endpoint_url to the tagging API client."""
        mock_tagging_client = MagicMock()
        mock_tagging_client.get_resources = AsyncMock(
            return_value={"ResourceTagMappingList": [], "PaginationToken": ""}
        )
        mock_tagging_client.__aenter__ = AsyncMock(return_value=mock_tagging_client)
        mock_tagging_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client = MagicMock(return_value=mock_tagging_client)

        discovery = InfrastructureDiscovery(
            region="us-east-1", endpoint_url="http://localhost:4566"
        )
        discovery._session = mock_session

        await discovery._discover_via_tagging_api()

        # Verify endpoint_url was passed
        mock_session.client.assert_called_once_with(
            "resourcegroupstaggingapi",
            region_name="us-east-1",
            endpoint_url="http://localhost:4566",
        )


class TestDiscoverViaDescribeStacksEdgeCases:
    """Tests for edge cases in _discover_via_describe_stacks."""

    @pytest.mark.asyncio
    async def test_non_prefixed_tagged_stack_uses_stack_name(self) -> None:
        """Stacks with ManagedBy tag but no name tag and no prefix use stack_name."""
        mock_client = MagicMock()
        mock_client.describe_stacks = AsyncMock(
            return_value={
                "Stacks": [
                    {
                        "StackName": "custom-app",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": datetime(2024, 6, 1, 12, 0, 0),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                        ],
                    }
                ],
            }
        )

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            limiters = await discovery._discover_via_describe_stacks()

        assert len(limiters) == 1
        # No name tag, no ZAEL- prefix => user_name falls back to stack_name
        assert limiters[0].user_name == "custom-app"
        assert limiters[0].stack_name == "custom-app"


class TestDescribeStackAsLimiterInfo:
    """Tests for _describe_stack_as_limiter_info edge cases."""

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_stacks(self) -> None:
        """Returns None when describe_stacks returns empty list."""
        mock_client = MagicMock()
        mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            result = await discovery._describe_stack_as_limiter_info("nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_extracts_tags_from_stack_when_tag_dict_is_none(self) -> None:
        """Extracts tags from describe_stacks response when tag_dict not provided."""
        mock_client = MagicMock()
        mock_client.describe_stacks = AsyncMock(
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

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            # Pass tag_dict=None to force extraction from stack
            result = await discovery._describe_stack_as_limiter_info("my-app", tag_dict=None)

        assert result is not None
        assert result.user_name == "my-app"
        assert result.version == "0.7.0"

    @pytest.mark.asyncio
    async def test_legacy_prefix_stack_no_name_tag(self) -> None:
        """ZAEL- prefixed stack without name tag derives user_name from prefix."""
        mock_client = MagicMock()
        mock_client.describe_stacks = AsyncMock(
            return_value={
                "Stacks": [
                    {
                        "StackName": "ZAEL-old-app",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                        ],
                    }
                ]
            }
        )

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            result = await discovery._describe_stack_as_limiter_info("ZAEL-old-app")

        assert result is not None
        assert result.user_name == "old-app"

    @pytest.mark.asyncio
    async def test_non_prefixed_stack_no_name_tag_uses_stack_name(self) -> None:
        """Non-prefixed stack without name tag uses stack_name as user_name."""
        mock_client = MagicMock()
        mock_client.describe_stacks = AsyncMock(
            return_value={
                "Stacks": [
                    {
                        "StackName": "custom-stack",
                        "StackStatus": "CREATE_COMPLETE",
                        "CreationTime": datetime(2024, 6, 1, 12, 0, 0),
                        "Tags": [
                            {"Key": "ManagedBy", "Value": "zae-limiter"},
                        ],
                    }
                ]
            }
        )

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            result = await discovery._describe_stack_as_limiter_info("custom-stack")

        assert result is not None
        assert result.user_name == "custom-stack"

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self) -> None:
        """Returns None when describe_stacks raises an exception."""
        mock_client = MagicMock()
        mock_client.describe_stacks = AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "ValidationError", "Message": "not found"}},
                "DescribeStacks",
            )
        )

        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            result = await discovery._describe_stack_as_limiter_info("nonexistent")

        assert result is None


class TestClose:
    """Tests for close method."""

    @pytest.mark.asyncio
    async def test_close_handles_client_exit_error(self) -> None:
        """close handles errors from client __aexit__."""
        discovery = InfrastructureDiscovery(region="us-east-1")

        mock_client = MagicMock()
        mock_client.__aexit__ = AsyncMock(side_effect=Exception("Cleanup error"))
        discovery._client = mock_client
        discovery._session = MagicMock()

        # Should not raise - errors are suppressed
        await discovery.close()

        assert discovery._client is None
        assert discovery._session is None

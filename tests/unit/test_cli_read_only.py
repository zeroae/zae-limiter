"""Commands that only read never provision (#648).

Every command below used to connect through ``Repository.open()``, which
deploys a missing stack, registers a missing namespace, writes a missing
version record and pushes Lambda code when the deployed version is behind the
client — all before the command read anything. They now connect through
``cli._connect_read_only()``.

Moto-backed, with every ``StackManager`` mocked, so a write shows up as a
call on the manager, a new table, or a changed item.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import yaml
from botocore.exceptions import ClientError
from click.testing import CliRunner

from zae_limiter.cli import cli
from zae_limiter.repository import Repository
from zae_limiter.version import get_schema_version

TABLE = "rate-limits"
REGION = "us-east-1"
NAMESPACE = "tenant-a"
CLIENT = "0.15.1"
BEHIND = "0.15.0"

# Commands that read one namespace. Each takes -N; the entity/resource
# arguments name things the populated table carries (or reports as absent
# with exit 0).
NAMESPACED_READS: list[list[str]] = [
    ["audit", "list", "-e", "user-1"],
    ["usage", "list", "-e", "user-1"],
    ["usage", "summary", "-e", "user-1"],
    ["resource", "get-defaults", "gpt-4"],
    ["resource", "list"],
    ["system", "get-defaults"],
    ["entity", "show", "user-1"],
    ["entity", "get-limits", "user-1", "--resource", "gpt-4"],
    ["entity", "list", "--with-custom-limits", "gpt-4"],
    ["entity", "list-resources"],
]

# Commands that read the namespace registry itself and take no -N.
REGISTRY_READS: list[list[str]] = [
    ["namespace", "list"],
    ["namespace", "orphans"],
    ["namespace", "show", NAMESPACE],
]

PLAIN_READS = NAMESPACED_READS + REGISTRY_READS


def _ids(commands: list[list[str]]) -> list[str]:
    return [" ".join(c[:2]) for c in commands]


def _record(lambda_version: str | None, **extra: str) -> dict[str, Any]:
    return {
        "schema_version": get_schema_version(),
        "lambda_version": lambda_version,
        "client_min_version": "0.0.0",
        **extra,
    }


async def _setup(*, namespace: bool, record: dict[str, Any] | None) -> None:
    repo = Repository(TABLE, REGION, None, _skip_deprecation_warning=True)
    try:
        await repo.create_table()
        if record is not None:
            await repo.set_version_record(**record)
        if namespace:
            await repo._register_namespace(NAMESPACE)
            scoped = await repo.namespace(NAMESPACE)
            await scoped.create_entity("user-1")
    finally:
        await repo.close()


async def _scan() -> tuple[list[str], list[dict[str, Any]]]:
    repo = Repository(TABLE, REGION, None, _skip_deprecation_warning=True)
    try:
        client = await repo._get_client()
        tables = (await client.list_tables())["TableNames"]
        items = (await client.scan(TableName=TABLE))["Items"] if TABLE in tables else []
        return tables, items
    finally:
        await repo.close()


def _manager() -> Mock:
    manager = Mock()
    for method in ("create_stack", "deploy_lambda_code", "deploy_provisioner_code"):
        setattr(manager, method, AsyncMock(return_value={"status": "deployed"}))
    manager.__aenter__ = AsyncMock(return_value=manager)
    manager.__aexit__ = AsyncMock(return_value=None)
    return manager


def _invoke(args: list[str], *, region: bool = True) -> tuple[Any, Mock]:
    """Run one command with every provisioning route mocked out."""
    stack_manager = Mock(return_value=_manager())
    options = ["--name", TABLE] + (["--region", REGION] if region else [])
    if args[0] not in ("namespace",) and args[:2] not in (["limits", "plan"], ["limits", "diff"]):
        options += ["-N", NAMESPACE]
    with (
        patch("zae_limiter.__version__", CLIENT),
        patch("zae_limiter.infra.stack_manager.StackManager", stack_manager),
        patch("zae_limiter.cli.StackManager", stack_manager),
    ):
        result = CliRunner().invoke(cli, [*args, *options])
    return result, stack_manager


def _assert_nothing_written(stack_manager: Mock, before: tuple[list[str], list[Any]]) -> None:
    stack_manager.assert_not_called()
    stack_manager.return_value.create_stack.assert_not_called()
    stack_manager.return_value.deploy_lambda_code.assert_not_called()
    stack_manager.return_value.deploy_provisioner_code.assert_not_called()
    assert asyncio.run(_scan()) == before


STACK_MISSING = (
    f"Error: Stack '{TABLE}' not found in {REGION}. Deploy it with 'zae-limiter deploy -n {TABLE}'."
)
NAMESPACE_MISSING = (
    f"Error: Namespace '{NAMESPACE}' not found. "
    f"Register it with 'zae-limiter namespace register {NAMESPACE}'."
)


class TestPlainReadsNeverProvision:
    """1a: a read never creates, registers, stamps or pushes anything."""

    @pytest.mark.parametrize("args", PLAIN_READS, ids=_ids(PLAIN_READS))
    def test_a_missing_stack_is_an_error_not_a_deploy(self, mock_dynamodb, args) -> None:
        result, stack_manager = _invoke(args)

        assert result.exit_code == 1, result.output
        assert STACK_MISSING in result.output
        _assert_nothing_written(stack_manager, ([], []))

    @pytest.mark.parametrize(
        "args", NAMESPACED_READS + [REGISTRY_READS[2]], ids=_ids(NAMESPACED_READS) + ["ns show"]
    )
    def test_a_missing_namespace_is_an_error_not_a_registration(self, mock_dynamodb, args) -> None:
        asyncio.run(_setup(namespace=False, record=_record(CLIENT)))
        before = asyncio.run(_scan())

        result, stack_manager = _invoke(args)

        assert result.exit_code == 1, result.output
        assert NAMESPACE_MISSING in result.output
        _assert_nothing_written(stack_manager, before)

    @pytest.mark.parametrize("args", REGISTRY_READS[:2], ids=_ids(REGISTRY_READS[:2]))
    def test_registry_reads_need_no_namespace_and_register_none(self, mock_dynamodb, args) -> None:
        """``namespace list``/``orphans`` read the registry, not a namespace:
        an empty one is reported, and "default" is not registered on the way."""
        asyncio.run(_setup(namespace=False, record=_record(CLIENT)))
        before = asyncio.run(_scan())

        result, stack_manager = _invoke(args)

        assert result.exit_code == 0, result.output
        assert "No " in result.output
        _assert_nothing_written(stack_manager, before)

    @pytest.mark.parametrize("args", PLAIN_READS, ids=_ids(PLAIN_READS))
    def test_a_lambda_behind_the_client_is_left_alone(self, mock_dynamodb, args) -> None:
        """2a: a plain read does not need the Lambda, so it neither refuses nor updates."""
        asyncio.run(_setup(namespace=True, record=_record(BEHIND)))
        before = asyncio.run(_scan())

        result, stack_manager = _invoke(args)

        assert result.exit_code == 0, result.output
        assert "Error" not in result.output
        _assert_nothing_written(stack_manager, before)

    def test_a_missing_version_record_is_not_written(self, mock_dynamodb) -> None:
        asyncio.run(_setup(namespace=True, record=None))
        before = asyncio.run(_scan())

        result, stack_manager = _invoke(["entity", "get-limits", "user-1", "-r", "gpt-4"])

        assert result.exit_code == 0, result.output
        _assert_nothing_written(stack_manager, before)

    def test_the_region_defaults_to_the_clients(self, mock_dynamodb) -> None:
        """Without --region the message names the region boto3 resolved."""
        result, stack_manager = _invoke(["resource", "list"], region=False)

        assert result.exit_code == 1, result.output
        assert STACK_MISSING in result.output
        stack_manager.assert_not_called()

    def test_a_client_below_the_minimum_is_still_refused(self, mock_dynamodb) -> None:
        """#638: reading does not excuse a client the stack has locked out."""
        asyncio.run(_setup(namespace=True, record=_record(CLIENT, client_min_version="0.16.0")))
        before = asyncio.run(_scan())

        result, stack_manager = _invoke(["resource", "list"])

        assert result.exit_code == 1, result.output
        assert "below minimum required version 0.16.0" in result.output
        _assert_nothing_written(stack_manager, before)

    def test_an_incompatible_schema_is_refused(self, mock_dynamodb) -> None:
        asyncio.run(_setup(namespace=True, record=_record(CLIENT, schema_version="9.0.0")))
        before = asyncio.run(_scan())

        result, stack_manager = _invoke(["resource", "list"])

        assert result.exit_code == 1, result.output
        assert "Schema migration required" in result.output
        _assert_nothing_written(stack_manager, before)


class TestConnectReadOnly:
    """The helper itself, for the paths the commands cannot reach."""

    def test_any_other_read_failure_propagates_and_closes(self) -> None:
        from zae_limiter.cli import _connect_read_only

        repo = Mock()
        repo.get_version_record = AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetItem"
            )
        )
        repo.close = AsyncMock()
        with (
            patch("zae_limiter.repository.Repository", Mock(return_value=repo)),
            pytest.raises(ClientError, match="AccessDenied"),
        ):
            asyncio.run(_connect_read_only(TABLE, REGION, None))
        repo.close.assert_awaited_once()

    def test_a_stack_level_connection_resolves_no_namespace(self) -> None:
        from zae_limiter.cli import _connect_read_only

        repo = Mock()
        repo.get_version_record = AsyncMock(return_value=_record(CLIENT))
        repo._resolve_namespace = AsyncMock()
        repo.close = AsyncMock()
        with (
            patch("zae_limiter.__version__", CLIENT),
            patch("zae_limiter.repository.Repository", Mock(return_value=repo)),
        ):
            result = asyncio.run(_connect_read_only(TABLE, REGION, None, namespace=None))
        assert result is repo
        repo._resolve_namespace.assert_not_called()
        repo.close.assert_not_called()


class TestPreviewsNeverProvision:
    """``limits plan`` / ``limits diff``: read-only, and refuse a behind Lambda (2a)."""

    @pytest.fixture
    def manifest(self, tmp_path: Path) -> str:
        path = tmp_path / "limits.yaml"
        path.write_text(
            yaml.dump({"namespace": NAMESPACE, "system": {"limits": {"rpm": {"capacity": 10}}}})
        )
        return str(path)

    def _preview(self, command: str, manifest: str) -> tuple[Any, Mock, MagicMock]:
        lambda_client = MagicMock()
        lambda_client.invoke.return_value = {"Payload": MagicMock(read=lambda: b'{"changes": []}')}
        with patch("zae_limiter.limits_cli.boto3.client", return_value=lambda_client) as boto:
            result, stack_manager = _invoke(["limits", command, "-f", manifest])
        return result, stack_manager, boto

    @pytest.mark.parametrize("command", ["plan", "diff"])
    def test_a_missing_stack_is_an_error_not_a_deploy(
        self, mock_dynamodb, manifest, command
    ) -> None:
        result, stack_manager, boto = self._preview(command, manifest)

        assert result.exit_code == 1, result.output
        assert STACK_MISSING in result.output
        boto.assert_not_called()
        _assert_nothing_written(stack_manager, ([], []))

    @pytest.mark.parametrize("command", ["plan", "diff"])
    def test_a_missing_namespace_is_an_error_not_a_registration(
        self, mock_dynamodb, manifest, command
    ) -> None:
        asyncio.run(_setup(namespace=False, record=_record(CLIENT)))
        before = asyncio.run(_scan())

        result, stack_manager, boto = self._preview(command, manifest)

        assert result.exit_code == 1, result.output
        assert NAMESPACE_MISSING in result.output
        boto.assert_not_called()
        _assert_nothing_written(stack_manager, before)

    @pytest.mark.parametrize(
        ("command", "lambda_version", "shown"),
        [
            ("plan", BEHIND, BEHIND),
            ("diff", BEHIND, BEHIND),
            ("plan", None, "unknown"),
            ("diff", None, "unknown"),
        ],
    )
    def test_lambdas_behind_the_client_are_refused_not_updated(
        self, mock_dynamodb, manifest, command, lambda_version, shown
    ) -> None:
        asyncio.run(_setup(namespace=True, record=_record(lambda_version)))
        before = asyncio.run(_scan())

        result, stack_manager, boto = self._preview(command, manifest)

        assert result.exit_code == 1, result.output
        assert (
            f"Error: the stack's Lambdas run {shown}; this client is {CLIENT}. "
            f"Run 'zae-limiter upgrade -n {TABLE}' first, then re-run the {command}."
        ) in result.output
        boto.assert_not_called()
        _assert_nothing_written(stack_manager, before)

    @pytest.mark.parametrize("command", ["plan", "diff"])
    def test_a_missing_version_record_is_refused_not_written(
        self, mock_dynamodb, manifest, command
    ) -> None:
        """Nothing proves the provisioner current, so a preview refuses."""
        asyncio.run(_setup(namespace=True, record=None))
        before = asyncio.run(_scan())

        result, stack_manager, boto = self._preview(command, manifest)

        assert result.exit_code == 1, result.output
        assert "the stack's Lambdas run unknown" in result.output
        boto.assert_not_called()
        _assert_nothing_written(stack_manager, before)

    @pytest.mark.parametrize("command", ["plan", "diff"])
    def test_a_current_stack_is_previewed_in_the_resolved_namespace(
        self, mock_dynamodb, manifest, command
    ) -> None:
        import json

        asyncio.run(_setup(namespace=True, record=_record(CLIENT)))
        before = asyncio.run(_scan())

        async def _namespace_id() -> str | None:
            repo = Repository(TABLE, REGION, None, _skip_deprecation_warning=True)
            try:
                return await repo._resolve_namespace(NAMESPACE)
            finally:
                await repo.close()

        result, stack_manager, boto = self._preview(command, manifest)

        assert result.exit_code == 0, result.output
        payload = json.loads(boto.return_value.invoke.call_args.kwargs["Payload"])
        assert payload["action"] == "plan"
        assert payload["namespace_id"] == asyncio.run(_namespace_id())
        _assert_nothing_written(stack_manager, before)

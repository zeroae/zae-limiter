"""Soft limits and bypass on the CLI, manifest and CloudFormation surfaces (#467, #311)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from zae_limiter.cli import cli
from zae_limiter.models import Limit
from zae_limiter_provisioner.applier import (
    apply_changes,
    require_non_enforcing_readers,
)
from zae_limiter_provisioner.differ import Change
from zae_limiter_provisioner.manifest import LimitDecl, LimitsManifest

from .test_cli import _serve_read_only

RPM = {"capacity": 100, "refill_amount": 100, "refill_period": 60}
SOFT_TPM = {"capacity": 1000, "refill_amount": 1000, "refill_period": 60, "soft": True}


class TestManifest:
    def test_soft_round_trips_and_defaults_hard(self):
        decl = LimitDecl.from_dict({"capacity": 10, "soft": True})
        assert decl.soft is True
        assert decl.to_dict()["soft"] is True
        assert "soft" not in LimitDecl.from_dict({"capacity": 10}).to_dict()

    def test_soft_must_be_a_boolean(self):
        with pytest.raises(ValueError, match="soft"):
            LimitDecl.from_dict({"capacity": 10, "soft": "yes"})

    @pytest.mark.parametrize("level", ["resources", "entities"])
    def test_disabled_takes_bypass(self, level):
        entry = {"disabled": "bypass", "limits": {"rpm": {"capacity": 1}}}
        data: dict[str, Any] = {"namespace": "x"}
        if level == "resources":
            data["resources"] = {"llm": entry}
        else:
            data["entities"] = {"u": {"resources": {"llm": entry}}}
        manifest = LimitsManifest.from_dict(data)
        decl = (
            manifest.resources["llm"]
            if level == "resources"
            else manifest.entities["u"].resources["llm"]
        )
        assert decl.disabled == "bypass"
        assert decl.to_dict()["disabled"] == "bypass"

    def test_disabled_rejects_anything_else(self):
        with pytest.raises(ValueError, match="bypass"):
            LimitsManifest.from_dict(
                {"namespace": "x", "resources": {"llm": {"disabled": "off", "limits": {}}}}
            )

    def test_system_bypass_is_still_rejected(self):
        with pytest.raises(ValueError, match="system"):
            LimitsManifest.from_dict({"namespace": "x", "system": {"disabled": "bypass"}})


class TestApplier:
    @staticmethod
    def _client(old: dict | None = None):
        client = MagicMock()
        client.put_item.return_value = {"Attributes": old} if old else {}
        client.delete_item.return_value = {"Attributes": old} if old else {}
        return client

    def test_soft_and_bypass_are_written(self):
        client = self._client()
        change = Change(
            action="create",
            level="resource",
            target="llm",
            data={"limits": {"rpm": RPM, "tpm": SOFT_TPM}, "disabled": "bypass"},
        )
        apply_changes([change], "t", "ns", client)
        item = client.put_item.call_args.kwargs["Item"]
        assert item["l_tpm_soft"] == {"BOOL": True}
        assert "l_rpm_soft" not in item
        assert item["disabled"] == {"S": "bypass"}

    @pytest.mark.parametrize(
        ("old", "limits", "expected"),
        [
            (None, {"tpm": SOFT_TPM}, ["tpm"]),
            ({"l_tpm_cp": {"N": "1"}, "l_tpm_soft": {"BOOL": True}}, {"tpm": SOFT_TPM}, []),
            ({"l_tpm_cp": {"N": "1"}, "l_tpm_soft": {"BOOL": True}}, {"tpm": RPM}, ["tpm"]),
            (None, {"rpm": RPM}, []),
        ],
    )
    @pytest.mark.parametrize("level", ["resource", "system"])
    def test_only_a_soft_change_is_noted(self, old, limits, expected, level):
        target = "llm" if level == "resource" else None
        change = Change(action="update", level=level, target=target, data={"limits": limits})
        result = apply_changes([change], "t", "ns", self._client(old))
        assert result.soft_changed == ([(level, target, expected)] if expected else [])

    def test_entity_levels_are_left_to_the_param_sync(self):
        change = Change(
            action="update", level="entity", target="u/llm", data={"limits": {"tpm": SOFT_TPM}}
        )
        assert apply_changes([change], "t", "ns", self._client()).soft_changed == []

    def test_a_delete_notes_the_soft_limits_it_held(self):
        old = {"w_s_cp": {"N": "1"}, "w_s_rsa": {"N": "60"}, "w_s_soft": {"BOOL": True}}
        change = Change(action="delete", level="resource", target="llm")
        result = apply_changes([change], "t", "ns", self._client(old))
        assert result.soft_changed == [("resource", "llm", ["s"])]

    def test_a_corrupt_old_image_errs_toward_restamping(self):
        old = {"l_x_cp": {"N": "1"}, "w_x_cp": {"N": "1"}, "l_x_soft": {"BOOL": True}}
        change = Change(action="delete", level="resource", target="llm")
        result = apply_changes([change], "t", "ns", self._client(old))
        assert result.soft_changed == [("resource", "llm", ["l_x_soft"])]

    def test_the_gate_is_free_without_soft_or_bypass(self):
        client = MagicMock()
        change = Change(action="update", level="resource", target="llm", data={"limits": {}})
        require_non_enforcing_readers([change], "t", client=client)
        client.get_item.assert_not_called()

    @pytest.mark.parametrize(
        "data", [{"limits": {"tpm": SOFT_TPM}}, {"limits": {}, "disabled": "bypass"}]
    )
    def test_the_gate_refuses_old_lambdas(self, data):
        from zae_limiter.exceptions import VersionMismatchError

        client = MagicMock()
        client.get_item.return_value = {
            "Item": {"lambda_version": {"S": "0.16.0"}, "schema_version": {"S": "1"}}
        }
        change = Change(action="update", level="resource", target="llm", data=data)
        with patch("zae_limiter_provisioner.applier.__version__", "0.17.1"):
            with pytest.raises(VersionMismatchError, match="soft limit or bypass"):
                require_non_enforcing_readers([change], "t", client=client)


class TestCloudFormation:
    def test_soft_and_bypass_round_trip(self):
        from zae_limiter_provisioner.handler import _cfn_properties_to_manifest

        from .test_limits_cli import TestLimitsCfnTemplateDisabled

        manifest = {
            "namespace": "x",
            "resources": {
                "llm": {"disabled": "bypass", "limits": {"rpm": RPM, "tpm": SOFT_TPM}},
            },
            "entities": {"u": {"resources": {"llm": {"disabled": "bypass", "limits": {}}}}},
        }
        props = TestLimitsCfnTemplateDisabled()._run_cfn_template(manifest)
        assert props["Resources"]["llm"]["Limits"]["tpm"]["Soft"] is True
        assert "Soft" not in props["Resources"]["llm"]["Limits"]["rpm"]
        # CloudFormation delivers every property as a string (#554).
        props["Resources"]["llm"]["Limits"]["tpm"]["Soft"] = "true"
        props["Resources"]["llm"]["Disabled"] = "Bypass"
        back = _cfn_properties_to_manifest(props)
        assert back["resources"]["llm"]["limits"]["tpm"]["soft"] is True
        assert back["resources"]["llm"]["disabled"] == "bypass"
        assert back["entities"]["u"]["resources"]["llm"]["disabled"] == "bypass"
        LimitsManifest.from_dict(back)

    def test_disabled_still_rejects_a_guess(self):
        from zae_limiter_provisioner.handler import _coerce_disabled

        assert _coerce_disabled("False", "x") is False
        with pytest.raises(ValueError):
            _coerce_disabled("yes", "x")


class TestHandlerSoftFanout:
    @patch("zae_limiter_provisioner.handler.fanout_soft")
    @patch("zae_limiter_provisioner.handler.boto3")
    def test_each_changed_level_is_fanned_out(self, mock_boto3, mock_fanout):
        from zae_limiter_provisioner.handler import _fanout_soft_changes

        errors = _fanout_soft_changes(
            "t", [("resource", "llm", ["tpm"]), ("system", None, ["rpm"])], "ns"
        )
        assert errors == []
        client = mock_boto3.client.return_value
        assert mock_fanout.call_args_list[0].args == (client, "t", "ns", "llm", {"tpm"})
        assert mock_fanout.call_args_list[1].args == (client, "t", "ns", None, {"rpm"})

    @patch("zae_limiter_provisioner.handler.boto3")
    def test_nothing_changed_means_no_work(self, mock_boto3):
        from zae_limiter_provisioner.handler import _fanout_soft_changes

        assert _fanout_soft_changes("t", [], "ns") == []
        mock_boto3.client.assert_not_called()

    @patch("zae_limiter_provisioner.handler.fanout_soft", side_effect=RuntimeError("boom"))
    @patch("zae_limiter_provisioner.handler.boto3")
    def test_a_failure_is_reported(self, mock_boto3, mock_fanout):
        from zae_limiter_provisioner.handler import _fanout_soft_changes

        assert _fanout_soft_changes("t", [("resource", "llm", ["tpm"])], "ns") == [
            "soft fan-out resource llm: boom"
        ]


class TestProvisionerFanoutSoft:
    TABLE = "prov-soft"

    @pytest.fixture
    def setup(self, mock_dynamodb):
        import boto3

        from zae_limiter import __version__
        from zae_limiter.sync_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository
        from zae_limiter.version import get_schema_version

        repo = SyncRepository(name=self.TABLE, region="us-east-1", _skip_deprecation_warning=True)
        repo.create_table()
        repo._register_namespace("default")
        repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
        repo.set_system_defaults([Limit.per_minute("tpm", 10)])
        repo.set_resource_defaults("llm", [Limit.per_minute("tpm", 10)])
        repo.set_limits("vip", [Limit.per_minute("tpm", 10)], resource="llm")
        limiter = SyncRateLimiter(repository=repo)
        for entity, resource in (("u", "llm"), ("vip", "llm"), ("u", "api")):
            with limiter.acquire(entity, resource, consume={"tpm": 1}):
                pass
        yield boto3.client("dynamodb", region_name="us-east-1"), repo
        repo.close()

    def _soft(self, client, repo, entity, resource):
        from zae_limiter.schema import pk_bucket, sk_state

        item = client.get_item(
            TableName=self.TABLE,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity, resource, 0)},
                "SK": {"S": sk_state()},
            },
        )["Item"]
        return bool(item.get("b_tpm_soft", {}).get("BOOL"))

    def test_a_resource_change_restamps_its_buckets(self, setup):
        from zae_limiter_provisioner.fanout import fanout_soft

        client, repo = setup
        change = Change(
            action="update", level="resource", target="llm", data={"limits": {"tpm": SOFT_TPM}}
        )
        apply_changes([change], self.TABLE, repo._namespace_id, client)
        assert fanout_soft(client, self.TABLE, repo._namespace_id, "llm", {"tpm"}) == 1
        assert self._soft(client, repo, "u", "llm") is True
        assert self._soft(client, repo, "vip", "llm") is False  # its own override decides
        assert self._soft(client, repo, "u", "api") is False

    def test_a_system_change_reaches_the_namespace(self, setup):
        from zae_limiter_provisioner.fanout import fanout_soft

        client, repo = setup
        change = Change(
            action="update", level="system", target=None, data={"limits": {"tpm": SOFT_TPM}}
        )
        apply_changes([change], self.TABLE, repo._namespace_id, client)
        assert fanout_soft(client, self.TABLE, repo._namespace_id, None, {"tpm"}) == 1
        assert self._soft(client, repo, "u", "api") is True
        assert self._soft(client, repo, "u", "llm") is False  # the resource level decides

    def test_a_vanished_bucket_is_skipped(self, setup):
        from zae_limiter_provisioner.fanout import stamp_bucket_soft

        client, _repo = setup
        stamp_bucket_soft(client, self.TABLE, "default/BUCKET#ghost#llm#0", {"tpm": True})

    def test_the_param_sync_stamps_soft(self, setup):
        from zae_limiter_provisioner.bucket_sync import build_bucket_param_update

        expr, names, values = build_bucket_param_update(
            {"tpm": SOFT_TPM, "rpm": RPM}, None, {"old"}, 0
        )
        soft_aliases = {k for k, v in names.items() if v.endswith("_soft")}
        assert len(soft_aliases) == 3  # tpm SET, rpm REMOVE, stale `old` REMOVE
        assert {"BOOL": True} in values.values()


class TestCli:
    @staticmethod
    def _writable(mock_repo_class: Mock, **methods: Any) -> Mock:
        mock_repo = Mock()
        for name, value in methods.items():
            setattr(mock_repo, name, AsyncMock(**value))
        mock_repo.close = AsyncMock(return_value=None)
        mock_repo_class.open = AsyncMock(return_value=mock_repo)
        return mock_repo

    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    @pytest.mark.parametrize(
        ("command", "method"),
        [
            (["resource", "set-defaults", "llm"], "set_resource_defaults"),
            (["system", "set-defaults"], "set_system_defaults"),
            (["entity", "set-limits", "u", "-r", "llm"], "set_limits"),
        ],
    )
    @patch("zae_limiter.repository.Repository")
    def test_soft_marks_the_named_limit(self, mock_repo_class, runner, command, method):
        repo = self._writable(mock_repo_class, **{method: {"return_value": None}})
        result = runner.invoke(cli, [*command, "-l", "rpm:10", "-l", "tpm:100", "--soft", "tpm"])
        assert result.exit_code == 0, result.output
        call = getattr(repo, method).call_args
        limits = next(a for a in (*call.args, *call.kwargs.values()) if isinstance(a, list))
        assert {limit.name: limit.soft for limit in limits} == {"rpm": False, "tpm": True}
        assert "tpm: 100/min (soft)" in result.output

    def test_soft_must_name_a_declared_limit(self, runner):
        result = runner.invoke(
            cli, ["resource", "set-defaults", "llm", "-l", "rpm:10", "--soft", "tpm"]
        )
        assert result.exit_code == 1
        assert "--soft names ['tpm']" in result.output

    @pytest.mark.parametrize(
        ("command", "method", "kwargs"),
        [
            (["resource", "bypass", "llm"], "bypass_resource", None),
            (["entity", "bypass", "u"], "bypass_entity", {"resource": None}),
            (["entity", "bypass", "u", "--resource", "llm"], "bypass_entity", {"resource": "llm"}),
        ],
    )
    @patch("zae_limiter.repository.Repository")
    def test_bypass_commands(self, mock_repo_class, runner, command, method, kwargs):
        repo = self._writable(mock_repo_class, **{method: {"return_value": 3}})
        result = runner.invoke(cli, command)
        assert result.exit_code == 0, result.output
        assert "Bypassed" in result.output and "3 buckets stamped" in result.output
        if kwargs is not None:
            assert getattr(repo, method).call_args.kwargs == kwargs

    @pytest.mark.parametrize("method", ["bypass_resource", "bypass_entity"])
    @patch("zae_limiter.repository.Repository")
    def test_a_failed_bypass_exits_1(self, mock_repo_class, runner, method):
        self._writable(mock_repo_class, **{method: {"side_effect": RuntimeError("nope")}})
        group = "resource" if method == "bypass_resource" else "entity"
        result = runner.invoke(cli, [group, "bypass", "x"])
        assert result.exit_code == 1
        assert "Failed to bypass" in result.output

    @pytest.mark.parametrize(
        ("command", "getter"),
        [
            (["resource", "get-defaults", "llm"], "get_resource_disabled"),
            (["entity", "get-limits", "u", "--resource", "llm"], "get_entity_disabled"),
        ],
    )
    @patch("zae_limiter.repository.Repository")
    def test_bypass_status_line(self, mock_repo_class, runner, command, getter):
        repo = Mock()
        repo.get_resource_defaults = AsyncMock(return_value=[Limit.per_minute("rpm", 5)])
        repo.get_limits = AsyncMock(return_value=[Limit.per_minute("rpm", 5, soft=True)])
        repo.get_resource_disabled = AsyncMock(return_value=None)
        repo.get_entity_disabled = AsyncMock(return_value=None)
        setattr(repo, getter, AsyncMock(return_value="bypass"))
        repo.get_resource_cascade = AsyncMock(return_value=None)
        repo.get_entity_cascade = AsyncMock(return_value=None)
        repo.close = AsyncMock(return_value=None)
        _serve_read_only(mock_repo_class, repo)
        result = runner.invoke(cli, command)
        assert result.exit_code == 0, result.output
        assert "Status: BYPASSED" in result.output

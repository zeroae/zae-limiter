"""Tests for the e2e stack reaper (``scripts/reap_e2e_stacks.py``, #189).

The reaper deletes stacks in a shared AWS account, so these pin what it must
never touch: stacks outside the prefix, young stacks, and stacks mid-operation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, WaiterError
from moto import mock_aws

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reap_e2e_stacks.py"


def _load() -> ModuleType:
    """Import ``scripts/reap_e2e_stacks.py`` (not an installed package) by path."""
    spec = importlib.util.spec_from_file_location("zae_reap_e2e_stacks", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reaper = _load()

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TEMPLATE = json.dumps(
    {"Resources": {"Queue": {"Type": "AWS::SQS::Queue", "Properties": {}}}},
)


def _summary(name: str, status: str = "CREATE_COMPLETE", age: timedelta = timedelta(days=3)):
    return {
        "StackName": name,
        "StackId": f"arn:aws:cloudformation:us-east-1:123456789012:stack/{name}/id",
        "StackStatus": status,
        "CreationTime": NOW - age,
    }


class TestSelect:
    def test_takes_old_prefixed_stacks_only(self):
        picked = reaper.select(
            [
                _summary("zci-test-old"),
                _summary("zci-test-young", age=timedelta(hours=2)),
                _summary("limiter"),
                _summary("test-zci-lookalike"),
            ],
            "zci-",
            NOW - timedelta(days=1),
        )
        assert [s.name for s in picked] == ["zci-test-old"]

    def test_retries_delete_failed_and_skips_operations_in_progress(self):
        picked = reaper.select(
            [
                _summary("zci-failed", "DELETE_FAILED"),
                _summary("zci-rollback", "ROLLBACK_COMPLETE"),
                _summary("zci-creating", "CREATE_IN_PROGRESS"),
                _summary("zci-deleting", "DELETE_IN_PROGRESS"),
                _summary("zci-updating", "UPDATE_IN_PROGRESS"),
            ],
            "zci-",
            NOW,
        )
        assert [s.name for s in picked] == ["zci-failed", "zci-rollback"]

    def test_cutoff_is_exclusive(self):
        assert (
            reaper.select(
                [_summary("zci-edge", age=timedelta(days=1))], "zci-", NOW - timedelta(days=1)
            )
            == []
        )


@pytest.fixture
def cfn(aws_credentials):
    with mock_aws():
        client = boto3.client("cloudformation", region_name="us-east-1")
        for name in ("zci-test-a", "zci-test-b", "limiter"):
            client.create_stack(StackName=name, TemplateBody=TEMPLATE)
        yield client


def _live(client) -> set[str]:
    return {
        s["StackName"]
        for s in client.list_stacks()["StackSummaries"]
        if s["StackStatus"] != "DELETE_COMPLETE"
    }


class TestReap:
    def test_deletes_prefixed_stacks_past_the_cutoff(self, cfn):
        later = datetime.now(UTC) + timedelta(days=2)
        assert reaper.reap(cfn, "zci-", timedelta(days=1), later, dry_run=False) == 0
        assert _live(cfn) == {"limiter"}

    def test_keeps_stacks_younger_than_the_cutoff(self, cfn, capsys):
        assert reaper.reap(cfn, "zci-", timedelta(days=1), datetime.now(UTC), dry_run=False) == 0
        assert _live(cfn) == {"zci-test-a", "zci-test-b", "limiter"}
        assert "No zci-* stacks older than" in capsys.readouterr().out

    def test_dry_run_deletes_nothing(self, cfn, capsys):
        later = datetime.now(UTC) + timedelta(days=2)
        assert reaper.reap(cfn, "zci-", timedelta(days=1), later, dry_run=True) == 0
        assert _live(cfn) == {"zci-test-a", "zci-test-b", "limiter"}
        assert capsys.readouterr().out.count("Would delete zci-test-") == 2


class _FlakyCfn:
    """``delete_stack`` succeeds; the waiter fails the first ``failures`` times."""

    def __init__(self, failures: int):
        self.failures = failures
        self.deletes = 0

    def delete_stack(self, **kwargs: Any) -> None:
        self.deletes += 1

    def get_waiter(self, name: str) -> Any:
        outer = self

        class Waiter:
            def wait(self, **kwargs: Any) -> None:
                if outer.deletes <= outer.failures:
                    raise WaiterError("StackDeleteComplete", "DELETE_FAILED", {})

        return Waiter()

    def get_paginator(self, name: str) -> Any:
        class Paginator:
            def paginate(self, **kwargs: Any):
                return [{"StackSummaries": [_summary("zci-test-stuck", "DELETE_FAILED")]}]

        return Paginator()


class TestDelete:
    def test_retries_a_delete_that_failed_once(self):
        cfn = _FlakyCfn(failures=1)
        stack = reaper.select([_summary("zci-x")], "zci-", NOW)[0]
        assert reaper.delete(cfn, stack) is True
        assert cfn.deletes == 2

    def test_gives_up_after_the_retry(self, capsys):
        cfn = _FlakyCfn(failures=5)
        stack = reaper.select([_summary("zci-x")], "zci-", NOW)[0]
        assert reaper.delete(cfn, stack) is False
        assert cfn.deletes == reaper.DELETE_ATTEMPTS
        assert "did not complete" in capsys.readouterr().err

    def test_a_refused_delete_counts_as_failed(self):
        class Refused(_FlakyCfn):
            def delete_stack(self, **kwargs: Any) -> None:
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteStack"
                )

        stack = reaper.select([_summary("zci-x")], "zci-", NOW)[0]
        assert reaper.delete(Refused(failures=0), stack) is False


class TestMain:
    def test_exit_code_reports_a_stack_left_behind(self, capsys):
        assert reaper.main(["--older-than-days", "0"], cfn=_FlakyCfn(failures=5)) == 1
        assert "::error::Could not delete zci-test-stuck" in capsys.readouterr().out

    def test_exit_code_is_zero_when_everything_went(self):
        assert reaper.main(["--older-than-days", "0"], cfn=_FlakyCfn(failures=0)) == 0

    @pytest.mark.parametrize(
        "argv",
        [["--older-than-days", "-1"], ["--older-than-days", "1", "--prefix", ""]],
    )
    def test_rejects_arguments_that_would_widen_the_sweep(self, argv):
        with pytest.raises(SystemExit) as exc:
            reaper.main(argv, cfn=_FlakyCfn(failures=0))
        assert exc.value.code == 2

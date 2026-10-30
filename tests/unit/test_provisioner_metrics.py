"""Provisioner debug metrics (``zae_limiter_provisioner.metrics``).

They measure an apply where moto cannot: round-trip latency, botocore CPU on
a small Lambda, throttling. Opt-in, and free of anything the manifest names.
"""

import json
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest

from zae_limiter_provisioner import handler
from zae_limiter_provisioner.metrics import (
    DEBUG_ENV,
    LOG_PREFIX,
    NULL_METRICS,
    PARTIAL_PREFIX,
    ApplyMetrics,
    debug_requested,
)

TABLE = "test-provisioner-metrics"
NS = "ns1"


@pytest.fixture
def table(mock_dynamodb):
    from zae_limiter.sync_repository import SyncRepository

    repo = SyncRepository(name=TABLE, region="us-east-1", _skip_deprecation_warning=True)
    repo.create_table()
    repo.close()


def _manifest(entities: int, entity_prefix: str = "user") -> dict:
    return {
        "namespace": "default",
        "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 50}}}},
        "entities": {
            f"{entity_prefix}-{i}": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 100}}}}}
            for i in range(entities)
        },
    }


def _apply(manifest: dict, **extra) -> dict:
    return handler.on_event(
        {"action": "apply", "table_name": TABLE, "namespace_id": NS, "manifest": manifest, **extra},
        None,
    )


class TestDebugRequested:
    @pytest.mark.parametrize(
        "event",
        [
            {"debug": True},
            {"debug": "true"},
            {"debug": "TRUE"},
            {"RequestType": "Update", "ResourceProperties": {"Debug": "true"}},
            {"RequestType": "Update", "ResourceProperties": {"Debug": True}},
        ],
    )
    def test_the_event_turns_it_on(self, event, monkeypatch):
        monkeypatch.delenv(DEBUG_ENV, raising=False)
        assert debug_requested(event) is True

    @pytest.mark.parametrize(
        "event",
        [
            {},
            {"debug": False},
            {"debug": "false"},
            {"debug": 1},
            {"RequestType": "Update", "ResourceProperties": {"Debug": "false"}},
        ],
    )
    def test_off_by_default(self, event, monkeypatch):
        monkeypatch.delenv(DEBUG_ENV, raising=False)
        assert debug_requested(event) is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_the_environment_turns_it_on_for_every_invocation(self, value, monkeypatch):
        monkeypatch.setenv(DEBUG_ENV, value)
        assert debug_requested({}) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_other_environment_values_leave_it_off(self, value, monkeypatch):
        monkeypatch.setenv(DEBUG_ENV, value)
        assert debug_requested({}) is False


class TestApplyMetrics:
    def test_phases_accumulate_and_fanouts_are_keyed_without_targets(self):
        metrics = ApplyMetrics()
        with metrics.phase("plan"):
            pass
        with metrics.phase("plan"):
            pass
        with metrics.fanout("sync", "entity"):
            pass
        metrics.count("changed_levels", 3)
        report = metrics.as_dict()
        assert set(report["phases_ms"]) == {"plan"}
        assert report["current_phase"] is None
        assert report["fanouts"]["sync:entity"]["count"] == 1
        assert report["counts"] == {"changed_levels": 3}

    def test_the_current_phase_is_reported_while_it_runs(self):
        metrics = ApplyMetrics()
        with metrics.phase("fanouts"):
            assert metrics.as_dict()["current_phase"] == "fanouts"

    def test_dynamodb_calls_are_timed_and_errors_counted(self, table):
        metrics = ApplyMetrics()
        client = metrics.instrument(boto3.client("dynamodb", region_name="us-east-1"))
        key = {"PK": {"S": "a"}, "SK": {"S": "b"}}
        client.get_item(TableName=TABLE, Key=key)
        client.put_item(TableName=TABLE, Item=key)
        with pytest.raises(client.exceptions.ConditionalCheckFailedException):
            client.put_item(
                TableName=TABLE, Item=key, ConditionExpression="attribute_not_exists(PK)"
            )
        report = metrics.as_dict()
        assert report["clients_created"] == 1
        assert report["dynamodb_calls"]["GetItem"]["count"] == 1
        assert report["dynamodb_calls"]["PutItem"]["count"] == 2
        assert report["dynamodb_calls"]["PutItem"]["errors"] == {
            "ConditionalCheckFailedException": 1
        }
        assert report["dynamodb_calls_total"] == 3

    def test_a_client_without_an_event_system_is_counted_and_left_alone(self):
        metrics = ApplyMetrics()
        client = object()
        assert metrics.instrument(client) is client
        assert metrics.as_dict()["clients_created"] == 1

    def test_the_context_says_how_big_the_function_is(self):
        context = MagicMock(memory_limit_in_mb="256")
        context.get_remaining_time_in_millis.return_value = 299_000
        report = ApplyMetrics(context).as_dict()
        assert (report["memory_limit_mb"], report["remaining_ms_at_start"]) == (256, 299_000)

    def test_emit_prints_one_prefixed_json_line(self, capsys):
        metrics = ApplyMetrics()
        report = metrics.emit()
        (line,) = capsys.readouterr().out.splitlines()
        prefix, payload = line.split(" ", 1)
        assert prefix == LOG_PREFIX
        assert json.loads(payload) == json.loads(json.dumps(report))

    def test_a_heartbeat_prints_partial_snapshots_until_emit(self, capsys):
        metrics = ApplyMetrics()
        metrics.start_heartbeat(interval=0.01)
        deadline = time.monotonic() + 5
        while PARTIAL_PREFIX not in capsys.readouterr().out:
            assert time.monotonic() < deadline, "no partial snapshot"
            time.sleep(0.01)
        metrics.emit()
        capsys.readouterr()
        time.sleep(0.05)
        assert PARTIAL_PREFIX not in capsys.readouterr().out

    def test_the_null_recorder_records_nothing(self):
        client = object()
        assert NULL_METRICS.instrument(client) is client
        with NULL_METRICS.phase("x"), NULL_METRICS.fanout("sync", "entity"):
            pass
        NULL_METRICS.count("x", 1)
        assert NULL_METRICS.enabled is False


class TestDebugApply:
    """An apply with debug on reports its metrics, and only counts and timings."""

    def test_debug_returns_and_logs_metrics_without_names(self, table, capsys):
        result = _apply(_manifest(10, entity_prefix="classified"), debug=True)
        report = result["debug"]
        assert report["counts"]["changes"] == {"create:entity": 10, "create:resource": 1}
        assert report["counts"]["owed_fanouts"] == 10
        assert report["fanouts"]["sync:entity"]["count"] == 10
        assert report["clients_created"] == 1
        assert report["dynamodb_calls"]["PutItem"]["count"] == 12  # 11 levels + the record
        for phase in ("plan", "write_ahead", "config_writes", "fanouts", "record_state"):
            assert phase in report["phases_ms"]
        out = capsys.readouterr().out
        assert f"{LOG_PREFIX} " in out
        # Nothing the manifest names leaves the function in the metrics.
        assert "classified" not in json.dumps(report)
        assert "gpt-4" not in json.dumps(report)

    def test_off_by_default(self, table, monkeypatch):
        monkeypatch.delenv(DEBUG_ENV, raising=False)
        assert "debug" not in _apply(_manifest(1))

    def test_a_failed_debug_run_still_logs_its_metrics(self, table, capsys):
        with (
            patch.object(handler, "_plan_changes", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            _apply(_manifest(1), debug=True)
        line = next(
            line for line in capsys.readouterr().out.splitlines() if line.startswith(LOG_PREFIX)
        )
        report = json.loads(line.split(" ", 1)[1])
        assert report["counts"]["failed"] is True
        assert report["current_phase"] is None

    def test_a_cfn_event_with_debug_carries_metrics(self, table):
        with patch.object(handler, "_send_cfn_response") as respond:
            result = handler.on_event(
                {
                    "RequestType": "Create",
                    "ResponseURL": "https://example.invalid",
                    "ResourceProperties": {
                        "TableName": TABLE,
                        "NamespaceId": NS,
                        "Debug": "true",
                        "Resources": {"gpt-4": {"Limits": {"rpm": {"Capacity": "5"}}}},
                    },
                },
                None,
            )
        assert result["debug"]["counts"]["changes"] == {"create:resource": 1}
        assert respond.call_args.args[2] == "SUCCESS"

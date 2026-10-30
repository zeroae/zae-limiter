"""Provisioner throughput.

A 1270-level initial apply took over seven minutes in production: every owed
fan-out ran one after another, each on a newly built DynamoDB client (and so a
fresh TLS connection). These pin the shared client and the phased concurrency
that replaced the serial loop.
"""

import threading
import time
from unittest.mock import patch

import boto3
import pytest

from zae_limiter_provisioner import handler
from zae_limiter_provisioner.differ import Change

TABLE = "test-provisioner-throughput"
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


class TestWorkers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, 8), ("", 8), ("16", 16), ("0", 1), ("1000", 64), ("many", 8)],
    )
    def test_the_environment_sets_the_fanout_concurrency(self, value, expected, monkeypatch):
        if value is None:
            monkeypatch.delenv(handler.WORKERS_ENV, raising=False)
        else:
            monkeypatch.setenv(handler.WORKERS_ENV, value)
        assert handler._workers_from_env() == expected

    def test_the_connection_pool_covers_every_worker(self, monkeypatch):
        monkeypatch.setattr(handler, "_APPLY_WORKERS", 32)
        with patch.object(handler.boto3, "client") as client:
            handler._new_client()
        assert client.call_args.kwargs["config"].max_pool_connections == 34


class TestRunFanouts:
    def test_a_phase_finishes_before_the_next_starts(self):
        """`disabled` before `cascade` before `sync`; resource levels before entity levels."""
        keys = [
            ("sync", "entity", "u2/x"),
            ("cascade", "entity", "u1/x"),
            ("disabled", "entity", "u1/x"),
            ("sync", "entity", "u1/x"),
            ("disabled", "resource", "x"),
            ("disabled", "entity", "u2/x"),
        ]
        finished: list[tuple[int, bool]] = []
        lock = threading.Lock()

        def run(key):
            phase = handler._fanout_phase(key)
            with lock:
                # Nothing from a later phase has finished yet.
                assert all(done <= phase for done in finished)
            time.sleep(0.01)
            with lock:
                finished.append(phase)
            return []

        assert handler._run_fanouts(keys, run) == []
        assert finished == sorted(finished)

    def test_a_phase_runs_its_keys_concurrently(self, monkeypatch):
        monkeypatch.setattr(handler, "_APPLY_WORKERS", 4)
        barrier = threading.Barrier(4, timeout=5)

        def run(_key):
            barrier.wait()  # deadlocks (BrokenBarrierError) if run one at a time
            return []

        keys = [("sync", "entity", f"u{i}/x") for i in range(4)]
        assert handler._run_fanouts(keys, run) == []

    def test_errors_keep_the_serial_order(self):
        keys = [("sync", "entity", f"u{i}/x") for i in range(5)]

        def run(key):
            time.sleep(0.01 * (5 - int(key[2][1])))  # finish in reverse
            return [key[2]]

        assert handler._run_fanouts(keys, run) == [k[2] for k in keys]


class TestParallelConfigWrites:
    def test_results_are_recorded_in_change_order_with_failures_per_change(self, table):
        from zae_limiter_provisioner.applier import apply_changes

        changes = [
            Change("create", "entity", f"u{i}/gpt-4", {"limits": {"rpm": _limit(i + 1)}})
            for i in range(12)
        ]
        # One unbuildable item (two timezones) fails on its own.
        changes[5] = Change(
            "create",
            "entity",
            "u5/gpt-4",
            {
                "limits": {
                    "a": {
                        **_limit(1),
                        "schedule": [{"cron": "0 9 * * *", "tz": "UTC", "scale": 2}],
                    },
                    "b": {
                        **_limit(1),
                        "schedule": [{"cron": "0 9 * * *", "tz": "Europe/Paris", "scale": 2}],
                    },
                }
            },
        )
        result = apply_changes(changes, TABLE, NS)
        assert result.created == 11
        assert len(result.errors) == 1 and result.errors[0].startswith("create entity u5/gpt-4")
        assert [c.target for c in result.limits_changed] == [
            c.target for i, c in enumerate(changes) if i != 5
        ]


def _limit(capacity: int) -> dict:
    return {"capacity": capacity, "refill_amount": capacity, "refill_period": 60}


class TestInitialApply:
    def test_one_client_serves_the_whole_apply(self, table, monkeypatch):
        built = []
        original = boto3.client

        def counting(*args, **kwargs):
            built.append(args)
            return original(*args, **kwargs)

        with patch.object(handler.boto3, "client", side_effect=counting):
            result = _apply(_manifest(20))
        assert (result["created"], result["errors"]) == (21, [])
        # Was one per owed fan-out: 20 entity syncs here, 1270 in production.
        assert len(built) == 1

    def test_every_owed_fanout_lands_and_is_released(self, table):
        result = _apply(_manifest(30))
        assert (result["created"], result["errors"], result["pending"]) == (31, [], 0)
        again = _apply(_manifest(30))
        assert (again["changes"], again["unchanged"]) == ([], 31)

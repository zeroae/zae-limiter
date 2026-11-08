"""A manifest apply that changes a resource- or system-level limit reaches
active buckets (ADR-153, #823).

The provisioner owes the change a ``sync`` fan-out, a pending item like every
other (``pending.py``), run from the level's live config item by
``fanout.fanout_params``: GSI2 for a resource, GSI4 for the system, each bucket
read and written once, its fill instants re-expressed in a pinned write.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest

from tests.fixtures.balances import balance_milli
from zae_limiter import RateLimitExceeded, schema
from zae_limiter.sync_limiter import SyncRateLimiter
from zae_limiter.sync_repository import SyncRepository
from zae_limiter_provisioner import pending
from zae_limiter_provisioner.handler import on_event

TABLE = "prov-default-sync"
RESOURCE = "gpt-4"
T0 = 1_800_000_000_000


class _Killed(BaseException):
    """The Lambda runtime killing an invocation: no ``except Exception`` catches it."""


@pytest.fixture
def clock():
    class Clock:
        now = T0

    with (
        patch("zae_limiter_provisioner.bucket_sync.read_clock_ms", lambda: Clock.now),
        patch("zae_limiter_provisioner.fanout.read_clock_ms", lambda: Clock.now),
    ):
        yield Clock


@pytest.fixture
def repo(mock_dynamodb, clock):
    setup = SyncRepository(name=TABLE, region="us-east-1", _skip_deprecation_warning=True)
    setup.create_table()
    setup._register_namespace("default")
    scoped = setup.namespace("default")
    scoped._now_ms = lambda: clock.now
    yield scoped
    setup.close()


def _manifest(capacity: int, **extra: Any) -> dict[str, Any]:
    return {
        "namespace": "default",
        "resources": {RESOURCE: {"limits": {"rpm": {"capacity": capacity}}}},
        **extra,
    }


def _apply(repo: SyncRepository, manifest: dict[str, Any]) -> dict[str, Any]:
    return on_event(
        {
            "action": "apply",
            "table_name": TABLE,
            "namespace_id": repo._namespace_id,
            "manifest": manifest,
        },
        MagicMock(),
    )


def _item(repo: SyncRepository, entity_id: str) -> dict[str, Any]:
    return boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName=TABLE,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, RESOURCE, 0)},
            "SK": {"S": schema.sk_state()},
        },
        ConsistentRead=True,
    )["Item"]


def _cp(item: dict[str, Any]) -> int:
    return int(item[schema.bucket_attr("rpm", schema.BUCKET_FIELD_CP)]["N"])


def _burst(limiter: SyncRateLimiter, entity_id: str) -> int:
    admitted = 0
    while admitted < 2_000:
        try:
            with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
                admitted += 1
        except RateLimitExceeded:
            break
    return admitted


def test_lowering_a_resource_limit_reaches_an_active_bucket(repo, clock):
    assert _apply(repo, _manifest(1000))["errors"] == []
    limiter = SyncRateLimiter(repository=repo)
    with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
        pass

    result = _apply(repo, _manifest(100))

    assert result["errors"] == []
    item = _item(repo, "user-1")
    assert _cp(item) == 100_000
    assert balance_milli(repo, item, "rpm", clock.now) == 100_000  # trimmed to the new share
    clock.now += 60_000
    repo.invalidate_config_cache()
    assert _burst(limiter, "user-1") == 100


def test_lowering_a_system_limit_reaches_an_active_bucket(repo, clock):
    system = {"system": {"limits": {"rpm": {"capacity": 1000}}}}
    assert _apply(repo, {"namespace": "default", **system})["errors"] == []
    with SyncRateLimiter(repository=repo).acquire("user-1", RESOURCE, {"rpm": 1}):
        pass

    lowered = {"system": {"limits": {"rpm": {"capacity": 100}}}}
    assert _apply(repo, {"namespace": "default", **lowered})["errors"] == []

    assert _cp(_item(repo, "user-1")) == 100_000


def test_an_entity_with_its_own_config_keeps_it(repo, clock):
    vip = {"entities": {"vip": {"resources": {RESOURCE: {"limits": {"rpm": {"capacity": 1000}}}}}}}
    assert _apply(repo, _manifest(1000, **vip))["errors"] == []
    limiter = SyncRateLimiter(repository=repo)
    for entity_id in ("vip", "user-1"):
        with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
            pass

    assert _apply(repo, _manifest(100, **vip))["errors"] == []

    assert _cp(_item(repo, "vip")) == 1_000_000
    assert _cp(_item(repo, "user-1")) == 100_000


def test_a_fan_out_cut_short_is_retried_by_the_next_apply(repo, clock):
    assert _apply(repo, _manifest(1000))["errors"] == []
    with SyncRateLimiter(repository=repo).acquire("user-1", RESOURCE, {"rpm": 1}):
        pass

    with patch("zae_limiter_provisioner.handler.fanout_params", side_effect=_Killed()):
        with pytest.raises(_Killed):
            _apply(repo, _manifest(100))
    assert _cp(_item(repo, "user-1")) == 1_000_000  # the config landed, the fan-out did not
    keys = {p.key for p in pending.read_pending(_client(), TABLE, repo._namespace_id)}
    assert ("sync", "resource", RESOURCE) in keys

    # Once the killed run's lease is over, the next apply (no config change)
    # claims the item and runs the fan-out from the live config.
    later = pending.now_ms() + pending.MAX_RUN_MS + 2 * pending.LEASE_MARGIN_MS
    with patch.object(pending, "now_ms", return_value=later):
        result = _apply(repo, _manifest(100))
    assert result["errors"] == []
    assert _cp(_item(repo, "user-1")) == 100_000
    assert pending.read_pending(_client(), TABLE, repo._namespace_id) == []


def _client():
    return boto3.client("dynamodb", region_name="us-east-1")


def test_a_vanished_bucket_or_one_carrying_nothing_is_not_written(repo, clock):
    from zae_limiter_provisioner.fanout import _sync_default_bucket

    assert _apply(repo, _manifest(1000))["errors"] == []
    with SyncRateLimiter(repository=repo).acquire("user-1", RESOURCE, {"rpm": 1}):
        pass
    pk = schema.pk_bucket(repo._namespace_id, "user-1", RESOURCE, 0)
    gone = schema.pk_bucket(repo._namespace_id, "nobody", RESOURCE, 0)
    tpm = {"tpm": {"capacity": 10, "refill_amount": 10, "refill_period": 60}}

    assert _sync_default_bucket(_client(), TABLE, gone, tpm, None, clock.now) is False
    before = _item(repo, "user-1")
    assert _sync_default_bucket(_client(), TABLE, pk, tpm, {"tpd"}, clock.now) is False
    assert _item(repo, "user-1") == before


def test_an_image_a_raise_holds_is_waited_out_and_read_again(repo, clock):
    from zae_limiter_provisioner.bucket_sync import _update_one_shard, build_bucket_param_update

    assert _apply(repo, _manifest(1000))["errors"] == []
    with SyncRateLimiter(repository=repo).acquire("user-1", RESOURCE, {"rpm": 1}):
        pass
    pk = schema.pk_bucket(repo._namespace_id, "user-1", RESOURCE, 0)
    image = {**_item(repo, "user-1"), schema.BUCKET_FIELD_RAISE: {"N": str(clock.now + 500)}}
    lowered = {"rpm": {"capacity": 100, "refill_amount": 100, "refill_period": 60}}
    plan = build_bucket_param_update(lowered, 7, None, clock.now)
    with patch("zae_limiter_provisioner.bucket_sync.time.sleep") as sleep:
        assert _update_one_shard(_client(), TABLE, pk, *plan, image=image) is True
    sleep.assert_called_once_with(0.5)
    assert _cp(_item(repo, "user-1")) == 100_000

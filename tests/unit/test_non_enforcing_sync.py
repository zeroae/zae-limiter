"""Soft limits and bypass through the generated sync classes (ADR-151, review D14).

`test_bypass.py` and `test_soft_limits.py` are async-only: they patch
coroutines between a read and a write, which the sync generator does not
translate. These are the end-to-end behaviours again, through
`SyncRepository`, `SyncRateLimiter` and `SyncLease`, so the generated twins
are exercised rather than only regenerated.
"""

from unittest.mock import patch

import pytest

from zae_limiter import RateLimitExceeded, SyncRateLimiter, schema
from zae_limiter.models import Limit


def _item(repo, entity_id: str, resource: str) -> dict:
    client = repo._get_client()
    return client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, 0)},
            "SK": {"S": schema.sk_state()},
        },
        ConsistentRead=True,
    )["Item"]


def test_a_soft_limit_goes_into_debt(sync_repository):
    sync_repository.set_resource_defaults(
        "llm", [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10, soft=True)]
    )
    limiter = SyncRateLimiter(repository=sync_repository)
    for _ in range(3):
        with limiter.acquire("u", "llm", {"rpm": 1, "tpm": 8}) as lease:
            pass
    assert lease.overdrawn == ["tpm"]
    assert int(_item(sync_repository, "u", "llm")["b_tpm_tk"]["N"]) < 0


def test_a_bypass_admits_and_debits_nothing(sync_repository):
    sync_repository.set_resource_defaults("api", [Limit.per_minute("rpm", 2)])
    limiter = SyncRateLimiter(repository=sync_repository)
    with limiter.acquire("u", "api", {"rpm": 1}):
        pass
    sync_repository.bypass_resource("api")
    for _ in range(5):
        with limiter.acquire("u", "api", {"rpm": 1}) as lease:
            assert lease.bypassed is True
    item = _item(sync_repository, "u", "api")
    assert int(item["b_rpm_tc"]["N"]) == 6_000
    assert int(item["b_rpm_tk"]["N"]) <= 1_000


def test_a_stamp_left_behind_stops_admitting_past_its_deadline(sync_repository):
    """Review D1, sync: a bypass a fan-out missed admits only until its deadline."""
    sync_repository.set_resource_defaults("llm", [Limit.per_minute("rpm", 2)])
    sync_repository.bypass_resource("llm")
    start = sync_repository._now_ms()
    with patch.object(sync_repository, "_now_ms", return_value=start):
        with SyncRateLimiter(repository=sync_repository).acquire("u", "llm", {"rpm": 1}):
            pass
    sync_repository._write_resource_config_flag("llm", schema.CONFIG_FIELD_DISABLED, None)
    admitted = 0
    later = start + schema.DEFAULT_STAMP_TTL_SECONDS * 1000 + 1
    with patch.object(sync_repository, "_now_ms", return_value=later):
        for _ in range(10):
            sync_repository.invalidate_config_cache()
            try:
                with SyncRateLimiter(repository=sync_repository).acquire("u", "llm", {"rpm": 1}):
                    admitted += 1
            except RateLimitExceeded:
                pass
    assert admitted <= 2
    assert "bypass" not in _item(sync_repository, "u", "llm")


def test_a_multi_resource_lease_scopes_bypassed(sync_repository):
    sync_repository.set_resource_defaults("llm", [Limit.per_minute("rpm", 10)])
    sync_repository.set_resource_defaults("api", [Limit.per_minute("rpm", 10)])
    sync_repository.bypass_resource("api")
    limiter = SyncRateLimiter(repository=sync_repository)
    with limiter.acquire("u", "llm", {"rpm": 1}, also={"api": {"rpm": 1}}) as lease:
        assert lease.bypassed is False
        assert lease.resource("api").bypassed is True


@pytest.mark.parametrize("disabled", [True, "bypass"])
def test_the_setters_take_both_access_values(sync_repository, disabled):
    sync_repository.set_resource_defaults("llm", [Limit.per_minute("rpm", 1)], disabled=disabled)
    assert sync_repository.get_resource_disabled("llm") == disabled

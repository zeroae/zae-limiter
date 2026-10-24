"""LocalStack coverage for soft limits and bypass (ADR-151, #467, #311).

moto evaluates condition expressions with its own engine; these run the
fast path's stamp terms, the bypass shape and an expired stamp against
LocalStack's DynamoDB (review D14). Every test reads the item back raw.
"""

import pytest

from zae_limiter import RateLimitExceeded, schema
from zae_limiter.models import Limit

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _item(repo, entity_id: str, resource: str) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, 0)},
            "SK": {"S": schema.sk_state()},
        },
        ConsistentRead=True,
    )
    return response["Item"]


def _n(item: dict, attr: str) -> int:
    return int(item[attr]["N"])


async def test_a_soft_limit_is_debited_into_debt_on_the_fast_path(localstack_limiter, test_repo):
    """The stamp term `(#o > :now AND attribute_exists(#oc))` admits a spent
    soft limit while the hard one beside it is still tested."""
    await test_repo.set_resource_defaults(
        "llm", [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10, soft=True)]
    )
    for _ in range(3):
        async with localstack_limiter.acquire("u", "llm", {"rpm": 1, "tpm": 8}) as lease:
            pass
    assert lease.overdrawn == ["tpm"]
    item = await _item(test_repo, "u", "llm")
    assert _n(item, "b_tpm_tk") < 0
    assert _n(item, "b_tpm_tc") == 24_000
    assert _n(item, "b_tpm_soft") > test_repo._now_ms()  # a live deadline


async def test_a_soft_limit_lease_adjusts_into_debt(localstack_limiter, test_repo):
    """#467 AC4: `consume()`, `adjust()` and `release()` on a soft limit are
    persisted like any other — `tc` counts net consumption, `tk` may go below
    zero — so usage snapshots, built from `tc`, stay exact."""
    await test_repo.set_resource_defaults("meter", [Limit.per_minute("tpm", 10, soft=True)])
    async with localstack_limiter.acquire("u", "meter", {"tpm": 5}) as lease:
        await lease.consume(tpm=20)  # past the balance: soft, admitted
        await lease.adjust(tpm=3)
        await lease.release(tpm=1)
    item = await _item(test_repo, "u", "meter")
    assert _n(item, "b_tpm_tc") == (5 + 20 + 3 - 1) * 1000
    assert _n(item, "b_tpm_tk") < 0


async def test_a_bypassed_bucket_counts_and_debits_nothing(localstack_limiter, test_repo):
    await test_repo.set_resource_defaults("api", [Limit.per_minute("rpm", 2)])
    async with localstack_limiter.acquire("u", "api", {"rpm": 1}):
        pass
    await test_repo.bypass_resource("api")
    for _ in range(5):
        async with localstack_limiter.acquire("u", "api", {"rpm": 1}) as lease:
            assert lease.bypassed is True
    item = await _item(test_repo, "u", "api")
    assert _n(item, "b_rpm_tc") == 6_000
    assert _n(item, "b_rpm_tk") <= 1_000  # the balance was not touched by the bypass


async def test_an_expired_or_boolean_stamp_is_enforced(localstack_limiter, test_repo):
    """A stamp past its deadline, or a pre-release `BOOL true`, admits nothing:
    `#byp > :now` is false for both on DynamoDB's own type rules."""
    await test_repo.set_resource_defaults("old", [Limit.per_minute("rpm", 2)])
    async with localstack_limiter.acquire("u", "old", {"rpm": 0}):
        pass
    client = await test_repo._get_client()
    key = {
        "PK": {"S": schema.pk_bucket(test_repo._namespace_id, "u", "old", 0)},
        "SK": {"S": schema.sk_state()},
    }
    for stale in ({"N": str(test_repo._now_ms() - 1)}, {"BOOL": True}):
        await client.update_item(
            TableName=test_repo.table_name,
            Key=key,
            UpdateExpression="SET bypass = :v, b_rpm_tk = :z",
            ExpressionAttributeValues={":v": stale, ":z": {"N": "0"}},
        )
        with pytest.raises(RateLimitExceeded):
            async with localstack_limiter.acquire("u", "old", {"rpm": 1}):
                pass
        assert "bypass" not in await _item(test_repo, "u", "old")  # removed

"""Records real_808.json: the #808 burst on the real library (moto, frozen clock).

Not collected by pytest from here. To re-record, copy into tests/unit/ of a checkout,
run `uv run pytest tests/unit/probe_808.py -n 0 -s --no-cov`, then delete the copy.
"""

import json

from zae_limiter import Limit, RateLimitExceeded
from zae_limiter.schema import pk_bucket, sk_state

T0 = 1_780_000_000_000
OUT = "real_808.json"


async def _raw(repo):
    client = await repo._get_client()
    key = {"PK": {"S": pk_bucket(repo._namespace_id, "u", "r", 0)}, "SK": {"S": sk_state()}}
    resp = await client.get_item(TableName=repo.table_name, Key=key, ConsistentRead=True)
    return resp["Item"]


async def test_trace_808(limiter):
    repo = limiter._repository
    now = [T0]
    repo._now_ms = lambda: now[0]
    repo._rejection_cache.ttl_seconds = 0
    lim = Limit.custom("rpm", capacity=300, refill_amount=300, refill_period_seconds=14_400)
    await repo.set_limits("u", [lim], resource="r")
    async with limiter.acquire("u", "r", {"rpm": 1}):
        pass
    now[0] += 86_400_000  # idle one day
    start = now[0]
    trace = []
    for _ in range(700):
        now[0] += 1  # 1000 requests per second
        ok = True
        try:
            async with limiter.acquire("u", "r", {"rpm": 1}):
                pass
        except RateLimitExceeded:
            ok = False
        item = await _raw(repo)
        trace.append(
            {
                "t_ms": now[0] - start,
                "admitted": ok,
                "tk": int(item["b_rpm_tk"]["N"]) / 1000,
                "rf_ms": int(item["rf"]["N"]) - start,
            }
        )
    with open(OUT, "w") as f:
        json.dump({"C": 300, "r_per_s": 300 / 14_400, "trace": trace}, f)

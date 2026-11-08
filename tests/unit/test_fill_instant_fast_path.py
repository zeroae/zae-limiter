"""The fill-instant fast path: ADR-154's two writes (#819).

On an item storing fill instants each hard limit, the reserved ``wcu``
included, gets the not-full write (``ADD fi c·S·rp`` under ``t·ra ≤ fi ≤ t·ra +
slack``) or the full write (``SET fi = t·ra + c·S·rp`` under ``fi < t·ra``),
chosen from the last image this process saw. A wrong guess fails, returns the
item, and is retried from it with the same stamp; a request still undecided
when its write budget (``L + 2``) runs out is the contended 429.

Not a sync-generation source: the sync twin of every path here is generated
from the same modules, and the #808 reproductions in ``test_limiter.py`` run on
both.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.fixtures.capacity import CapacityCounter, _counting_client
from zae_limiter import Limit, RateLimiter, RateLimitExceeded
from zae_limiter import fill_instant as fi
from zae_limiter.fill_instant import FILL_INSTANT_EXTRA_WRITES, Side
from zae_limiter.repository_protocol import SpeculativeFailureReason, SpeculativeResult
from zae_limiter.schema import (
    BUCKET_FIELD_FI,
    WCU_LIMIT_NAME,
    bucket_attr,
    pk_bucket,
    sk_state,
)

RESOURCE = "gpt-4"
T0 = 1_800_000_000_000
RPM = Limit.per_minute("rpm", 100)
TPM = Limit.per_minute("tpm", 10_000)
P_RPM = fi.ShardParams(100_000, 100_000, 60_000, 1)
P_TPM = fi.ShardParams(10_000_000, 10_000_000, 60_000, 1)
P_WCU = fi.ShardParams(1_000_000, 1_000_000, 1_000, 1)


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now


@pytest.fixture
def clock(limiter) -> Clock:
    clock = Clock(T0)
    limiter._repository._now_ms = lambda: clock.now
    return clock


def _key(repo, entity_id: str, resource: str = RESOURCE, shard: int = 0) -> dict[str, Any]:
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard)},
        "SK": {"S": sk_state()},
    }


async def _raw(repo, entity_id: str, resource: str = RESOURCE) -> dict[str, Any]:
    client = await repo._get_client()
    response = await client.get_item(TableName=repo.table_name, Key=_key(repo, entity_id, resource))
    return response.get("Item") or {}


async def _fi(repo, entity_id: str, limit: str, resource: str = RESOURCE) -> int:
    return int((await _raw(repo, entity_id, resource))[bucket_attr(limit, BUCKET_FIELD_FI)]["N"])


async def _set_fi(repo, entity_id: str, limit: str, value: int, resource: str = RESOURCE) -> None:
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=_key(repo, entity_id, resource),
        UpdateExpression="SET #f = :v",
        ExpressionAttributeNames={"#f": bucket_attr(limit, BUCKET_FIELD_FI)},
        ExpressionAttributeValues={":v": {"N": str(value)}},
    )


def claimed(write: dict[str, Any]) -> dict[str, str]:
    """Each fill instant a write moves, by limit name, and the side it claims."""
    names = write["ExpressionAttributeNames"]
    condition = write["ConditionExpression"]
    sides = {}
    i = 0
    while f"#zf{i}" in names:
        limit = names[f"#zf{i}"][len("b_") : -len("_fi")]
        sides[limit] = "full" if f"#zf{i} < :zs{i}" in condition else "not_full"
        i += 1
    return sides


class Recorder:
    """Every admission write of the fill-instant fast path, and a hook run before each.

    The hook receives the write about to land, so a test can play another
    consumer that lands first.
    """

    def __init__(self, repo) -> None:
        self.repo = repo
        self.writes: list[dict[str, Any]] = []
        self.before: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._real = repo._update_item_retrying_conflict
        repo._update_item_retrying_conflict = self._send

    async def _send(self, client, **kwargs):
        names = kwargs.get("ExpressionAttributeNames", {})
        if "#zf0" in names and "#zxi" in names and "ReturnValues" in kwargs:
            if self.before is not None:
                await self.before(kwargs)
            self.writes.append(kwargs)
        return await self._real(client, **kwargs)

    def sides(self, index: int) -> dict[str, str]:
        return claimed(self.writes[index])

    def on(self, entity_id: str, resource: str = RESOURCE) -> list[dict[str, Any]]:
        pk = pk_bucket(self.repo._namespace_id, entity_id, resource, 0)
        return [w for w in self.writes if w["Key"]["PK"]["S"] == pk]


def opposite(params: fi.ShardParams, t_ms: int, side: str) -> int:
    """A fill instant on the other side of ``t_ms`` from ``side``, within one request's slack."""
    if side == "full":
        return fi.stamp(t_ms, params) + fi.span(1_000, params)
    return fi.stamp(t_ms, params) - 1


async def _warm(limiter, entity_id: str, limits: list[Limit], resource: str = RESOURCE) -> None:
    """Create the bucket (slow path) and teach this process its image (one fast write)."""
    await limiter._repository.set_limits(entity_id, limits, resource=resource)
    async with limiter.acquire(entity_id, resource, consume={limits[0].name: 1}):
        pass
    async with limiter.acquire(entity_id, resource, consume={limits[0].name: 1}):
        pass


class TestOneWrite:
    async def test_a_predicted_admission_is_one_update_and_no_read(self, limiter, clock) -> None:
        await _warm(limiter, "e1", [RPM])
        clock.now = T0 + 1_000
        counter = CapacityCounter()
        with _counting_client(counter, limiter):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                pass
        assert counter.update_item == 1
        assert counter.get_item == counter.query == 0
        assert counter.batch_get_item == counter.transact_write_items == []

    async def test_it_records_max_z_t_plus_c_over_r(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        before = await _fi(repo, "e1", "rpm")
        clock.now = T0 + 3_600_000  # an hour idle: the bucket is full again
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 7}):
            pass
        after = await _fi(repo, "e1", "rpm")
        assert before < fi.stamp(clock.now, P_RPM)
        assert after == fi.stamp(clock.now, P_RPM) + fi.span(7_000, P_RPM)


class TestRetries:
    async def test_every_write_of_a_request_carries_the_same_stamp(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        recorder = Recorder(repo)
        clock.now = T0 + 3_600_000

        async def overtake(write: dict[str, Any]) -> None:
            if not recorder.writes:  # another consumer lands first, once
                await _set_fi(repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + 1)

        recorder.before = overtake
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            clock.now += 5_000  # the clock moves on while the request retries
        assert len(recorder.writes) == 2
        stamps = {w["ExpressionAttributeValues"][":zs0"]["N"] for w in recorder.writes}
        assert len(stamps) == 1

    async def test_decided_within_two_writes_when_nothing_lands_between(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        recorder = Recorder(repo)
        # A stale image showing full, overtaken once: the retry is chosen from
        # the truth the failure returned and decides.
        clock.now = T0 + 3_600_000

        async def overtake(write: dict[str, Any]) -> None:
            if not recorder.writes:
                await _set_fi(repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + 10)

        recorder.before = overtake
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        assert len(recorder.writes) == 2
        assert recorder.sides(0)["rpm"] == "full"
        assert recorder.sides(1)["rpm"] == "not_full"

    async def test_decided_within_l_plus_two_when_only_admissions_land(
        self, limiter, clock
    ) -> None:
        """Theorem 4: each write, another consumer turns one more limit not full."""
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM, TPM])
        recorder = Recorder(repo)
        clock.now = T0 + 3_600_000  # every limit full, `wcu` too
        flips = [("rpm", P_RPM), ("tpm", P_TPM), (WCU_LIMIT_NAME, P_WCU)]

        async def admit_elsewhere(write: dict[str, Any]) -> None:
            if len(recorder.writes) < len(flips):
                name, params = flips[len(recorder.writes)]
                await _set_fi(
                    repo, "e1", name, fi.stamp(clock.now, params) + fi.span(1_000, params)
                )

        recorder.before = admit_elsewhere
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 1}):
            pass
        limits = 3  # rpm, tpm and wcu
        assert len(recorder.writes) <= limits + FILL_INSTANT_EXTRA_WRITES

    async def test_overtaken_until_the_cap_is_contended_and_debits_nothing(
        self, limiter, clock
    ) -> None:
        """Theorem 3(c): returns landing between writes can sustain the doubt."""
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        recorder = Recorder(repo)
        clock.now = T0 + 3_600_000

        async def flip(write: dict[str, Any]) -> None:
            # Always the opposite of what the write about to land claims.
            await _set_fi(repo, "e1", "rpm", opposite(P_RPM, clock.now, claimed(write)["rpm"]))

        recorder.before = flip
        with pytest.raises(RateLimitExceeded) as raised:
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                pass
        assert raised.value.contended
        assert not raised.value.violations
        assert len(recorder.writes) == 2 + FILL_INSTANT_EXTRA_WRITES  # rpm and wcu
        # Every write failed: the fill instant is the other consumer's.
        last = claimed(recorder.writes[-1])["rpm"]
        assert await _fi(repo, "e1", "rpm") == opposite(P_RPM, clock.now, last)

    async def test_a_multi_resource_acquire_counts_every_resources_limits(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        # Both configured first: an admin write clears this process's images.
        await repo.set_limits("e1", [RPM], resource="a")
        await repo.set_limits("e1", [TPM], resource="b")
        for resource, name in (("a", "rpm"), ("b", "tpm")):
            for _ in range(2):  # create, then one fast write for the image
                async with limiter.acquire("e1", resource, consume={name: 1}):
                    pass
        recorder = Recorder(repo)
        clock.now = T0 + 3_600_000

        async def flip(write: dict[str, Any]) -> None:
            for resource, name, params in (("a", "rpm", P_RPM), ("b", "tpm", P_TPM)):
                side = claimed(write).get(name)
                if side is not None:
                    await _set_fi(
                        repo, "e1", name, opposite(params, clock.now, side), resource=resource
                    )

        recorder.before = flip
        with pytest.raises(RateLimitExceeded) as raised:
            async with limiter.acquire("e1", "a", consume={"rpm": 1}, also={"b": {"tpm": 1}}):
                pass
        assert raised.value.contended
        # One budget for the acquire: 2 + (rpm, wcu) + (tpm, wcu).
        assert len(recorder.writes) == FILL_INSTANT_EXTRA_WRITES + 2 + 2


class TestDecisionsWithoutAWrite:
    async def test_a_request_above_the_share_sends_nothing(self, limiter, clock) -> None:
        await _warm(limiter, "e1", [RPM])
        limiter._repository._rejection_cache.clear()  # not the rejection cache's answer
        counter = CapacityCounter()
        with _counting_client(counter, limiter):
            with pytest.raises(RateLimitExceeded) as raised:
                async with limiter.acquire("e1", RESOURCE, consume={"rpm": 101}):
                    pass
        assert [v.limit_name for v in raised.value.violations] == ["rpm"]
        assert counter.update_item == counter.get_item == counter.put_item == 0
        assert counter.batch_get_item == counter.transact_write_items == []

    async def test_an_image_showing_full_and_a_busy_shard_sends_not_full_first(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        fill_key = (repo._namespace_id, "e1", RESOURCE, 0)
        for _ in range(16):
            repo._fill_images.observe(fill_key, "rpm", True)
        assert (repo._fill_images.utilisation(fill_key, "rpm") or 0) > 500
        recorder = Recorder(repo)
        clock.now = T0 + 3_600_000  # the image shows full
        await _set_fi(repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + 5)  # it is not
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        assert recorder.sides(0)["rpm"] == "not_full"
        assert len(recorder.writes) == 1


class TestSoftLimits:
    async def test_admitted_by_hard_limits_and_debited_after(self, limiter, clock) -> None:
        repo = limiter._repository
        soft_tpm = Limit.per_minute("tpm", 10, soft=True)
        await repo.set_limits("e1", [RPM, soft_tpm], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 1}):
            pass
        recorder = Recorder(repo)
        clock.now = T0 + 1_000
        # Far beyond the soft limit's capacity: admitted, never exceeded.
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 25}):
            pass
        admission, *debits = recorder.writes
        assert (
            bucket_attr("tpm", BUCKET_FIELD_FI)
            not in admission["ExpressionAttributeNames"].values()
        )
        assert len(debits) == 1
        assert debits[0]["ExpressionAttributeNames"]["#zf0"] == bucket_attr("tpm", BUCKET_FIELD_FI)
        balance = fi.balance_milli(
            await _fi(repo, "e1", "tpm"), clock.now, fi.ShardParams(10_000, 10_000, 60_000, 1)
        )
        assert balance < 0


class TestCascade:
    async def test_an_undecided_parent_refunds_the_child(self, limiter, clock) -> None:
        repo = limiter._repository
        await limiter.create_entity("org")
        await limiter.create_entity("user", parent_id="org", cascade=True)
        await repo.set_limits("org", [RPM], resource=RESOURCE)
        await repo.set_limits("user", [RPM], resource=RESOURCE)
        for _ in range(3):  # create both, then teach this process both images
            async with limiter.acquire("user", RESOURCE, consume={"rpm": 1}):
                pass
        child_before = await _fi(repo, "user", "rpm")
        recorder = Recorder(repo)
        clock.now = T0 + 3_600_000
        org_pk = pk_bucket(repo._namespace_id, "org", RESOURCE, 0)

        async def overtake_parent(write: dict[str, Any]) -> None:
            # Only the parent's writes are overtaken; the child's lands.
            if write["Key"]["PK"]["S"] == org_pk:
                await _set_fi(repo, "org", "rpm", opposite(P_RPM, clock.now, claimed(write)["rpm"]))

        recorder.before = overtake_parent
        with pytest.raises(RateLimitExceeded) as raised:
            async with limiter.acquire("user", RESOURCE, consume={"rpm": 1}):
                pass
        assert raised.value.contended
        # The child's debit, if it landed, was returned: its balance at the
        # request's instant is what it was before the request.
        after = await _fi(repo, "user", "rpm")
        assert fi.balance_milli(after, clock.now, P_RPM) == fi.balance_milli(
            child_before, clock.now, P_RPM
        )


def test_the_cap_is_a_named_constant() -> None:
    assert FILL_INSTANT_EXTRA_WRITES == 2
    assert Side.FULL is not Side.NOT_FULL


async def _attrs(repo, entity_id: str, expression: str, names: dict, values: dict | None = None):
    client = await repo._get_client()
    kwargs: dict[str, Any] = {
        "TableName": repo.table_name,
        "Key": _key(repo, entity_id),
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names,
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    await client.update_item(**kwargs)


class TestRouting:
    """What the two writes cannot express goes elsewhere, decided from the image."""

    async def _fill(self, limiter, consume=None):
        repo = limiter._repository
        return await repo.speculative_consume("e1", RESOURCE, consume or {"rpm": 1}, shard_id=0)

    async def test_a_missing_item_is_missing(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        client = await repo._get_client()
        await client.delete_item(TableName=repo.table_name, Key=_key(repo, "e1"))
        result = await self._fill(limiter)
        assert result.failure_reason is SpeculativeFailureReason.BUCKET_MISSING
        assert repo._fill_images.get((repo._namespace_id, "e1", RESOURCE, 0)) is None

    @pytest.mark.parametrize(
        ("attr", "value"),
        [("disabled", {"BOOL": True}), ("ttl", {"N": "1"}), ("rx", {"N": str(T0 + 60_000)})],
        ids=["disabled", "expired-ttl", "live-raise-marker"],
    )
    async def test_a_stale_cached_image_is_asked_again(self, limiter, clock, attr, value) -> None:
        """#821: a route decided from this process's cached image sends no write,
        and only a fast-path write refreshes the image. Trusted, a stamp lifted
        since (by another process, or by the slow pass that fixed the item)
        kept the shard off the fast path, or answered 403, for good. The image
        is forgotten and today's write asks the item: one refused write, then
        the fast path admits, with no read."""
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        key = (repo._namespace_id, "e1", RESOURCE, 0)
        cached = repo._fill_images.get(key)
        assert cached is not None
        repo._fill_images.store(key, {**cached.item, attr: value})  # stale
        counter = CapacityCounter()
        with _counting_client(counter, limiter):
            result = await self._fill(limiter)
        assert result.success
        assert counter.update_item == 2  # today's write, refused on `xi`; then the pair
        assert counter.get_item == counter.query == 0 and counter.batch_get_item == []
        refreshed = repo._fill_images.get(key)
        assert refreshed is not None and attr not in refreshed.item

    @pytest.mark.parametrize(
        ("expression", "names", "values", "reason"),
        [
            ("SET #d = :t", {"#d": "disabled"}, {":t": {"BOOL": True}}, "DISABLED"),
            ("REMOVE #x", {"#x": "xi"}, None, "SCHEDULE_BOUNDARY"),
            ("SET #b = :past", {"#b": "bypass"}, {":past": {"N": "1"}}, "SCHEDULE_BOUNDARY"),
            ("SET #t = :old", {"#t": "ttl"}, {":old": {"N": "1"}}, "SCHEDULE_BOUNDARY"),
            (
                "SET #s = :past",
                {"#s": "b_rpm_soft"},
                {":past": {"N": "1"}},
                "SCHEDULE_BOUNDARY",
            ),
        ],
        ids=["disabled", "no-xi", "expired-bypass", "expired-ttl", "expired-soft"],
    )
    async def test_each_guard_routes_from_the_image(
        self, limiter, clock, expression, names, values, reason
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        await _attrs(repo, "e1", expression, names, values)
        # The cached image is stale; the first write fails and its ALL_OLD routes.
        result = await self._fill(limiter)
        assert result.failure_reason is getattr(SpeculativeFailureReason, reason)

    async def test_a_closed_vu_window_waits_only_for_a_quota(self, limiter, clock) -> None:
        """ADR-155: a fill instant is never pinned on `vu`; a quota's `tk` still is."""
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM, Limit.quota("rpd", 50, cron="0 0 * * *")])
        await _attrs(repo, "e1", "SET #v = :past", {"#v": "vu"}, {":past": {"N": "5"}})
        repo._fill_images.clear()
        alone = await self._fill(limiter, {"rpm": 1})
        assert alone.success
        await _attrs(repo, "e1", "SET #v = :past", {"#v": "vu"}, {":past": {"N": "5"}})
        with_quota = await self._fill(limiter, {"rpm": 1, "rpd": 1})
        assert with_quota.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_a_limit_missing_from_the_item_goes_to_the_slow_path(
        self, limiter, clock
    ) -> None:
        await _warm(limiter, "e1", [RPM])
        result = await self._fill(limiter, {"rpm": 1, "tpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_a_boundary_since_xi_goes_to_the_slow_path(self, limiter, clock) -> None:
        from zae_limiter import ScheduleEntry

        midnight = 1_800_057_600_000
        clock.now = midnight - 120_000
        scheduled = Limit.per_minute("rpm", 100).with_schedule(
            (ScheduleEntry(cron="* 0 * * *", scale=0.5),)
        )
        await _warm(limiter, "e1", [scheduled])
        clock.now = midnight + 60_000
        result = await self._fill(limiter)
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_a_short_quota_beside_a_fill_instant_rejects(self, limiter, clock) -> None:
        quota = Limit.quota("rpd", 2, cron="0 0 * * *")
        await _warm(limiter, "e1", [RPM, quota])
        await _attrs(
            limiter._repository, "e1", "SET #q = :zero", {"#q": "b_rpd_tk"}, {":zero": {"N": "0"}}
        )
        result = await self._fill(limiter, {"rpm": 1, "rpd": 1})
        assert result.failure_reason is SpeculativeFailureReason.APP_LIMIT_EXHAUSTED

    async def test_an_oversize_request_on_a_stale_image_refreshes_it(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        fill_key = (repo._namespace_id, "e1", RESOURCE, 0)
        image = repo._fill_images.get(fill_key)
        assert image is not None
        image.stored_at -= 3_600  # older than the config cache TTL
        recorder = Recorder(repo)
        result = await self._fill(limiter, {"rpm": 101})
        assert result.failure_reason is SpeculativeFailureReason.APP_LIMIT_EXHAUSTED
        assert len(recorder.writes) == 1  # the write that must fail, for a fresh image

    async def test_a_bypassed_item_counts_and_debits_only_wcu(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        before = await _fi(repo, "e1", "rpm")
        await _attrs(
            repo, "e1", "SET #b = :live", {"#b": "bypass"}, {":live": {"N": str(T0 + 60_000)}}
        )
        result = await self._fill(limiter, {"rpm": 50})
        assert result.success and result.bypassed
        assert await _fi(repo, "e1", "rpm") == before

    async def test_a_transaction_conflict_is_contention(self, limiter, clock) -> None:
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])

        async def conflicted(client, **kwargs):
            raise ClientError(
                {"Error": {"Code": "TransactionConflictException", "Message": "busy"}}, "UpdateItem"
            )

        repo._update_item_retrying_conflict = conflicted
        result = await self._fill(limiter)
        assert result.failure_reason is SpeculativeFailureReason.CONTENTION

    async def test_any_other_error_is_raised(self, limiter, clock) -> None:
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])

        async def broken(client, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem"
            )

        repo._update_item_retrying_conflict = broken
        with pytest.raises(ClientError):
            await self._fill(limiter)

    async def test_an_undecided_shard_probe_is_contended(self, limiter, clock) -> None:
        await _warm(limiter, "e1", [RPM])
        first = await self._fill(limiter)

        async def undecided(*args, **kwargs):
            return SpeculativeResult(
                success=False,
                old_buckets=first.buckets,
                shard_id=1,
                shard_count=2,
                failure_reason=SpeculativeFailureReason.UNDECIDED,
            )

        limiter._repository.speculative_consume = undecided
        exhausted = SpeculativeResult(
            success=False,
            old_buckets=first.buckets,
            shard_id=0,
            shard_count=2,
            failure_reason=SpeculativeFailureReason.APP_LIMIT_EXHAUSTED,
        )
        with pytest.raises(RateLimitExceeded) as raised:
            await limiter._retry_on_other_shard(
                "e1", RESOURCE, {"rpm": 1}, None, exhausted, clock.now
            )
        assert raised.value.contended


class TestSoftDebitGivesUp:
    async def _setup(self, limiter):
        repo = limiter._repository
        soft_tpm = Limit.per_minute("tpm", 10, soft=True)
        await repo.set_limits("e1", [RPM, soft_tpm], resource=RESOURCE)
        for _ in range(2):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 1}):
                pass
        return repo

    async def test_after_its_attempts_only_reporting_is_lost(self, limiter, clock, caplog) -> None:
        repo = await self._setup(limiter)
        before = await _fi(repo, "e1", "tpm")
        real = repo._update_item_retrying_conflict

        async def soft_debit_fails(client, **kwargs):
            names = kwargs.get("ExpressionAttributeNames", {})
            if "#disabled" not in names and "#zf0" in names:  # the debit, not the admission
                await _set_fi(repo, "e1", "tpm", fi.stamp(clock.now, P_TPM) - 1)
                bump = {
                    **kwargs,
                    "ConditionExpression": kwargs["ConditionExpression"]
                    + " AND attribute_not_exists(PK)",
                }
                return await real(client, **bump)
            return await real(client, **kwargs)

        repo._update_item_retrying_conflict = soft_debit_fails
        clock.now = T0 + 1_000
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 3}):
            pass
        assert "soft debit given up" in caplog.text
        assert await _fi(repo, "e1", "tpm") != before + 1  # never debited

    async def test_a_backend_error_keeps_the_admission(self, limiter, clock, caplog) -> None:
        from botocore.exceptions import ClientError

        repo = await self._setup(limiter)
        real = repo._update_item_retrying_conflict

        async def soft_debit_breaks(client, **kwargs):
            names = kwargs.get("ExpressionAttributeNames", {})
            if "#disabled" not in names and "#zf0" in names:
                raise ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem"
                )
            return await real(client, **kwargs)

        repo._update_item_retrying_conflict = soft_debit_breaks
        clock.now = T0 + 1_000
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 3}):
            pass
        assert "soft debit failed" in caplog.text

    @pytest.mark.parametrize("mutation", ["no-xi", "gone"])
    async def test_it_stops_on_what_it_cannot_debit(self, limiter, clock, caplog, mutation) -> None:
        repo = await self._setup(limiter)
        item = await _raw(repo, "e1")
        states = repo._deserialize_composite_bucket(item)
        if mutation == "no-xi":
            item = {k: v for k, v in item.items() if k != "xi"}
        else:
            item = {k: v for k, v in item.items() if not k.startswith("b_tpm_")}
        result = await repo._debit_soft_fill("e1", RESOURCE, 0, {"tpm": 1_000}, item, states)
        assert result == states
        assert "soft debit given up" in caplog.text


class TestReturns:
    async def test_nothing_to_return_sends_nothing(self, limiter) -> None:
        assert await limiter._repository.return_fill_instants(
            "e1", RESOURCE, 0, {}, expressed_ms=T0, shard_count=1
        )

    async def test_a_return_whose_pins_moved_is_dropped(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        item = await _raw(repo, "e1")
        (rpm,) = [s for s in repo._deserialize_composite_bucket(item) if s.limit_name == "rpm"]
        before = await _fi(repo, "e1", "rpm")
        assert not await repo.return_fill_instants(
            "e1", RESOURCE, 0, {"rpm": (1_000, rpm)}, expressed_ms=T0 - 1, shard_count=1
        )
        assert await _fi(repo, "e1", "rpm") == before

    async def test_any_other_error_is_raised(self, limiter, clock) -> None:
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        item = await _raw(repo, "e1")
        (rpm,) = [s for s in repo._deserialize_composite_bucket(item) if s.limit_name == "rpm"]

        async def broken(client, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem"
            )

        repo._update_item_retrying_conflict = broken
        with pytest.raises(ClientError):
            await repo.return_fill_instants(
                "e1", RESOURCE, 0, {"rpm": (1_000, rpm)}, expressed_ms=T0, shard_count=1
            )

    async def test_a_failed_lease_return_is_raised_after_the_adjustment(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])

        async def failing(*args, **kwargs):
            raise RuntimeError("return failed")

        repo.return_fill_instants = failing
        with pytest.raises(RuntimeError, match="return failed"):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 5}) as lease:
                await lease.release(rpm=2)

    async def test_a_failed_rollback_return_is_logged(self, limiter, clock, caplog) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])

        async def failing(*args, **kwargs):
            raise RuntimeError("return failed")

        repo.return_fill_instants = failing
        with pytest.raises(KeyError):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 5}):
                raise KeyError("caller")
        assert "Failed to return a fill instant on rollback" in caplog.text

    async def test_a_failed_compensation_is_logged(self, limiter, clock, caplog) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        first = await repo.speculative_consume("e1", RESOURCE, {"rpm": 1}, shard_id=0)

        async def failing(*args, **kwargs):
            raise RuntimeError("return failed")

        repo.return_fill_instants = failing
        await limiter._compensate_speculative("e1", RESOURCE, {"rpm": 1}, 0, first.buckets)
        assert "Fill-instant compensation failed" in caplog.text

    async def test_compensation_returns_fill_instants_and_credits_a_quota(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        quota = Limit.quota("rpd", 50, cron="0 0 * * *")
        await _warm(limiter, "e1", [RPM, quota])
        before = await _fi(repo, "e1", "rpm")
        tk_before = int((await _raw(repo, "e1"))["b_rpd_tk"]["N"])
        landed = await repo.speculative_consume("e1", RESOURCE, {"rpm": 1, "rpd": 1}, shard_id=0)
        assert landed.success
        await limiter._compensate_speculative(
            "e1", RESOURCE, {"rpm": 1, "rpd": 1}, 0, landed.buckets
        )
        assert await _fi(repo, "e1", "rpm") == before
        assert int((await _raw(repo, "e1"))["b_rpd_tk"]["N"]) == tk_before

    async def test_a_lease_without_a_known_xi_returns_nothing(self, limiter, clock) -> None:
        await _warm(limiter, "e1", [RPM])
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 5}) as lease:
            for entry in lease.entries:
                entry.state.stored_expressed_ms = None
            assert lease._fill_returns(lambda e: -1_000) == []


class TestImageCache:
    def test_it_is_bounded_oldest_first(self) -> None:
        from zae_limiter.fill_images import FillImageCache

        with pytest.raises(ValueError, match="max_entries"):
            FillImageCache(max_entries=0)
        cache = FillImageCache(max_entries=2)
        for shard in range(3):
            cache.store(("ns", "e", "r", shard), {"shard": shard})
        assert len(cache) == 2
        assert cache.get(("ns", "e", "r", 0)) is None
        cache.observe(("ns", "e", "r", 0), "rpm", True)  # an evicted shard learns nothing
        assert cache.utilisation(("ns", "e", "r", 0), "rpm") is None


class TestSlowPathImage:
    """A shard this process wrote on the slow path is known to its fast path (#821)."""

    async def test_the_first_fast_request_after_a_create_is_one_write(self, limiter, clock) -> None:
        await limiter._repository.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):  # the slow path creates it
            pass
        counter = CapacityCounter()
        with _counting_client(counter, limiter):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                pass
        assert counter.update_item == 1  # not today's write first, refused on `xi`
        assert counter.batch_get_item == counter.transact_write_items == []

    async def test_a_slow_update_leaves_the_item_it_wrote(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        key = (repo._namespace_id, "e1", RESOURCE, 0)
        repo._fill_images.forget(key)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        clock.now = T0 + 1_000
        async with slow.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        image = repo._fill_images.get(key)
        assert image is not None and image.item == await _raw(repo, "e1")

    def test_only_bucket_items_storing_fill_instants_are_kept(self) -> None:
        from zae_limiter.fill_images import FillImageCache
        from zae_limiter.lease import _remember_fill_images

        class Backend:
            _fill_images = FillImageCache()

        legacy = {"PK": {"S": "ns/BUCKET#e#r#0"}, "SK": {"S": "#STATE"}}  # no `xi`
        other = {"PK": {"S": "ns/ENTITY#e"}, "SK": {"S": "#META"}, "xi": {"N": "1"}}
        backend = Backend()
        _remember_fill_images(backend, [{"Put": {"Item": legacy}}], other)
        assert len(backend._fill_images) == 0
        # A backend without the cache keeps nothing, and does not fail.
        _remember_fill_images(object(), [{"Put": {"Item": legacy}}], None)


class TestMixedForms:
    async def test_a_wcu_still_stored_as_tk_is_debited_as_tk(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        await _attrs(
            repo,
            "e1",
            "SET #wt = :full REMOVE #wf",
            {"#wt": "b_wcu_tk", "#wf": "b_wcu_fi"},
            {":full": {"N": "1000000"}},
        )
        repo._fill_images.clear()
        result = await repo.speculative_consume("e1", RESOURCE, {"rpm": 1}, shard_id=0)
        assert result.success
        assert int((await _raw(repo, "e1"))["b_wcu_tk"]["N"]) == 999_000

    async def test_a_boundary_since_xi_with_no_vu_goes_to_the_slow_path(
        self, limiter, clock
    ) -> None:
        from zae_limiter import ScheduleEntry

        repo = limiter._repository
        midnight = 1_800_057_600_000
        clock.now = midnight - 120_000
        scheduled = Limit.per_minute("rpm", 100).with_schedule(
            (ScheduleEntry(cron="* 0 * * *", scale=0.5),)
        )
        await _warm(limiter, "e1", [scheduled])
        await _attrs(repo, "e1", "REMOVE #v", {"#v": "vu"})
        repo._fill_images.clear()
        clock.now = midnight + 60_000
        result = await repo.speculative_consume("e1", RESOURCE, {"rpm": 1}, shard_id=0)
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

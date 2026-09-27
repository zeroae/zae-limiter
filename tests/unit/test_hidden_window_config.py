"""A ``reset_after`` limit's config is stored where pre-v0.15 readers do not look (#640).

Option B of #638. A reader predating ADR-139 discovers config limits by the
``l_`` prefix alone and cannot reconstruct a session quota, so a level holding
one failed as a whole for it: every acquire raised under
``on_unavailable=block`` and went unlimited under ``allow``. Stored under
``w_`` instead, the limit is invisible to that reader, which keeps enforcing
the level's other limits.

The v0.14.0 discovery rules are restated here verbatim (``_v014_*``) so the
contract is pinned without installing the old release; the behaviour of the
real release was verified separately against zae-limiter 0.14.0 from PyPI.

Not a sync-generation source: every path exercised is the async repository,
and its sync twin is generated from the same module.
"""

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.fixtures.sharding import pinned_shard, spendable
from tests.fixtures.windows import SESSION_10, T0
from zae_limiter import Limit, RateLimiterUnavailable, ScheduleEntry
from zae_limiter.models import BucketState
from zae_limiter.schema import (
    LIMIT_FIELD_RSA,
    config_limit_names,
    limit_attr,
    parse_limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
)
from zae_limiter_provisioner.applier import apply_changes
from zae_limiter_provisioner.bucket_sync import _decode_limits
from zae_limiter_provisioner.differ import Change

RESOURCE = "gpt-4"
RPM = Limit.per_minute("rpm", 100)
SESSION = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
SESSION_SCHEDULED = SESSION.with_schedule((ScheduleEntry(cron="* 0-6 * * *", scale=0.5, tz="UTC"),))


def _v014_client_limit_names(item: dict[str, Any]) -> list[str]:
    """``Repository._deserialize_composite_limits``'s discovery loop at v0.14.0."""
    names = []
    suffix = "_cp"
    for attr_name in item:
        if attr_name.startswith("l_") and attr_name.endswith(suffix):
            name = attr_name[len("l_") : -len(suffix)]
            if name:
                names.append(name)
    return names


def _v014_provisioner_limit_names(item: dict[str, Any]) -> list[str]:
    """``bucket_sync._decode_limits`` at v0.14.0: ``parse_limit_attr`` keyed on ``l_``,
    keeping only names that carry all of cp/ra/rp."""
    fields: dict[str, set[str]] = {}
    for attr_name in item:
        if not attr_name.startswith("l_"):
            continue
        rest = attr_name[len("l_") :]
        idx = rest.rfind("_")
        if idx <= 0:
            continue
        fields.setdefault(rest[:idx], set()).add(rest[idx + 1 :])
    return [name for name, got in fields.items() if {"cp", "ra", "rp"} <= got]


async def _raw(repo, pk, sk):
    client = await repo._get_client()
    response = await client.get_item(TableName=repo.table_name, Key={"PK": pk, "SK": sk})
    return response.get("Item") or {}


async def _stored_level(repo, level):
    """Write ``[RPM, SESSION_SCHEDULED]`` at ``level`` and return the raw item."""
    limits = [RPM, SESSION_SCHEDULED]
    if level == "system":
        await repo.set_system_defaults(limits)
        return await _raw(repo, {"S": pk_system(repo._namespace_id)}, {"S": sk_config()})
    if level == "resource":
        await repo.set_resource_defaults(RESOURCE, limits)
        return await _raw(
            repo, {"S": pk_resource(repo._namespace_id, RESOURCE)}, {"S": sk_config()}
        )
    await repo.set_limits("u", limits, resource=RESOURCE)
    return await _raw(repo, {"S": pk_entity(repo._namespace_id, "u")}, {"S": sk_config(RESOURCE)})


def _session_attrs(item):
    return {a for a in item if parse_limit_attr(a) and parse_limit_attr(a)[0] == "session"}


def _applier_item(level, target):
    client = MagicMock()
    decl = {
        "session": {
            "capacity": 10_000,
            "refill_amount": 0,
            "refill_period": 1,
            "reset_after_seconds": 18_000,
            "schedule": [{"cron": "* 0-6 * * *", "tz": "UTC", "scale": 0.5}],
        },
        "rpm": {"capacity": 100, "refill_amount": 100, "refill_period": 60},
    }
    result = apply_changes(
        [Change(action="update", level=level, target=target, data={"limits": decl})],
        table_name="test",
        namespace_id="ns",
        client=client,
    )
    assert result.errors == []
    return client.put_item.call_args.kwargs["Item"]


class TestNothingOfAWindowLimitIsStoredUnderL:
    """The one property the old readers depend on: no ``l_`` attribute at all."""

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    async def test_through_the_repository(self, limiter, level):
        item = await _stored_level(limiter._repository, level)
        session = _session_attrs(item)
        assert session == {
            "w_session_cp",
            "w_session_ra",
            "w_session_rp",
            "w_session_rsa",
            "w_session_sched",
        }
        assert not any(a.startswith("l_session") for a in item)
        # The ordinary limit beside it is untouched.
        assert {"l_rpm_cp", "l_rpm_ra", "l_rpm_rp"} <= item.keys()

    @pytest.mark.parametrize(
        "level,target", [("system", "system"), ("resource", RESOURCE), ("entity", "u/gpt-4")]
    )
    def test_through_the_provisioner_applier(self, level, target):
        item = _applier_item(level, target)
        assert not any(a.startswith("l_session") for a in item)
        assert {"w_session_cp", "w_session_rsa", "w_session_sched"} <= item.keys()
        assert {"l_rpm_cp", "l_rpm_ra", "l_rpm_rp"} <= item.keys()


class TestTheOldDiscoveryRulesSeeOnlyTheOrdinaryLimit:
    """v0.14.0's two rules, applied to what v0.15 now stores."""

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    async def test_client_rule(self, limiter, level):
        item = await _stored_level(limiter._repository, level)
        assert _v014_client_limit_names(item) == ["rpm"]

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    async def test_provisioner_rule(self, limiter, level):
        item = await _stored_level(limiter._repository, level)
        assert _v014_provisioner_limit_names(item) == ["rpm"]

    def test_the_applier_item_too(self):
        item = _applier_item("entity", "u/gpt-4")
        assert _v014_client_limit_names(item) == ["rpm"]
        assert _v014_provisioner_limit_names(item) == ["rpm"]


class TestTheV15ReadersSeeBoth:
    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    async def test_a_mixed_level_round_trips_unchanged(self, limiter, level):
        repo = limiter._repository
        await _stored_level(repo, level)
        await repo.invalidate_config_cache()
        if level == "system":
            stored, _on_unavailable = await repo.get_system_defaults()
        elif level == "resource":
            stored = await repo.get_resource_defaults(RESOURCE)
        else:
            stored = await repo.get_limits("u", resource=RESOURCE)
        assert sorted(stored, key=lambda limit: limit.name) == [RPM, SESSION_SCHEDULED]

    async def test_resolution_and_an_acquire_see_the_hidden_limit(self, limiter):
        repo = limiter._repository
        await repo.set_limits("u", [RPM, SESSION_10], resource=RESOURCE)
        resolved, _unavailable, source = await repo.resolve_limits("u", RESOURCE)
        assert source == "entity"
        assert {limit.name for limit in resolved} == {"rpm", "session"}
        async with limiter.acquire("u", RESOURCE, consume={"rpm": 1, "session": 4}) as lease:
            assert lease.consumed == {"rpm": 1, "session": 4}

    def test_the_provisioner_decodes_both(self):
        item = _applier_item("entity", "u/gpt-4")
        decoded = _decode_limits(item)
        assert decoded["session"]["reset_after_seconds"] == 18_000
        assert decoded["session"]["capacity"] == 10_000
        assert len(decoded["session"]["schedule"]) == 1
        assert decoded["rpm"]["capacity"] == 100

    async def test_a_limit_stored_under_l_with_rsa_is_still_read(self, limiter):
        """Written by an unreleased v0.15 build before #640. Read as it always
        was; the next write of the level moves it to ``w_`` (full replace)."""
        repo = limiter._repository
        key = {
            "PK": {"S": pk_entity(repo._namespace_id, "u")},
            "SK": {"S": sk_config(RESOURCE)},
        }
        legacy = {
            **key,
            "entity_id": {"S": "u"},
            "resource": {"S": RESOURCE},
            "config_version": {"N": "1"},
            "l_session_cp": {"N": "10000"},
            "l_session_ra": {"N": "0"},
            "l_session_rp": {"N": "1"},
            "l_session_rsa": {"N": "18000"},
        }
        client = await repo._get_client()
        await client.put_item(TableName=repo.table_name, Item=legacy)
        assert await repo.get_limits("u", resource=RESOURCE) == [SESSION]
        assert _decode_limits(legacy)["session"]["reset_after_seconds"] == 18_000

        await repo.set_limits("u", [SESSION], resource=RESOURCE)
        item = await _raw(repo, key["PK"], key["SK"])
        assert not any(a.startswith("l_") for a in item)
        assert item["w_session_rsa"] == {"N": "18000"}


class TestAnItemTheReaderCannotTrustIsUnavailable:
    """Two shapes the prefix makes possible, both corrupt, both whole-item."""

    @staticmethod
    async def _put(repo, extra):
        client = await repo._get_client()
        item = {
            "PK": {"S": pk_entity(repo._namespace_id, "u")},
            "SK": {"S": sk_config(RESOURCE)},
            "entity_id": {"S": "u"},
            "resource": {"S": RESOURCE},
            "config_version": {"N": "1"},
            "l_rpm_cp": {"N": "100"},
            "l_rpm_ra": {"N": "100"},
            "l_rpm_rp": {"N": "60"},
            **extra,
        }
        await client.put_item(TableName=repo.table_name, Item=item)
        await repo.invalidate_config_cache()
        return item

    BOTH = {
        "l_session_cp": {"N": "10000"},
        "l_session_ra": {"N": "100"},
        "l_session_rp": {"N": "60"},
        "w_session_cp": {"N": "10000"},
        "w_session_ra": {"N": "0"},
        "w_session_rp": {"N": "1"},
        "w_session_rsa": {"N": "18000"},
    }
    NO_RSA = {
        "w_session_cp": {"N": "10000"},
        "w_session_ra": {"N": "0"},
        "w_session_rp": {"N": "1"},
    }

    async def test_one_name_under_both_prefixes(self, limiter):
        repo = limiter._repository
        item = await self._put(repo, self.BOTH)
        with pytest.raises(RateLimiterUnavailable, match="both 'l_' and 'w_'"):
            await repo.resolve_limits("u", RESOURCE)
        with pytest.raises(RateLimiterUnavailable, match="both"):
            await repo.get_limits("u", resource=RESOURCE)
        with pytest.raises(ValueError, match="both"):
            _decode_limits(item)

    async def test_a_w_limit_without_a_window(self, limiter):
        repo = limiter._repository
        item = await self._put(repo, self.NO_RSA)
        with pytest.raises(RateLimiterUnavailable, match="w_session_rsa"):
            await repo.resolve_limits("u", RESOURCE)
        with pytest.raises(ValueError, match="w_session_rsa"):
            _decode_limits(item)

    def test_a_stray_field_under_the_other_prefix_does_not_merge(self):
        """``l_session_sched`` beside a ``w_session`` limit belongs to nothing:
        with no ``l_session_cp`` it is not a limit, and it must not attach its
        schedule to the one stored under ``w_``."""
        item = {**self.BOTH}
        for field in ("cp", "ra", "rp"):
            del item[f"l_session_{field}"]
        item["l_session_sched"] = {"S": "garbage"}
        decoded = _decode_limits(item)
        assert "schedule" not in decoded["session"]


class TestConfigLimitNames:
    def test_order_and_prefix(self):
        item = {
            "w_session_cp": {"N": "1"},
            "w_session_rsa": {"N": "1"},
            "l_rpm_cp": {"N": "1"},
            "l_x_ra": {"N": "1"},
            "w__cp": {"N": "1"},
            "sched_tz": {"S": "UTC"},
        }
        assert config_limit_names(item) == {"session": True, "rpm": False}

    def test_limit_attr_spells_both_prefixes(self):
        assert limit_attr("rpm", "cp") == "l_rpm_cp"
        assert limit_attr("session", LIMIT_FIELD_RSA, windowed=True) == "w_session_rsa"
        assert parse_limit_attr("w_session_rsa") == ("session", "rsa")
        assert parse_limit_attr("w_") is None


class TestAShardAnOldClientCreatedIsSeededByTransfer:
    """#633's seed is the hard dependency (#640): a pre-v0.15 client doubles the
    entity and creates shard 1 carrying only the limits it can see. Shard 0 still
    holds its old-count share of the live window — the old client's #587
    reclaim cannot see the session limit — so a fresh share on shard 1 would
    mint. The seed takes a transfer instead: nothing spendable is created."""

    async def test_the_seed_conserves_the_entitys_spendable_total(self, limiter):
        repo = limiter._repository
        await repo.set_limits("u", [RPM, SESSION_10], resource=RESOURCE)
        repo._now_ms = lambda: T0
        async with limiter.acquire("u", RESOURCE, consume={"rpm": 1, "session": 2}):
            pass
        assert await repo.bump_shard_count("u", RESOURCE, 1) == 2

        # What the old client's create looks like: rpm (it can see it) and wcu.
        now = T0 + 60_000
        rpm_state = BucketState.from_limit("u", RESOURCE, RPM, now, shard_count=2)
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "u", RESOURCE, [rpm_state], now, shard_id=1, shard_count=2
                )
            ]
        )
        before = await spendable(repo, "u", "session", 2, resource=RESOURCE)
        assert before == 8

        repo._now_ms = lambda: now + 1_000
        with pinned_shard(1):
            async with limiter.acquire("u", RESOURCE, consume={"rpm": 1}):
                pass
        after = await spendable(repo, "u", "session", 2, resource=RESOURCE)
        assert after <= before
        # Shard 0 was clamped to its new share of 5 and shard 1 got the 3 taken.
        assert after == 8

"""Tests for the provisioner's sync boto3 bucket param sync (issue #481).

Mirrors the mocking conventions in test_provisioner_fanout.py: a MagicMock
boto3 DynamoDB client with `client.exceptions.*` populated with real exception
classes so `except client.exceptions.X` matches as it would against real boto3.
"""

from unittest.mock import MagicMock

import pytest

from zae_limiter.schema import (
    BUCKET_SCHED_NONE,
    bucket_attr,
    gsi3_pk_entity,
    limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
    sk_state,
)
from zae_limiter_provisioner.bucket_sync import (
    DEFAULT_TTL_MULTIPLIER,
    build_bucket_param_update,
    resolve_bucket_limits,
    resolve_effective_limits,
    sync_bucket_params,
)

ConditionalCheckFailedException = type("ConditionalCheckFailedException", (Exception,), {})


def _make_client() -> MagicMock:
    client = MagicMock()
    client.exceptions.ConditionalCheckFailedException = ConditionalCheckFailedException
    return client


LIMITS = {"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60}}


class TestBuildBucketParamUpdate:
    def test_converts_whole_tokens_to_millitokens(self):
        """Config items store whole tokens; bucket items store millitokens."""
        expr, names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=0, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        cp_alias = next(k for k, v in names.items() if v == bucket_attr("rpm", "cp"))
        ra_alias = next(k for k, v in names.items() if v == bucket_attr("rpm", "ra"))
        rp_alias = next(k for k, v in names.items() if v == bucket_attr("rpm", "rp"))
        assert values[cp_alias.replace("#", ":")] == {"N": "1000000"}
        assert values[ra_alias.replace("#", ":")] == {"N": "1000000"}
        # refill_period is SECONDS on config, MILLISECONDS on the bucket
        assert values[rp_alias.replace("#", ":")] == {"N": "60000"}
        assert expr.startswith("SET ")

    def test_ttl_multiplier_zero_removes_ttl(self):
        """Entity custom limits mean the bucket must persist: REMOVE ttl."""
        expr, names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=0, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert names["#ttl"] == "ttl"
        assert "#ttl" in expr.split("REMOVE")[1].split(",")[-1]

    def test_ttl_multiplier_positive_sets_ttl(self):
        """Back on defaults: TTL = now + max_time_to_fill * multiplier."""
        expr, _names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=7, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert ":ttl_val" in values
        # time_to_fill = (1000/1000)*60 = 60s; TTL = 1789000000 + 420
        assert values[":ttl_val"] == {"N": str(1_789_000_000 + 420)}
        assert "#ttl" not in expr.split("REMOVE")[1]

    def test_ttl_multiplier_none_leaves_ttl_alone(self):
        expr, names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert "#ttl" not in names
        assert ":ttl_val" not in values

    def test_stale_limits_removed_but_never_rf(self):
        """Stale limit attrs go; the shared `rf` optimistic lock must not."""
        expr, names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names={"tpm"}, now_ms=1_789_000_000_000
        )
        removed = {names[a.strip()] for a in expr.split("REMOVE")[1].split(",")}
        assert removed >= {
            bucket_attr("tpm", f) for f in ("tk", "cp", "ra", "rp", "tc", "sched", "rsched")
        }
        assert bucket_attr("tpm", "rf") not in removed
        assert "rf" not in removed

    def test_vu_is_expired_on_every_update(self):
        """Mirrors the async fan-out (#222 Task 13): `vu = 0` unconditionally,
        forcing one materialising pass that clamps a surplus over a lowered
        ceiling. A manifest apply that shrinks a capacity has exactly #469's
        exposure, and `differ.py` re-asserts every manifest resource on every
        apply, so the mirror needs this as much as the async path."""
        expr, names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert "#vu = :vu_zero" in expr
        assert names["#vu"] == "vu"
        assert values[":vu_zero"] == {"N": "0"}

    def test_vu_is_never_set_and_removed_together(self):
        """#488: SET and REMOVE on one attribute is a ValidationException, and
        `ttl_multiplier=0` is the branch that builds a REMOVE list."""
        expr, _names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=0, stale_limit_names={"tpm"}, now_ms=1_789_000_000_000
        )
        set_clause, remove_clause = expr.split(" REMOVE ")
        assert "#vu" in set_clause
        assert "#vu" not in remove_clause

    def test_an_unscheduled_manifest_clears_the_stamps(self):
        """Schedules became manifest-expressible in #543, so this write is now
        authoritative over them: override, not merge. A limit re-applied
        without a schedule must lose the one it had, exactly as the async
        fan-out does — leaving it behind would keep the aggregator refilling
        toward a ceiling the operator has already changed."""
        expr, names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        removed = {names[a.strip()] for a in expr.split("REMOVE")[1].split(",")}
        assert {"sched", "sched_tz", "rsched"} <= removed

    def test_hyphenated_limit_names_use_indexed_aliases(self):
        """Limit names may contain hyphens, which are illegal in expression names."""
        expr, names, _values = build_bucket_param_update(
            {"req-per-min": {"capacity": 5, "refill_amount": 5, "refill_period": 1}},
            ttl_multiplier=None,
            stale_limit_names=None,
            now_ms=0,
        )
        assert bucket_attr("req-per-min", "cp") in names.values()
        assert "-" not in expr


class TestDurationWindowParamSync:
    """`l_{name}_rsa` / `b_{name}_rsa` — the provisioner mirror (ADR-139, plan
    Task 9). Mirrors `TestBuildBucketParamUpdate`'s scheduling tests, scoped to
    the duration-window field.
    """

    WINDOW = {
        "session": {
            "capacity": 10_000,
            "refill_amount": 0,
            "refill_period": 1,
            "reset_after_seconds": 18_000,
        }
    }

    def test_a_window_limit_gets_rsa_set(self):
        expr, names, values = build_bucket_param_update(
            self.WINDOW, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        alias = next(a for a, attr in names.items() if attr == bucket_attr("session", "rsa"))
        set_clause = expr.split("REMOVE")[0]
        assert f"{alias} = :" in set_clause
        assert values[f":{alias[1:]}"] == {"N": "18000"}

    def test_a_limit_without_a_window_gets_rsa_removed(self):
        """A quota converted to a drip must lose `rsa`, or the item keeps
        reconstructing as a quota forever. Absence means "no window", so this
        is a plain REMOVE — unlike `sched`, where absence means "inherit the
        item default" and #541 needs the explicit `BUCKET_SCHED_NONE`
        marker."""
        expr, names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        alias = next(a for a, attr in names.items() if attr == bucket_attr("rpm", "rsa"))
        remove_clause = expr.split("REMOVE")[1]
        assert alias in [a.strip() for a in remove_clause.split(",")]

    def test_ws_is_never_written(self):
        for limits in (self.WINDOW, LIMITS, QUOTA):
            expr, names, _values = build_bucket_param_update(
                limits, ttl_multiplier=None, stale_limit_names=None, now_ms=0
            )
            ws_attrs = {attr for attr in names.values() if attr.endswith("_ws")}
            assert not ws_attrs, limits
            assert "_ws" not in expr, limits

    def test_rsa_is_never_set_and_removed_together(self):
        """#488's rule extended to `rsa`."""
        mixed = {**self.WINDOW, **LIMITS}
        expr, names, _values = build_bucket_param_update(
            mixed, ttl_multiplier=0, stale_limit_names={"gone"}, now_ms=0
        )
        set_clause, remove_clause = expr.split(" REMOVE ")
        set_aliases = {
            part.split("=")[0].strip() for part in set_clause.removeprefix("SET ").split(",")
        }
        remove_aliases = {part.strip() for part in remove_clause.split(",")}
        assert not (set_aliases & remove_aliases)

    def test_the_decoded_window_reaches_the_update(self):
        """`_decode_limits` must read `l_{name}_rsa` off a raw config item, or
        an entity-wide fan-out re-resolving a session quota silently drops its
        window (the #487 class of bug, for this field)."""
        client = _make_client()
        item = _limits_item(session=(10_000, 0, 1))
        item[limit_attr("session", "rsa")] = {"N": "18000"}
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        limits, _level = resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        assert limits["session"]["reset_after_seconds"] == 18_000

        _expr, names, values = build_bucket_param_update(
            limits, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        alias = next(a for a, attr in names.items() if attr == bucket_attr("session", "rsa"))
        assert values[f":{alias[1:]}"] == {"N": "18000"}

    def test_an_ordinary_limit_decodes_with_no_window(self):
        """The trio alone must not manufacture a `reset_after` — the
        optional-key split must stay off `_REQUIRED_MANIFEST_KEYS`."""
        client = _make_client()
        client.get_item.side_effect = _levels(
            {(pk_resource("ns123", "gpt-4"), sk_config()): _limits_item(rpm=(10, 10, 60))}
        )
        limits = resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        assert "reset_after_seconds" not in limits["rpm"]

    def test_entity_wide_fanout_stamps_the_resolved_window(self):
        """The #487 blast radius, for `rsa`: under `_default_` every bucket is
        re-resolved from config, so a dropped window is not merely unread —
        it is actively stripped off the bucket enforcing it."""
        client = _make_client()
        client.query.side_effect = _query_pages({"Items": [{"PK": {"S": _pk(resource="gpt-4")}}]})
        entity_default = _limits_item(session=(10_000, 0, 1))
        entity_default[limit_attr("session", "rsa")] = {"N": "18000"}
        client.get_item.side_effect = _levels(
            {(pk_entity("ns123", "user-1"), sk_config("_default_")): entity_default}
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            {"session": {"capacity": 10_000, "refill_amount": 0, "refill_period": 1}},
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 1
        names = client.update_item.call_args.kwargs["ExpressionAttributeNames"]
        values = client.update_item.call_args.kwargs["ExpressionAttributeValues"]
        alias = next(a for a, attr in names.items() if attr == bucket_attr("session", "rsa"))
        assert values[f":{alias[1:]}"] == {"N": "18000"}

    def test_ttl_reconstruction_does_not_crash_on_a_duration_quota(self):
        """The TTL branch rebuilds a `Limit` from the manifest-shaped decl;
        before `reset_after` was threaded through it, a session quota raised
        `ValueError: min() iterable argument is empty` out of
        `schema._recovery_seconds` (no `reset_schedule` to scan)."""
        expr, _names, values = build_bucket_param_update(
            self.WINDOW, ttl_multiplier=7, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        # 18000s window x 7 multiplier.
        assert values[":ttl_val"] == {"N": str(1_789_000_000 + 18_000 * 7)}
        assert "#ttl = :ttl_val" in expr


def _query_pages(*pages):
    """client.query side_effect returning the given pages, then repeating the last.

    _fanout-style discovery runs the query twice, so the side_effect must keep
    answering after the first pass is exhausted.
    """
    responses = list(pages)

    def _query(**kwargs):
        return responses.pop(0) if len(responses) > 1 else responses[0]

    return _query


def _pk(entity_id="user-1", resource="gpt-4", shard=0, ns="ns123"):
    return f"{ns}/BUCKET#{entity_id}#{resource}#{shard}"


class TestSyncBucketParams:
    def test_writes_every_discovered_shard(self):
        client = _make_client()
        client.query.side_effect = _query_pages(
            {"Items": [{"PK": {"S": _pk(shard=0)}}, {"PK": {"S": _pk(shard=1)}}]}
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 2
        keys = {c.kwargs["Key"]["PK"]["S"] for c in client.update_item.call_args_list}
        assert keys == {_pk(shard=0), _pk(shard=1)}
        for call in client.update_item.call_args_list:
            assert call.kwargs["Key"]["SK"] == {"S": sk_state()}
            assert call.kwargs["ConditionExpression"] == "attribute_exists(PK)"

    def test_queries_gsi3_scoped_to_the_resource(self):
        client = _make_client()
        client.query.side_effect = _query_pages({"Items": []})
        sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        params = client.query.call_args.kwargs
        assert params["IndexName"] == "GSI3"
        assert params["ExpressionAttributeValues"][":pk"] == {
            "S": gsi3_pk_entity("ns123", "user-1")
        }
        assert params["ExpressionAttributeValues"][":sk"] == {"S": "BUCKET#gpt-4#"}

    def test_runs_two_passes_without_double_writing(self):
        """Second pass catches an in-flight bucket; pass-one PKs are not rewritten."""
        client = _make_client()
        client.query.side_effect = _query_pages(
            {"Items": [{"PK": {"S": _pk(shard=0)}}]},
            {"Items": [{"PK": {"S": _pk(shard=0)}}, {"PK": {"S": _pk(shard=1)}}]},
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 2
        assert client.update_item.call_count == 2

    def test_vanished_shard_is_tolerated(self):
        """TTL can expire a shard between discovery and write."""
        client = _make_client()
        client.query.side_effect = _query_pages({"Items": [{"PK": {"S": _pk()}}]})
        client.update_item.side_effect = ConditionalCheckFailedException()
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 0

    def test_no_limits_is_a_noop(self):
        client = _make_client()
        assert (
            sync_bucket_params(
                client,
                "tbl",
                "ns123",
                "user-1",
                "gpt-4",
                {},
                ttl_multiplier=0,
                stale_limit_names=None,
                now_ms=0,
            )
            == 0
        )
        client.query.assert_not_called()

    def test_paginates_discovery(self):
        client = _make_client()
        client.query.side_effect = _query_pages(
            {"Items": [{"PK": {"S": _pk(shard=0)}}], "LastEvaluatedKey": {"PK": {"S": "x"}}},
            {"Items": [{"PK": {"S": _pk(shard=1)}}]},
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 2


def _limits_item(**limits):
    """A config item carrying composite limit attributes (whole tokens)."""
    item = {}
    for name, (cp, ra, rp) in limits.items():
        item[limit_attr(name, "cp")] = {"N": str(cp)}
        item[limit_attr(name, "ra")] = {"N": str(ra)}
        item[limit_attr(name, "rp")] = {"N": str(rp)}
    return item


def _levels(mapping):
    def _get_item(**kwargs):
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        return {"Item": mapping[key]} if key in mapping else {}

    return _get_item


class TestResolveEffectiveLimits:
    def test_entity_default_wins_over_resource(self):
        client = _make_client()
        client.get_item.side_effect = _levels(
            {
                (pk_entity("ns123", "user-1"), sk_config("_default_")): _limits_item(
                    rpm=(50, 50, 60)
                ),
                (pk_resource("ns123", "gpt-4"), sk_config()): _limits_item(rpm=(999, 999, 60)),
            }
        )
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {
            "rpm": {"capacity": 50, "refill_amount": 50, "refill_period": 60}
        }

    def test_falls_through_to_resource_then_system(self):
        client = _make_client()
        client.get_item.side_effect = _levels(
            {
                (pk_system("ns123"), sk_config()): _limits_item(rpm=(10, 10, 60)),
            }
        )
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {
            "rpm": {"capacity": 10, "refill_amount": 10, "refill_period": 60}
        }

    def test_no_level_defines_limits(self):
        client = _make_client()
        client.get_item.side_effect = _levels({})
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {}

    def test_skips_entity_default_level_when_resource_is_default(self):
        """Mirrors resolve_disabled: no point reading _default_ twice."""
        client = _make_client()
        client.get_item.side_effect = _levels({})
        resolve_effective_limits(client, "tbl", "ns123", "user-1", "_default_")
        read = [c.kwargs["Key"]["SK"]["S"] for c in client.get_item.call_args_list]
        assert read.count(sk_config("_default_")) == 0

    def test_ignores_non_limit_attributes(self):
        """`disabled`, `config_version` and friends must not become limits."""
        client = _make_client()
        item = _limits_item(rpm=(10, 10, 60))
        item["disabled"] = {"BOOL": True}
        item["config_version"] = {"N": "3"}
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        assert set(resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4")) == {"rpm"}

    def test_unknown_limit_field_is_ignored(self):
        """An `l_{name}_{field}` attribute for a field we do not map is skipped.

        Forward compatibility: a newer writer can add a per-limit field this
        code does not know about (the #222 scheduling fields will), and it must
        not be mistaken for cp/ra/rp or make an otherwise-valid limit malformed.
        """
        client = _make_client()
        item = _limits_item(rpm=(10, 10, 60))
        item[limit_attr("rpm", "zz")] = {"N": "7"}
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {
            "rpm": {"capacity": 10, "refill_amount": 10, "refill_period": 60}
        }

    def test_partial_limit_attributes_are_skipped(self):
        """A limit missing cp/ra/rp is malformed; do not synthesise defaults."""
        client = _make_client()
        item = {limit_attr("rpm", "cp"): {"N": "10"}}
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {}


class TestResolveBucketLimits:
    """The full ADR-100 walk, including the entity(resource) level (#487)."""

    def test_entity_resource_level_outranks_entity_default(self):
        client = _make_client()
        client.get_item.side_effect = _levels(
            {
                (pk_entity("ns123", "user-1"), sk_config("gpt-4")): _limits_item(
                    rpm=(900, 900, 60)
                ),
                (pk_entity("ns123", "user-1"), sk_config("_default_")): _limits_item(
                    rpm=(100, 100, 60)
                ),
            }
        )
        limits, level = resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        assert limits == {"rpm": {"capacity": 900, "refill_amount": 900, "refill_period": 60}}
        assert level == "entity"

    def test_reports_the_level_that_answered(self):
        """The level decides the bucket's TTL, so it has to come back."""
        client = _make_client()
        client.get_item.side_effect = _levels(
            {(pk_resource("ns123", "gpt-4"), sk_config()): _limits_item(rpm=(200, 200, 60))}
        )
        limits, level = resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        assert limits == {"rpm": {"capacity": 200, "refill_amount": 200, "refill_period": 60}}
        assert level == "resource"

    def test_nothing_configured_anywhere(self):
        client = _make_client()
        client.get_item.side_effect = _levels({})
        assert resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4") == ({}, None)


class TestEntityWideScopeWidensDiscovery:
    """An entity-wide `_default_` manifest entry must reach the buckets (#487).

    `_default_` is a config scope, not a resource: the prefix
    `BUCKET#_default_#` matches no bucket item, so forwarding the sentinel made
    the sync a silent no-op. Widening alone would be worse — precedence is
    Entity(resource) > Entity(`_default_`) > Resource > System, so each
    discovered bucket is re-resolved for its OWN resource, exactly as
    `fanout.fanout_entity` re-resolves `disabled`.
    """

    ENTITY_DEFAULT = {"rpm": {"capacity": 500, "refill_amount": 500, "refill_period": 60}}

    @staticmethod
    def _client_with(buckets, config):
        client = _make_client()
        client.query.side_effect = _query_pages({"Items": [{"PK": {"S": pk}} for pk in buckets]})
        client.get_item.side_effect = _levels(config)
        return client

    @staticmethod
    def _written(client):
        """{resource: {bucket_attr: value}} for every bucket written."""
        out = {}
        for call in client.update_item.call_args_list:
            resource = call.kwargs["Key"]["PK"]["S"].split("#")[2]
            names = call.kwargs["ExpressionAttributeNames"]
            values = call.kwargs["ExpressionAttributeValues"]
            out[resource] = {
                names[alias]: values[alias.replace("#", ":", 1)]["N"]
                for alias in names
                if alias.replace("#", ":", 1) in values
            }
        return out

    def test_default_scope_queries_unscoped(self):
        client = self._client_with(
            [],
            {
                (pk_entity("ns123", "user-1"), sk_config("_default_")): _limits_item(
                    rpm=(500, 500, 60)
                )
            },
        )
        sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            self.ENTITY_DEFAULT,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert client.query.call_args.kwargs["ExpressionAttributeValues"][":sk"] == {"S": "BUCKET#"}

    def test_a_resource_specific_entity_config_is_not_clobbered(self):
        """The guard against a naive widen: gpt-4 keeps its own 900."""
        client = self._client_with(
            [_pk(resource="gpt-4"), _pk(resource="claude-3")],
            {
                (pk_entity("ns123", "user-1"), sk_config("gpt-4")): _limits_item(
                    rpm=(900, 900, 60)
                ),
                (pk_entity("ns123", "user-1"), sk_config("_default_")): _limits_item(
                    rpm=(500, 500, 60)
                ),
            },
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            self.ENTITY_DEFAULT,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 2
        by_resource = self._written(client)
        cp = bucket_attr("rpm", "cp")
        assert by_resource["gpt-4"][cp] == "900000", "gpt-4's own entity config outranks _default_"
        assert by_resource["claude-3"][cp] == "500000"

    def test_ttl_follows_the_level_that_resolved_each_bucket(self):
        """Entity level persists; resource/system expires (#271, #296)."""
        client = self._client_with(
            [_pk(resource="gpt-4"), _pk(resource="claude-3")],
            {
                (pk_entity("ns123", "user-1"), sk_config("gpt-4")): _limits_item(
                    rpm=(900, 900, 60)
                ),
                (pk_resource("ns123", "claude-3"), sk_config()): _limits_item(rpm=(200, 200, 60)),
            },
        )
        sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            self.ENTITY_DEFAULT,
            ttl_multiplier=DEFAULT_TTL_MULTIPLIER,
            stale_limit_names=None,
            now_ms=1_789_000_000_000,
        )
        exprs = {
            call.kwargs["Key"]["PK"]["S"].split("#")[2]: call.kwargs["UpdateExpression"]
            for call in client.update_item.call_args_list
        }
        assert exprs["gpt-4"].split("REMOVE")[1].split(",")[-1].strip() == "#ttl", (
            "entity limits: the bucket must persist"
        )
        assert "#ttl = :ttl_val" in exprs["claude-3"], "resource defaults: the bucket must expire"

    def test_stale_names_are_intersected_with_each_resolution(self):
        """A name the bucket's own level still declares must not be removed.

        SET and REMOVE on one attribute in a single expression is a DynamoDB
        ValidationException, and if it landed it would strip a configured limit.
        """
        client = self._client_with(
            [_pk(resource="gpt-4"), _pk(resource="claude-3")],
            {
                (pk_entity("ns123", "user-1"), sk_config("gpt-4")): _limits_item(
                    rpm=(900, 900, 60), tpm=(90, 90, 60)
                ),
                (pk_system("ns123"), sk_config()): _limits_item(rpm=(50, 50, 60)),
            },
        )
        sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            {"rpm": {"capacity": 50, "refill_amount": 50, "refill_period": 60}},
            ttl_multiplier=DEFAULT_TTL_MULTIPLIER,
            stale_limit_names={"tpm"},
            now_ms=0,
        )
        exprs = {
            call.kwargs["Key"]["PK"]["S"].split("#")[2]: call.kwargs["UpdateExpression"]
            for call in client.update_item.call_args_list
        }
        names = {
            call.kwargs["Key"]["PK"]["S"].split("#")[2]: call.kwargs["ExpressionAttributeNames"]
            for call in client.update_item.call_args_list
        }
        tpm_cp = bucket_attr("tpm", "cp")
        assert tpm_cp in names["gpt-4"].values(), "gpt-4's entity config still declares tpm"
        assert tpm_cp not in _removed(exprs["gpt-4"], names["gpt-4"]), "nothing is stale for gpt-4"
        assert tpm_cp in _removed(exprs["claude-3"], names["claude-3"]), (
            "tpm is stale on claude-3 (system has no tpm)"
        )

    def test_a_resource_that_resolves_to_nothing_is_left_alone(self):
        """No configured level means no correct value to write."""
        client = self._client_with([_pk(resource="ghost")], {})
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            self.ENTITY_DEFAULT,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 0
        client.update_item.assert_not_called()

    def test_resolution_is_memoized_per_resource(self):
        """A 500-bucket entity must not re-resolve once per shard."""
        client = self._client_with(
            [_pk(resource="gpt-4", shard=n) for n in range(4)],
            {(pk_system("ns123"), sk_config()): _limits_item(rpm=(50, 50, 60))},
        )
        sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            self.ENTITY_DEFAULT,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert client.update_item.call_count == 4
        # One walk for gpt-4: entity(gpt-4), entity(_default_), resource, system.
        assert client.get_item.call_count == 4

    def test_a_real_resource_is_still_scoped_and_uses_the_caller_directive(self):
        """The scoped path is untouched: no resolution, caller's limits verbatim."""
        client = self._client_with([_pk(resource="gpt-4")], {})
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 1
        client.get_item.assert_not_called()
        assert client.query.call_args.kwargs["ExpressionAttributeValues"][":sk"] == {
            "S": "BUCKET#gpt-4#"
        }


# --- #222: schedules become manifest-expressible -----------------------------

BIZ = [{"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}]
BIZ_COMPACT = "1h9-17w1-5s500"
WEEKEND = [{"cron": "* * * * SAT,SUN", "tz": "America/New_York", "scale": 0.25}]
WEEKEND_COMPACT = "1w6,7s250"
MIDNIGHT = [{"cron": "0 0 * * *", "tz": "America/New_York"}]
MIDNIGHT_COMPACT = "1m0h0"

SCHEDULED = {"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60, "schedule": BIZ}}
QUOTA = {
    "rpd": {
        "capacity": 10000,
        # ADR-137: a reset flips the manifest shorthand default to 0, and
        # `to_dict()` always emits the field, so 0 is what a round trip yields.
        "refill_amount": 0,
        "refill_period": 86400,
        "reset_schedule": MIDNIGHT,
    }
}


def _clauses(expr):
    """`(set_attrs, removed_attrs)` for an expression, resolved through aliases."""
    set_clause, _, remove_clause = expr.partition(" REMOVE ")
    return set_clause.removeprefix("SET "), remove_clause


def _removed(expr, names):
    _set_clause, remove_clause = _clauses(expr)
    return {names[a.strip()] for a in remove_clause.split(",") if a.strip()}


def _set_attrs(expr, names):
    set_clause, _remove = _clauses(expr)
    return {names[part.split(" = ")[0].strip()] for part in set_clause.split(",")}


class TestProvisionerStampsSchedules:
    """Task 8: a manifest-applied schedule must reach the buckets (#222)."""

    def test_stamps_the_item_level_default_and_the_hoisted_timezone(self):
        expr, names, values = build_bucket_param_update(
            SCHEDULED, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        assert values[":sched"] == {"S": BIZ_COMPACT}
        assert values[":sched_tz"] == {"S": "America/New_York"}
        assert names["#sched"] == "sched"
        assert names["#sched_tz"] == "sched_tz"
        assert "#sched = :sched" in expr
        assert "#sched_tz = :sched_tz" in expr
        # Nothing to reset, so `rsched` is cleared rather than left stale.
        assert "rsched" in _removed(expr, names)

    def test_vu_is_zero_not_a_computed_boundary(self):
        """A future `vu` leaves the fast path spending a surplus over a lowered
        ceiling until natural refill catches up (§3.4)."""
        _expr, _names, values = build_bucket_param_update(
            SCHEDULED, ttl_multiplier=None, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert values[":vu_zero"] == {"N": "0"}

    def test_base_params_stay_undivided_and_unscaled(self):
        """The schedule applies on top; cp/ra stay the base (§2.1)."""
        _expr, names, values = build_bucket_param_update(
            SCHEDULED, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        cp_alias = next(a for a, attr in names.items() if attr == bucket_attr("rpm", "cp"))
        assert values[cp_alias.replace("#", ":")] == {"N": "1000000"}

    def test_a_second_limit_with_a_different_schedule_gets_its_own_override(self):
        """The N-limit case: one item-level default plus per-limit overrides.

        Hoisting only the first scheduled limit's encoding and stopping there
        would silently apply `rpm`'s business-hours halving to `tpm`.
        """
        expr, names, values = build_bucket_param_update(
            {
                "rpm": {
                    "capacity": 1000,
                    "refill_amount": 1000,
                    "refill_period": 60,
                    "schedule": BIZ,
                },
                "tpm": {
                    "capacity": 50,
                    "refill_amount": 50,
                    "refill_period": 60,
                    "schedule": WEEKEND,
                },
            },
            ttl_multiplier=None,
            stale_limit_names=None,
            now_ms=0,
        )
        assert values[":sched"] == {"S": BIZ_COMPACT}
        override = next(
            a for a, attr in names.items() if attr == bucket_attr("tpm", "sched") and a != "#sched"
        )
        assert values[f":{override[1:]}"] == {"S": WEEKEND_COMPACT}
        assert f"{override} = :{override[1:]}" in expr
        # The limit that *is* the default carries no override of its own.
        assert bucket_attr("rpm", "sched") in _removed(expr, names)

    def test_a_limit_sharing_the_default_encoding_has_its_override_cleared(self):
        """Absence means "inherit the item default", so a stale override left
        behind keeps enforcing a superseded schedule forever."""
        expr, names, values = build_bucket_param_update(
            {
                "rpm": {
                    "capacity": 1000,
                    "refill_amount": 1000,
                    "refill_period": 60,
                    "schedule": BIZ,
                },
                "tpm": {
                    "capacity": 50,
                    "refill_amount": 50,
                    "refill_period": 60,
                    "schedule": BIZ,
                },
            },
            ttl_multiplier=None,
            stale_limit_names=None,
            now_ms=0,
        )
        assert values[":sched"] == {"S": BIZ_COMPACT}
        removed = _removed(expr, names)
        assert bucket_attr("rpm", "sched") in removed
        assert bucket_attr("tpm", "sched") in removed
        assert WEEKEND_COMPACT not in str(values)

    def test_reset_schedule_stamps_rsched(self):
        expr, names, values = build_bucket_param_update(
            QUOTA, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        assert values[":rsched"] == {"S": MIDNIGHT_COMPACT}
        assert values[":sched_tz"] == {"S": "America/New_York"}
        assert names["#rsched"] == "rsched"

    def test_a_quota_clears_sched_while_keeping_sched_tz(self):
        """`sched_tz` is shared by both tuples, so it is decided from whether
        ANYTHING on the item is scheduled. Deciding it inside the parameter
        branch would REMOVE it for a quota carrying only a reset, and the
        stored `rsched` would then decode as UTC forever."""
        expr, names, values = build_bucket_param_update(
            QUOTA, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        removed = _removed(expr, names)
        assert "sched" in removed
        assert "sched_tz" not in removed
        assert values[":sched_tz"] == {"S": "America/New_York"}
        assert ":sched" not in values

    def test_unscheduled_limits_clear_every_stamp_except_vu(self):
        """Override, not merge: dropping a schedule must clear the item.

        `vu` is deliberately NOT in the removed set — it is SET to 0 on every
        fan-out, and SET + REMOVE on one attribute is a ValidationException
        (#488).
        """
        expr, names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        removed = _removed(expr, names)
        assert {"sched", "sched_tz", "rsched"} <= removed
        assert bucket_attr("rpm", "sched") in removed
        assert bucket_attr("rpm", "rsched") in removed
        assert "vu" not in removed
        assert values[":vu_zero"] == {"N": "0"}
        assert ":sched" not in values
        assert ":sched_tz" not in values

    def test_no_attribute_is_both_set_and_removed(self):
        """#488, over every branch this task adds."""
        for limits in (LIMITS, SCHEDULED, QUOTA):
            expr, names, _values = build_bucket_param_update(
                limits, ttl_multiplier=0, stale_limit_names={"tpm"}, now_ms=0
            )
            assert not (_set_attrs(expr, names) & _removed(expr, names)), limits

    def test_limits_disagreeing_on_a_timezone_are_rejected(self):
        """One item, one `sched_tz`: silently keeping the first limit's zone
        would reinterpret the second limit's cron in the wrong one."""
        with pytest.raises(ValueError, match="timezone"):
            build_bucket_param_update(
                {
                    "rpm": {
                        "capacity": 1,
                        "refill_amount": 1,
                        "refill_period": 60,
                        "schedule": BIZ,
                    },
                    "tpm": {
                        "capacity": 1,
                        "refill_amount": 1,
                        "refill_period": 60,
                        "schedule": [{"cron": "* 9-17 * * *", "tz": "Europe/London", "scale": 0.5}],
                    },
                },
                ttl_multiplier=None,
                stale_limit_names=None,
                now_ms=0,
            )

    def test_a_quota_keeps_its_reset_when_the_bucket_ttl_is_computed(self):
        """The TTL leg rebuilds a `Limit`, and ADR-137 rejects a zero refill
        that carries no reset — so the reset has to be carried across.

        `ttl_multiplier=0` is the ADR-136 entity-level case, which is the one
        reachable for a manifest-declared entity quota. The resource/system
        case divides by the zero rate and is #532.
        """
        expr, names, values = build_bucket_param_update(
            QUOTA, ttl_multiplier=0, stale_limit_names=None, now_ms=0
        )
        assert "#ttl" in _clauses(expr)[1]
        assert values[":rsched"] == {"S": MIDNIGHT_COMPACT}
        assert names["#ttl"] == "ttl"

    def test_schedule_entries_are_accepted_as_objects_too(self):
        """`_decode_limits` yields parsed entries; the handler yields wire dicts."""
        from zae_limiter.schedule import ScheduleEntry

        entry = ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5)
        _expr, _names, values = build_bucket_param_update(
            {
                "rpm": {
                    "capacity": 1000,
                    "refill_amount": 1000,
                    "refill_period": 60,
                    "schedule": (entry,),
                }
            },
            ttl_multiplier=None,
            stale_limit_names=None,
            now_ms=0,
        )
        assert values[":sched"] == {"S": BIZ_COMPACT}


class TestDecodeAdmitsScheduledLimits:
    """The exact-set filter in `_decode_limits` drops any widened shape.

    A scheduled limit that fails to decode does not raise — the comprehension
    simply stops yielding it — and `_resolved_plan` then unstamps the very
    limit the apply was asked to schedule.
    """

    @staticmethod
    def _scheduled_config(compact, *, reset=False, tz="America/New_York"):
        item = _limits_item(rpm=(1000, 1000, 60))
        item[limit_attr("rpm", "rsched" if reset else "sched")] = {"S": compact}
        item["sched_tz"] = {"S": tz}
        return item

    def test_a_scheduled_limit_survives_the_precedence_walk(self):
        client = _make_client()
        client.get_item.side_effect = _levels(
            {(pk_resource("ns123", "gpt-4"), sk_config()): self._scheduled_config(BIZ_COMPACT)}
        )
        limits, level = resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        assert level == "resource"
        assert set(limits) == {"rpm"}
        assert limits["rpm"]["capacity"] == 1000

    def test_the_decoded_schedule_round_trips_to_the_same_compact_form(self):
        client = _make_client()
        client.get_item.side_effect = _levels(
            {(pk_resource("ns123", "gpt-4"), sk_config()): self._scheduled_config(BIZ_COMPACT)}
        )
        limits, _level = resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        _expr, _names, values = build_bucket_param_update(
            limits, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        assert values[":sched"] == {"S": BIZ_COMPACT}
        assert values[":sched_tz"] == {"S": "America/New_York"}

    def test_the_hoisted_timezone_reaches_the_decoded_entries(self):
        """A config item stores one `sched_tz`, not one per entry; decoding
        without it silently reinterprets every cron in UTC."""
        client = _make_client()
        client.get_item.side_effect = _levels(
            {
                (pk_resource("ns123", "gpt-4"), sk_config()): self._scheduled_config(
                    BIZ_COMPACT, tz="Asia/Tokyo"
                )
            }
        )
        limits, _level = resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        _expr, _names, values = build_bucket_param_update(
            limits, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        assert values[":sched_tz"] == {"S": "Asia/Tokyo"}

    def test_a_reset_schedule_survives_the_walk(self):
        client = _make_client()
        item = _limits_item(rpd=(10000, 0, 86400))
        item[limit_attr("rpd", "rsched")] = {"S": MIDNIGHT_COMPACT}
        item["sched_tz"] = {"S": "America/New_York"}
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        limits, _level = resolve_bucket_limits(client, "tbl", "ns123", "user-1", "gpt-4")
        _expr, _names, values = build_bucket_param_update(
            limits, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )
        assert values[":rsched"] == {"S": MIDNIGHT_COMPACT}

    def test_a_limit_missing_a_required_field_is_still_dropped(self):
        """Widening the filter must not turn it off: cp/ra/rp stay required."""
        client = _make_client()
        item = {
            limit_attr("rpm", "cp"): {"N": "10"},
            limit_attr("rpm", "sched"): {"S": BIZ_COMPACT},
            "sched_tz": {"S": "America/New_York"},
        }
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {}

    def test_entity_wide_fanout_stamps_the_resolved_schedule(self):
        """The landmine's real blast radius (#487): under `_default_` every
        bucket is re-resolved from config, so a dropped schedule is not merely
        unread — it is actively stripped off the bucket enforcing it."""
        client = _make_client()
        client.query.side_effect = _query_pages({"Items": [{"PK": {"S": _pk(resource="gpt-4")}}]})
        entity_default = _limits_item(rpm=(500, 500, 60))
        entity_default[limit_attr("rpm", "sched")] = {"S": BIZ_COMPACT}
        entity_default["sched_tz"] = {"S": "America/New_York"}
        client.get_item.side_effect = _levels(
            {(pk_entity("ns123", "user-1"), sk_config("_default_")): entity_default}
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "_default_",
            {"rpm": {"capacity": 500, "refill_amount": 500, "refill_period": 60}},
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 1
        values = client.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":sched"] == {"S": BIZ_COMPACT}


class TestTheMirrorMarksUnscheduledLimits:
    """#541, on the Lambda side of the same encoder.

    The provisioner and ``set_limits()`` write the same attributes to the same
    items, so a different inheritance rule here would mean which schedule a
    limit runs under depends on which writer touched the bucket last. Every
    manifest apply re-asserts every resource (`differ.py`), so this runs often.
    """

    MIXED = {
        "rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60, "schedule": BIZ},
        "tpm": {"capacity": 50, "refill_amount": 50, "refill_period": 60},
        "rpd": {
            "capacity": 10000,
            "refill_amount": 0,
            "refill_period": 86400,
            "reset_schedule": MIDNIGHT,
        },
    }

    def _build(self):
        return build_bucket_param_update(
            self.MIXED, ttl_multiplier=None, stale_limit_names=None, now_ms=0
        )

    def _override(self, names, values, limit, field):
        alias = next(
            a
            for a, attr in names.items()
            if attr == bucket_attr(limit, field) and not a.startswith("#stale")
        )
        return values.get(f":{alias[1:]}"), alias

    def test_an_unscheduled_limit_is_marked_rather_than_left_absent(self):
        expr, names, values = self._build()
        value, alias = self._override(names, values, "tpm", "sched")
        assert value == {"S": BUCKET_SCHED_NONE}
        assert f"{alias} = :{alias[1:]}" in expr

    def test_a_quota_is_marked_unscheduled_on_the_parameter_tuple(self):
        """The direction the issue understates: without the marker the daily
        quota inherits the rate limit's 0.5x window and silently serves half
        its allowance between 09:00 and 17:00."""
        _expr, names, values = self._build()
        assert self._override(names, values, "rpd", "sched")[0] == {"S": BUCKET_SCHED_NONE}

    def test_a_rate_limit_is_marked_reset_free(self):
        """...and without this one the rate limit takes the quota's midnight
        reset, a hard SET of its balance on a calendar it never declared."""
        _expr, names, values = self._build()
        assert self._override(names, values, "rpm", "rsched")[0] == {"S": BUCKET_SCHED_NONE}
        assert self._override(names, values, "tpm", "rsched")[0] == {"S": BUCKET_SCHED_NONE}

    def test_the_limits_supplying_each_default_still_carry_no_override(self):
        """Not "an override for every limit": absence still means "inherit",
        which is what keeps a shared schedule down to one attribute."""
        expr, names, _values = self._build()
        removed = _removed(expr, names)
        assert bucket_attr("rpm", "sched") in removed
        assert bucket_attr("rpd", "rsched") in removed

    def test_no_attribute_is_both_set_and_removed(self):
        """#488 again, over the branch the marker adds."""
        expr, names, _values = self._build()
        set_attrs = _set_attrs(expr, names)
        assert not (set_attrs & _removed(expr, names))


def test_the_mirror_encodes_exactly_what_the_async_repository_does():
    """One encoder, mirrored — asserted against the original, not re-described.

    `bucket_sync._encode_one_tuple` is a hand-written copy of
    `Repository._encode_one_tuple`, and the two write the same attributes to
    the same bucket items. A divergence in which limit supplies the item-level
    default, or in whether an unscheduled limit is marked (#541), would make a
    limit's schedule depend on whether an admin last used the Python API or a
    manifest.
    """
    from zae_limiter.models import Limit
    from zae_limiter.repository import Repository
    from zae_limiter_provisioner.bucket_sync import _encode_item_schedules
    from zae_limiter_provisioner.manifest import entries_from_manifest

    limits = [
        Limit.per_minute("rpm", 1000).with_schedule(
            entries_from_manifest(BIZ, reset=False),
        ),
        Limit.per_minute("tpm", 50),
        Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York"),
    ]
    named = [(lim.name, lim.schedule, lim.reset_schedule) for lim in limits]
    assert _encode_item_schedules(named) == Repository._encode_item_schedules(named)

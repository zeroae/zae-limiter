"""Tests for the provisioner's sync boto3 bucket param sync (issue #481).

Mirrors the mocking conventions in test_provisioner_fanout.py: a MagicMock
boto3 DynamoDB client with `client.exceptions.*` populated with real exception
classes so `except client.exceptions.X` matches as it would against real boto3.
"""

from unittest.mock import MagicMock

from zae_limiter.schema import (
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
    build_bucket_param_update,
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
        assert "REMOVE" in expr
        assert names["#ttl"] == "ttl"
        assert expr.split("REMOVE")[1].strip().startswith("#ttl")

    def test_ttl_multiplier_positive_sets_ttl(self):
        """Back on defaults: TTL = now + max_time_to_fill * multiplier."""
        expr, _names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=7, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert ":ttl_val" in values
        # time_to_fill = (1000/1000)*60 = 60s; TTL = 1789000000 + 420
        assert values[":ttl_val"] == {"N": str(1_789_000_000 + 420)}
        assert "REMOVE" not in expr

    def test_ttl_multiplier_none_leaves_ttl_alone(self):
        expr, names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert "#ttl" not in names
        assert ":ttl_val" not in values
        assert "REMOVE" not in expr

    def test_stale_limits_removed_but_never_rf(self):
        """Stale limit attrs go; the shared `rf` optimistic lock must not."""
        expr, names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names={"tpm"}, now_ms=1_789_000_000_000
        )
        removed = {names[a.strip()] for a in expr.split("REMOVE")[1].split(",")}
        assert removed == {bucket_attr("tpm", f) for f in ("tk", "cp", "ra", "rp", "tc")}
        assert bucket_attr("tpm", "rf") not in removed
        assert "rf" not in removed

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

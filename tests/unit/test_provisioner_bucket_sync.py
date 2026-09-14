"""Tests for the provisioner's sync boto3 bucket param sync (issue #481).

Mirrors the mocking conventions in test_provisioner_fanout.py: a MagicMock
boto3 DynamoDB client with `client.exceptions.*` populated with real exception
classes so `except client.exceptions.X` matches as it would against real boto3.
"""

from unittest.mock import MagicMock

from zae_limiter.schema import bucket_attr
from zae_limiter_provisioner.bucket_sync import build_bucket_param_update

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

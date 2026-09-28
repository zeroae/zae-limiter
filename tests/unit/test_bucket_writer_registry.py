"""Every bucket write declares whether it writes a quota's ``tk`` or ``gc`` (ADR-145 I1/I3).

A quota's allowance is conserved only if every writer of a quota's balance
(``b_{q}_tk``) or grant count (``b_{q}_gc``) follows the ADR-145 rules: within a
period tokens only move, and ``gc`` is written only by a reset, roll, create or
seed. The way a new writer breaks that is by landing unnoticed, so this file
makes every one of them be named here, with what it writes.

Discovery is deliberately broad rather than clever:

- every ``Repository`` method named ``build_*`` / ``_build_*`` (the write
  builders, whether or not they issue the call themselves), and
- every ``Repository`` method and every ``zae_limiter_aggregator.processor``
  function whose source issues a DynamoDB write (``update_item(``,
  ``put_item(``, ``delete_item(``, ``transact_write_items(``, or a
  ``"Update"`` / ``"Put"`` / ``"Delete"`` transaction item), or builds an
  ``UpdateExpression`` for someone else to issue.

That also sweeps in writers of config, audit and version items; they are
listed as ``NOT_BUCKET`` so the set stays exact. A new one fails
``test_every_writer_is_registered`` until someone decides which it is — and if
it writes a quota's ``tk`` or ``gc``, it must pass
``test_quota_conservation_fuzz.py`` and the ADR-145 acceptance tests, and
join ``test_expression_tokens.py``.

The generated ``SyncRepository`` mirrors ``Repository`` method for method and
is not listed separately.
"""

import inspect
import re

from zae_limiter.repository import Repository
from zae_limiter_aggregator import processor

NOT_BUCKET = None

# name -> (writes a quota's tk, writes a quota's gc), or NOT_BUCKET.
REPOSITORY = {
    # Builders.
    "build_bucket_put_item": (True, True),  # wraps build_composite_create
    "build_bucket_update_item": (True, False),  # legacy single-limit tk write
    "build_composite_create": (True, True),  # create: tk from a move or grant, gc = count
    "build_composite_normal": (True, True),  # rf-locked: reset/roll/seed stamp gc, pinned
    "build_composite_retry": (True, False),  # ADD only; never seeds a quota
    "build_composite_adjust": (True, False),  # adjust / rollback ADD
    "build_quota_donor_debits": (True, False),  # the donor side of a move
    "_build_quota_count_freeze": (False, True),  # gc = if_not_exists(gc, :g) on a raise
    "_build_bucket_param_update": (False, False),  # param sync: cp/ra/rp/sched, vu = 0
    # Direct bucket writes.
    "_speculative_consume_single": (True, False),  # the fast path ADD; never gc
    "bump_shard_count": (False, False),  # shard_count on shard 0
    "_propagate_shard_count": (False, False),  # shard_count on siblings (R6 residual)
    "_freeze_and_raise_shard_counts": (False, True),  # the planner's raise, with the freeze
    "_propagate_window_start": (False, False),  # ws / rsa / vu / wtc, never tk
    "_stamp_bucket_disabled": (False, False),  # disabled flag
    "_sync_one_bucket_shard": (False, False),  # issues _build_bucket_param_update
    "get_or_create_bucket": (True, True),  # legacy create via build_bucket_put_item
    "purge_namespace": (True, True),  # deletes whole items, buckets included
    # Executors: run builders registered above.
    "transact_write": NOT_BUCKET,
    "write_each": NOT_BUCKET,
    # Config, registry, audit and version items.
    "_cleanup_entity_config_registry": NOT_BUCKET,
    "_initialize_version_record": NOT_BUCKET,
    "_log_audit_event": NOT_BUCKET,
    "_register_namespace": NOT_BUCKET,
    "_require_reset_after_readers": NOT_BUCKET,
    "_set_entity_disabled": NOT_BUCKET,
    "_set_resource_disabled": NOT_BUCKET,
    "_write_audit_retention_config": NOT_BUCKET,
    "create_entity": NOT_BUCKET,
    "delete_limits": NOT_BUCKET,
    "delete_namespace": NOT_BUCKET,
    "delete_resource_defaults": NOT_BUCKET,
    "delete_system_defaults": NOT_BUCKET,
    "put_provisioner_state": NOT_BUCKET,
    "recover_namespace": NOT_BUCKET,
    "set_limits": NOT_BUCKET,
    "set_resource_defaults": NOT_BUCKET,
    "set_system_defaults": NOT_BUCKET,
    "set_version_record": NOT_BUCKET,
}

AGGREGATOR = {
    "try_refill_bucket": (True, True),  # refill ADD; a reset / roll stamps gc, pinned
    "_donor_update_items": (True, False),  # the donor side of a Path 2 move
    "_quota_count_freeze": (False, True),  # Path 1 raising a legacy item freezes gc
    "propagate_shard_count": (True, True),  # Path 1 raise + Path 2 clone put / transaction
    "try_proactive_shard": (False, False),  # shard_count on shard 0
    "update_snapshot": NOT_BUCKET,  # usage snapshot items
}

_WRITE = re.compile(
    r"update_item\(|put_item\(|delete_item\(|transact_write_items\("
    r'|"Update"\s*:|"Put"\s*:|"Delete"\s*:|"UpdateExpression"|UpdateExpression='
)


def _writes(fn) -> bool:
    return bool(_WRITE.search(inspect.getsource(fn)))


def _repository_writers() -> set[str]:
    found = set()
    for name, fn in inspect.getmembers(Repository, inspect.isfunction):
        if name.startswith(("build_", "_build_")) or _writes(fn):
            found.add(name)
    return found


def _aggregator_writers() -> set[str]:
    return {
        name
        for name, fn in inspect.getmembers(processor, inspect.isfunction)
        if fn.__module__ == processor.__name__ and (name.startswith("_build_") or _writes(fn))
    }


def _message(kind: str, missing: set[str], stale: set[str]) -> str:
    return (
        f"{kind} writers changed. Unregistered: {sorted(missing)}; registered but gone: "
        f"{sorted(stale)}. Add each new writer with whether it writes a quota's tk or gc "
        "(ADR-145 I1/I3); a quota writer must also pass test_quota_conservation_fuzz.py "
        "and join test_expression_tokens.py."
    )


def test_every_repository_writer_is_registered():
    found = _repository_writers()
    assert found == set(REPOSITORY), _message(
        "Repository", found - set(REPOSITORY), set(REPOSITORY) - found
    )


def test_every_aggregator_writer_is_registered():
    found = _aggregator_writers()
    assert found == set(AGGREGATOR), _message(
        "Aggregator", found - set(AGGREGATOR), set(AGGREGATOR) - found
    )


def test_the_fast_path_never_writes_gc():
    """I3: the speculative consume must stay 0 RCU + 1 WCU and blind to ``gc``."""
    assert REPOSITORY["_speculative_consume_single"] == (True, False)
    source = inspect.getsource(Repository._speculative_consume_single)
    assert "BUCKET_FIELD_GC" not in source
    assert "grant_count" not in source


def test_only_grant_writers_write_gc():
    """I3: ``gc`` is written only where a grant is sized — a reset or roll
    (normal path, aggregator refill), a create or seed (create, normal, Path 2
    clone), or the freeze that preserves an existing grant's size when a
    legacy item's count is raised."""
    gc_writers = {
        name for table in (REPOSITORY, AGGREGATOR) for name, v in table.items() if v and v[1]
    }
    assert gc_writers == {
        "build_bucket_put_item",
        "build_composite_create",
        "build_composite_normal",
        "_build_quota_count_freeze",
        "_freeze_and_raise_shard_counts",
        "get_or_create_bucket",
        "purge_namespace",
        "try_refill_bucket",
        "_quota_count_freeze",
        "propagate_shard_count",
    }

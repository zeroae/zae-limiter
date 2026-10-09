# Per-resource cascade policy: design

**Decision record:** [ADR-146](../adr/146-per-resource-cascade-policy.md)
**Issue:** [#676](https://github.com/zeroae/zae-limiter/issues/676)

The numbered decisions and the cost table below were ADR-146's Decision section until its
acceptance for v0.16.0, when they moved here so the record meets the ADR format standard
(100 lines; no API signatures or cost tables). They are unchanged. The ADR states what must
hold; this document is how it is built.

## Decisions in detail

The decisions below were agreed with the owner on 2026-10-04 during the v0.16.0
planning for #674. This ADR is their first written record.

**Partially supersedes ADR-112.** Whether the parent is included is decided per
(entity, resource) by the resolved policy below; ADR-112's META `cascade` remains the
default when no level sets one. ADR-112's other decisions stand: no per-call parameter,
the child decides, and `cascade` is aliased as a reserved word.

1. **Store a tri-state `cascade` on config items**, beside `disabled`: on resource config
   (`{ns}/RESOURCE#{r}`, `#CONFIG`) and entity config (`{ns}/ENTITY#{id}`,
   `#CONFIG#{resource}` and `#CONFIG#_default_`). Absent means inherit; `true` / `false`
   are explicit. Not supported on system config, matching ADR-125.

2. **Resolve it by the ADR-125 walk**: entity(resource) → entity(`_default_`) → resource,
   first explicit value wins, independent of which level supplies the limits. **Absent
   everywhere ⇒ the entity's META `cascade`**, so every existing deployment behaves
   exactly as before. An entity with no `parent_id` never cascades, whatever the policy.
   The slow path resolves it from the items `resolve_disabled()` already reads: **0 extra
   RCU**.

3. **The bucket item's `cascade` becomes the effective policy for (entity, resource).**
   The fast path keeps reading it from the item and stays **0 RCU**.

4. **The cache holds cascade per (entity, resource), and the item overrules it**
   (owner's "Option A"). The per-entity entry keeps `parent_id`, the shard counts and
   the entity-wide flag; a separate map per (entity, resource), learned from the entity's
   own item stamp, decides the warm path, with the entity-wide flag as the guess for a
   resource not yet seen. On the warm path:
   - cache says cascade, the child's returned item says not → refund the parent's debit
     (1 WCU) and correct the cache;
   - cache says no cascade, the item says cascade → issue the parent write after the child
     (exactly the cold path today) and correct the cache.
   A mismatch happens only after a policy change, at most once per (process, entity,
   resource). **Only a stamp carrying `parent_id` is a policy**: the owner stamp always
   writes both, so `cascade=False` with no `parent_id` is a bucket an older version
   created for a parent from its child's view (#684). For that one the cache's answer
   stands, and the stamp teaches the cache nothing.

5. **Stamps self-heal.** Every slow-path write — the create `Put` and the rf-locked normal
   write (the #684 owner stamp, `build_composite_normal(owner=...)`) — stamps the
   **resolved** policy and the owner's `parent_id`. A bucket the fan-out missed is
   corrected on its next slow pass. The one exception is the parent-only fallback
   (`_try_parent_only_acquire`), which never ran the disable walk: it stamps the
   parent's policy only when its config fetch read fresh, and otherwise leaves the stamp
   alone rather than pay a read for it.

6. **A change fans out eagerly**, through the ADR-125 discovery (GSI2 for a resource,
   GSI3 for an entity, two passes). Each discovered bucket is stamped with the policy
   resolved **for that bucket's own entity and resource**, so a resource-level change
   never clobbers an entity override, and a clear restamps whatever level now decides.
   Its config and META reads are strongly consistent (#705): it resolves the level it was
   just handed, and a stale replica answering "no policy" stamped every bucket with the old
   one, which the fast path then trusted.
   A bucket that only ever takes the fast path is reached only by this fan-out, which is
   why it is eager and not left to self-healing. The provisioner mirrors it
   (`fanout_cascade` in `zae_limiter_provisioner/fanout.py`). A manifest apply restamps
   only the levels whose stored policy actually changed, read from the config write's own
   `ALL_OLD` image at no extra read — unlike `disabled`, which fans out every declared
   level on every apply.

7. **Version gate**, reusing the ADR-141 machinery:
   - **Writer gate:** writing any cascade policy (setter keyword, dedicated method, or the
     provisioner) is refused unless the version record's `lambda_version` is
     `>= 0.16.0` or equals the writer's own version. A pre-0.16 provisioner rebuilds config
     items from the manifest without `cascade`, so its next apply would erase the policy
     silently.
   - **Ratchet:** an admitted write raises `client_min_version` to at least `0.16.0`,
     capped at a development writer's own build and never lowering it (ADR-141). A v0.15
     client decides cascade from META and stamps it on create, which would undo the
     policy; v0.15 already refuses to open a stack whose minimum it is below.

8. **Surfaces — both a setter keyword and dedicated methods:**
   - `set_resource_defaults(..., cascade=)` and `set_limits(..., cascade=)`, with the
     ADR-125 "preserve stored value" sentinel; passing it explicitly fans out.
   - Dedicated methods and CLI commands mirroring disable/enable/clear:
     `set_resource_cascade(resource, bool)` / `clear_resource_cascade(resource)` and
     `set_entity_cascade(entity_id, bool, resource=None)` /
     `clear_entity_cascade(entity_id, resource=None)`; CLI
     `resource set-cascade|clear-cascade` and `entity set-cascade|clear-cascade`.
   - Manifest `cascade:` on `resources.<name>` and `entities.<id>.resources.<name>`,
     round-tripped through `Custom::ZaeLimiterLimits` as a `Cascade` property. The
     manifest owns the policy for every item it declares: omitting `cascade` clears it on
     the next apply, as for `disabled` (owner decision). `cascade` on `system` is rejected
     with an error rather than ignored.
   - `get-defaults` / `get-limits` show an explicit policy, like `Status: DISABLED`.
   - The new repository methods (the setters and getters above, `resolve_access`,
     `resolve_cascade_from_fetched`) are **required** members of `RepositoryProtocol`, not a
     capability-gated option (ADR-109): the limiter calls them on every slow path. A
     third-party backend written for v0.15 must implement them to run v0.16.

9. **`limits plan` warns** when a manifest sets `cascade: true` on a resource that has
   **no entity entries** for it: the parent would then be limited by the per-user
   resource defaults, which is almost always a mistake.

10. **#677 (move an entity to a new parent) reuses the owner-stamp writer** from item 5
    to restamp `parent_id`, so one write path owns both attributes on bucket items.

With #675 (`acquire(..., also=...)`), each resource in the lease cascades per its own
resolved policy. A child's lease still covers child and parent only; the grandparent is
the parent's own acquires' business (#686).

## Cost

| Path | Change |
|------|--------|
| Fast path | None: 0 RCU, reads the item stamp |
| Slow path | None: resolved from the config items `resolve_disabled()` already reads |
| Warm path after a policy change | +1 WCU refund or one extra parent write, once per (process, entity, resource) |
| Setting a policy | O(buckets in scope) writes, admin path, same as `disable_resource()`; +1 consistent RCU for the gate read |
| LLM case (#674) | The `llm` budget stops cascading: **one parent write saved per call** |

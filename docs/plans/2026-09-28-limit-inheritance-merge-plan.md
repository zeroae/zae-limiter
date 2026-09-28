# Limit Inheritance by Merge — Implementation Plan

**Status:** Draft plan — open questions resolved, awaiting approval
**Date:** 2026-09-28
**ADRs:** ADR-143, ADR-144 (Proposed)
**Related:** ADR-118 (four-level hierarchy), ADR-136 (entity config ⇒ no bucket TTL), ADR-141 (version gate), ADR-142 (hidden config), #468 / #487 (limit-change fan-out), #633 (per-limit seeding)

## Problem

Limit resolution today is **override, not merge**. The first level with any limits (entity →
entity `_default_` → resource → system) supplies the **whole** set
(`ConfigCache._evaluate_hierarchy`, `Repository._resolve_limits_sequential`). Given

```yaml
system:
  limits:
    rpm: { capacity: 300 }
    tpm: { capacity: 10000 }
resources:
  gpt-4:
    limits:
      rpm: { capacity: 600 }
```

`gpt-4` is limited on `rpm` only. The system `tpm` is silently dropped. The same thing happens one
level up: an entity override that names only `rpm` drops the resource's `tpm`. Operators have to
repeat every limit at every level, and forgetting one removes that limit without any error.

## Goal

Let a config level declare that it **extends** the level below it. Its limits are then merged by
limit name with what that level resolves to:

- For each limit name, the highest-precedence level that defines it wins.
- The winning level supplies the **whole `Limit`**: numbers, `schedule`, `reset_schedule` and
  `reset_after` together. Fields are never mixed across levels, because mixing them would build a
  limit no operator wrote. (A `scale` schedule from system applied to an entity's capacity, for
  example.) The one deliberate exception is a **limit patch** (D8): a merging level can replace
  only the `schedule` of an inherited limit and keep its numbers.
- A merging level can remove an inherited limit explicitly.

Out of scope: merging arbitrary fields within one limit (D8 permits `schedule` only), and
changing the default. Override stays
the default. Changing it would be a silent behaviour change for every deployed manifest, and
`__init__.__all__` is the frozen v1.0 contract.

## Design decisions

### D1. Opt-in per level, not namespace-wide

A new tri-state config attribute, `inherit_limits`, is stored on resource, entity-resource and
entity `_default_` config items:

| Value | Meaning |
|-------|---------|
| absent / `false` | Today's behaviour: this level's limits are the whole set |
| `true` | Merge this level's limits over what the next level down resolves to |

It is meaningless on `system`, the bottom level, and is rejected there as `disabled` is.

**Why per level:** the user's two cases ("resource adds rpm on top of system" and "entity adds rpm
on top of resource") are local decisions. A namespace-wide switch would force one semantics on
every level of every tenant, and turning it on would change enforcement for every existing
override in one write. Per level, adopting merge costs one field on the configs that want it.

**Considered: a namespace-wide switch on the system config item.** It is simpler to explain, but it
is an all-or-nothing migration with no rollout path. If per-level proves verbose, it can be added
later as manifest sugar that stamps each level (see D6).

**Resolution walk.** Start at the highest level that has config. While the current level has
`inherit_limits: true`, descend to the next level that has config and fill in the names not yet
set. Stop at the first level without the flag, or at system. Example chain: entity(merge) →
`_default_`(absent) → stop. The resource level is not consulted, because `_default_` did not ask
for it. This is exactly today's walk plus a "continue" bit, so a chain with no flags resolves as
it does now.

### D2. Explicitly removing an inherited limit

A merging level can list names to drop: `exclude_limits: [tpm]`. This is a string-set attribute on
the config item, applied before the lower levels are consulted. Without it, the only way to drop
one inherited limit is to turn merge off and repeat the rest, which is the problem we are fixing.

**Considered: a tombstone limit (`tpm: null`).** Rejected. It would need a `Limit` that is not a
limit, and every `l_`/`w_` reader (`schema.config_limit_names`, both Lambdas) would have to learn
it. A separate attribute keeps limit attributes meaning "a limit".

### D3. `ConfigSource` stays one value, plus a per-limit map beside it

`resolve_limits()` keeps its `(limits, on_unavailable, config_source)` shape. `config_source`
becomes the **highest** level that contributed any limit. A new
`resolve_limits_detailed()` on `RepositoryProtocol` returns `dict[name, ConfigSource]` as well.
It has a default implementation that maps every name to `config_source`, so a third-party backend
that does not merge keeps working.

This keeps `_is_custom_config(config_source)` correct without changes: a bucket that got **any**
limit from an entity level is custom and persists (ADR-136). That is the right reading, since the
entity's own limits on that item must not expire.

### D4. Propagation: fan out resource and system changes to *merged* entity buckets only

This is the crux. Today, resource- and system-level changes reach buckets only through TTL expiry
(ADR-136, #271/#296). Under merge, an entity bucket carries **no TTL** (D3) but holds limits that
came from resource or system. A later `set_system_defaults(tpm=20000)` would never reach it.
Nothing detects the drift: the fast path never compares `cp` against config, and the normal path
writes `cp` only when seeding (#633).

**Decision.** After `set_resource_defaults()`, `delete_resource_defaults()`,
`set_system_defaults()` and `delete_system_defaults()`, fan out to the buckets of entities whose
resolution **inherits through** the changed level, and only those. Reuse
`_sync_bucket_params()`, which already resolves each discovered bucket by its own resource and
already stamps SET/REMOVE, `vu = 0`, the TTL decision per bucket and `FanoutIncomplete` (#468,
#487).

Discovery uses indexes that already exist:

| Change at | Entities to visit |
|-----------|-------------------|
| resource `R` | GSI3 `ENTITY_CONFIG#R` (entity configs for `R`) ∪ GSI3 `ENTITY_CONFIG#_default_` (entity-wide configs, which may inherit into `R`) |
| system | every entity with any entity config: `#ENTITY_CONFIG_RESOURCES` registry → GSI3 per resource |

Each candidate is filtered to those whose chain actually reaches the changed level with
`inherit_limits: true`. A non-merging entity is untouched, so the cost for an override-only
deployment is the discovery queries and **zero writes**.

This is not the O(all buckets) fan-out ADR-136 rejected. It is O(entities with explicit config
that opted into merge). Those are the same buckets ADR-136 exempted from TTL precisely because
they are deliberately configured. Buckets running purely on resource or system defaults still
propagate by TTL, unchanged.

**Considered: giving merged buckets a TTL whenever any limit comes from resource or system.** This
contradicts ADR-136 for exactly the entities it protects: rate-limit state reset on expiry, and
gaps in `tc` for usage snapshots.

**Considered: a per-limit config version checked on the fast path.** The fast path reads no config
by design (#315), so this would give up the 0 RCU path for every acquire.

Recorded as **ADR-144** (`docs/adr/144-fan-out-to-merged-entity-buckets.md`). It adds a fan-out
for merged entity buckets beside ADR-136 and cites it. It does not supersede ADR-136: ADR-136's
decision (which buckets carry a TTL) is unchanged, and it is Accepted, so it is not edited
(`adr-rules.md`).

### D5. The schedule timezone must agree across the merged set

A bucket item has one `sched_tz` (`models.hoisted_schedule_timezone()`). A merged set can combine
scheduled limits written at different levels in different zones. That can no longer be checked in
a single `set_limits()` call, because the conflicting zone may be on another level.

**Decision.** Validate at write time, in both directions:

- An entity or resource write with `inherit_limits: true` resolves its own chain and rejects a
  zone conflict with a `ValidationError` that names both limits and levels.
- A resource or system write checks the merging dependents it will fan out to (D4 discovery) and
  refuses before writing if any resulting set would conflict.

At resolution time, a conflict that got through anyway (a racing write, or an old writer) is
treated like corrupt config: `RateLimiterUnavailable`, which is subject to `on_unavailable`. It is
never resolved by guessing a zone, since the wrong zone moves a daily reset by hours with no error
(the failure `sched_tz` hoisting already guards against).

**Q1 (resolved 2026-09-28, owner): fail closed.** A zone conflict found at resolution is
`RateLimiterUnavailable`. The conflicting limit is never dropped, and no zone is guessed.
Write-time validation should make this unreachable in practice.

### D6. Manifest, CLI and CloudFormation surface

```yaml
resources:
  gpt-4:
    inherit_limits: true          # extend system
    limits:
      rpm: { capacity: 600 }      # tpm (10000) now comes from system
entities:
  user-premium:
    resources:
      gpt-4:
        inherit_limits: true      # extend gpt-4, which extends system
        exclude_limits: [tpm]     # but not tpm
        limits:
          rpm: { capacity: 1000 }
```

- Manifest: `inherit_limits` (bool) and `exclude_limits` (list of limit names, validated against
  `NAME_PATTERN`) on `resources.<name>` and `entities.<id>.resources.<name>`. Both are rejected
  on `system`. `exclude_limits` without `inherit_limits: true` is rejected, since it would do
  nothing.
- CloudFormation `Custom::ZaeLimiterLimits`: `InheritLimits` / `ExcludeLimits` properties, added to
  `handler._CFN_*` and `limits_cli._limits_to_cfn` as exact inverses, pinned by the existing
  round-trip test.
- CLI setters: `--inherit/--no-inherit` and `--exclude LIMIT` (repeatable) on
  `resource set-defaults` and `entity set-limits`. API: `inherit_limits=` / `exclude_limits=`
  keywords on `set_resource_defaults()` / `set_limits()`, using the same "preserve stored value"
  sentinel as `disabled`, because config writes are full-replace `PutItem`s.
- CLI readers: `entity get-limits` and `resource get-defaults` gain `--effective`, which prints the
  resolved set with the level each limit came from
  (`tpm: 10,000/min  (system, inherited)`). `limits plan` / `limits diff` print the effective set
  for each merged entry, so the answer to the user's original question is visible before
  applying.

### D7. Mixed versions

A client predating this feature ignores `inherit_limits` and resolves with override semantics.
That **under-enforces**: it drops the inherited limits, which is exactly today's behaviour. It
does not over-admit on the limits it does enforce. This is the same class of risk ADR-142
accepted, but it is still a silent divergence between clients sharing a bucket.

- **Provisioner (must gate).** `ResourceDecl.from_dict` / `EntityResourceDecl.from_dict` ignore
  unknown keys, so a pre-feature provisioner Lambda would **silently drop** `inherit_limits` and
  store an override-only item. Gate `limits apply` and the CFN handler the ADR-141 way: refuse
  unless the version record's `lambda_version` is at or above the release that introduces this,
  with the same `can_auto_update` / `zae-limiter upgrade` remedy.
- **Clients (ratchet).** A write that stores `inherit_limits: true` raises `client_min_version`
  with the existing ADR-141 ratchet (`ratcheted_client_min_version()`), so a too-old client is
  refused when it opens the repository rather than silently under-enforcing. The existing ADR-141
  holes still apply: the check runs only when a repository is opened, and v0.14 ignores the
  minimum.
- **Aggregator: unaffected.** It reads bucket items only and never resolves config.

**Q2 (resolved 2026-09-28, owner): raise the minimum client version.** Any admin or provisioner
write that stores `inherit_limits: true`, `exclude_limits` or `patch_limits` (D8) ratchets
`client_min_version` to the introducing release. It costs one conditional `UpdateItem` per such
write and turns silent under-enforcement into a loud refusal for every client from that release
on. ADR-141's documented holes are accepted as they are.

### D8. Limit patches: inherit the numbers, replace only the schedule

**Use case.** An entity should get `tpm` from the resource or system, so a later change to the
number still reaches it, but with its own time-of-day schedule. Under D1 alone, the entity has to
restate `capacity` beside the schedule. That pins the number at the entity level and cuts this
limit off from inheritance, which is what the feature exists to prevent.

```yaml
entities:
  user-premium:
    resources:
      gpt-4:
        inherit_limits: true
        patch_limits:
          tpm:                         # numbers come from below; only the schedule is this level's
            schedule:
              - { cron: "* 9-17 * * 1-5", tz: America/New_York, scale: 0.5 }
```

**Why this is consistent with "whole `Limit`".** Whole-limit granularity exists to stop the
resolver from assembling a limit **nobody wrote**. A patch is not assembled by the resolver. The
operator wrote "take whatever numbers come from below and apply this schedule", and that is the
limit they get. What stays forbidden is *implicit* field mixing: a level that declares `tpm` in
`limits` still replaces the inherited `tpm` whole.

**Rules.**

1. **Only `schedule` is patchable.** `reset_schedule` and `reset_after` change how the limit
   *recovers*. A quota has `refill_amount = 0` (ADR-137), so patching a reset onto an inherited
   dripping limit builds a limit that cannot be constructed, and a later change below could flip
   it between valid and invalid. The numeric fields are not patchable either: a level that wants
   its own capacity declares the limit in `limits`. Any other key under a patch is rejected by
   the manifest parser and by the API.
2. **A patch replaces; it does not append.** The patched schedule replaces any `schedule` the
   inherited limit carries, matching override-not-merge for schedules (#222). `schedule: []` is
   a legal patch meaning "inherit the numbers, drop the inherited schedule".
3. **Patches stack by precedence.** Resolution first finds the limit under D1 (the highest level
   that *declares* it), then applies the highest-precedence patch **above** that level, if any. A
   patch below the declaring level is shadowed, since the declaring level replaced the limit
   whole. At most one patch applies, so the result never depends on the order patches compose in.
4. **Requires `inherit_limits: true`.** A patch on a non-merging level has nothing to inherit and
   is rejected. The same name may not appear in more than one of `limits`, `patch_limits` and
   `exclude_limits` on a level.
5. **A patch whose limit disappears is inert.** If the level below stops providing `tpm` (removed
   or excluded), the patch applies to nothing and `tpm` is not enforced, which is what the
   removal asked for. It is not an error, since the patch writer cannot stop a system-level
   write. `limits plan` / `diff` and `get-limits --effective` report it as an orphaned patch.
6. **The combination is validated both ways, as with D5.** A patch is a valid `ScheduleEntry` on
   its own, but the patched `Limit` is only checkable against the inherited numbers: an absolute
   entry is subject to `Limit.__post_init__`'s interplay checks and the #570 magnitude bounds, and
   the zone joins the D5 timezone rule. So a patch write validates against the current chain, and
   a resource or system write validates the patches of the merging dependents it will fan out
   to. A combination that is still invalid at resolution is corrupt config:
   `RateLimiterUnavailable`, subject to `on_unavailable`. The resolver never silently drops the
   patch, because that would enforce a schedule the operator removed.

**Storage.** A new prefix, `p_{name}_sched`, holds the compact encoding (`schedule.encode()`,
versioned per #515) on the config item. The `sched_tz` hoisting rule covers patches as it covers
limits. Every current reader discovers a limit by its `_cp` attribute
(`schema.config_limit_names`), so a patch is **invisible to them, not corrupt**:

- A pre-feature client sees the level without the patch and enforces the inherited limit
  unscheduled. It under-enforces or over-enforces by exactly the schedule's effect (a `scale:
  0.5` window is not halved). This is covered by the D7 gate and ratchet.
- A config item holding *only* flags and patches has no limits of its own. Today's resolver reads
  that as an empty level and falls through, which for this item happens to be right. The new
  resolver must treat a level carrying `inherit_limits` as **present** even with an empty
  `limits`, and must not negative-cache it as `_NO_CONFIG`.
- `p_` is chosen over reusing `l_{name}_sched` without a `cp` so that no future reader can mistake
  a patch for a half-written limit. `bucket_sync._decode_limits` already has to refuse exactly
  that shape (#633).

**Bucket items: nothing new.** The resolved, patched `Limit` is an ordinary scheduled limit.
`_sync_bucket_params` and the slow path already stamp `b_{name}_sched` / `sched_tz` / `vu` from
the resolved limit, and the aggregator reads only the item. So a patch changes no bucket write
shape and no aggregator code.

**Propagation.** A patch write is a write at its own level, so an entity-level patch fans out like
any `set_limits()` today (#468, #487). A change to the inherited numbers below it reaches the
patched bucket through D4, since a patch implies `inherit_limits: true`.

**Considered: general field-level patches (`capacity` too).** Rejected. A patched `capacity` is
just an entity-level limit spelled differently, but one whose `refill_amount` still inherits. That
recreates the "limit nobody wrote" problem one field at a time. It can be revisited if a concrete
use case appears.

**Q3 (resolved 2026-09-28, owner): patches are allowed at every merging level.** A resource can
patch a system limit, and an entity (per-resource or `_default_`) can patch whatever it inherits.
The rules above have no level-specific part, and restricting them would be an asymmetry to explain
rather than a safety property. `system` cannot carry a patch, since it has nothing below it.

## Implementation phases

Each phase is its own PR with its own tests. Pre-existing bugs found along the way get their own
`fix(scope):` commits (`commits.md`).

### Phase 0: Design record

- [x] Write the ADRs (Proposed). ADR-000 allows one decision per ADR, so this is two:
      **ADR-143** (`docs/adr/143-merge-limits-through-hierarchy.md`) covers merge-by-name, opt-in
      per level, whole-`Limit` granularity, `exclude_limits`, schedule-only patches (D8), the
      fail-closed timezone rule and the client-version ratchet. **ADR-144**
      (`docs/adr/144-fan-out-to-merged-entity-buckets.md`) covers the targeted fan-out (D4)
      beside ADR-136.
- [ ] Open a tracking issue with `/issue create`, and pick its milestone by description. Patches
      get their own sub-issue, since Phase 6 can ship after the rest.
- [x] Open questions settled by the owner on 2026-09-28: Q1 fail closed, Q2 raise the minimum
      client version, Q3 patches allowed at resource and entity levels.

### Phase 1: Storage and resolution (core)

- [ ] `schema.py`: attribute names `inherit_limits` (BOOL) and `exclude_limits` (SS) on config
      items. Update the config-item docs in CLAUDE.md's attribute table.
- [ ] `repository.py`: serialize and deserialize both in the resource and entity config
      read/write paths. `batch_get_configs` returns them beside `(limits, on_unavailable)`.
      Change the tuple to a small `ConfigLevel` dataclass (limits, on_unavailable, inherit,
      exclude) rather than widening a positional tuple.
- [ ] `config_cache.py`: cache `ConfigLevel`, not bare limits. `_evaluate_hierarchy` implements
      the D1 walk and D2 exclusion, and returns the per-limit source map.
      **Cost: zero extra RCU.** The batched path already fetches all four levels on a miss
      (`_build_levels_and_check_cache`). Negative caching is unchanged.
- [ ] `_resolve_limits_sequential`: stop short-circuiting when the level found has
      `inherit_limits`. It then does up to four `GetItem`s, the same as a full miss today.
- [ ] `repository_protocol.py`: add `resolve_limits_detailed()` with the D3 default.
- [ ] Merge helper in one place (`models.merge_limit_levels(levels) -> (limits, sources)`), a
      pure function so the provisioner mirror (Phase 3) can be pinned against it by a test.
- [ ] `hatch run generate-sync`, and commit the regenerated `sync_*` files and sync tests.
- [ ] Unit tests (`test_config_cache.py`, `test_repository.py`, and generated sync twins):
  - the user's two scenarios, resolving `{rpm: 600, tpm: 10000}`;
  - override chain unchanged: no flag anywhere gives a byte-identical result to today;
  - the walk stops at the first non-merging level (entity(merge) → `_default_`(no flag) ignores
    resource);
  - a higher level wins by name, including when the higher limit is a quota and the lower one
    drips (whole `Limit`, not fields);
  - `exclude_limits` removes an inherited name but never the level's own limit;
  - merging onto an empty lower level; every level empty → `None`, as today;
  - hidden `w_` config (ADR-142) merges by name like `l_`;
  - `config_source` is the highest contributing level, and the source map is exact.

### Phase 2: Admission, TTL and seeding

- [ ] `limiter.py`: `_do_acquire`, `_try_parent_only_acquire` and `check_availability` consume
      the merged set. TTL keeps `_is_custom_config(config_source)` (D3). Add a test that a
      bucket with an entity `rpm` and a system `tpm` carries **no** `ttl`.
- [ ] Confirm #633 seeding covers "system gains `tpm` after a merged bucket exists": the slow
      path resolves `tpm`, finds it missing from the item and seeds it. Add a test; no code change
      expected.
- [ ] `RateLimitExceeded` / `lease.consumed`: no change, since both are driven by `consume`.
      Add one test that an inherited `tpm` named in `consume` is enforced and reported.

### Phase 3: Propagation (D4)

- [ ] `Repository._merged_dependents(level, resource)`: discovery per the D4 table, filtered to
      chains that reach the level through `inherit_limits: true`.
- [ ] Call `_sync_bucket_params()` per dependent after the config write in
      `set_resource_defaults` / `delete_resource_defaults` / `set_system_defaults` /
      `delete_system_defaults`. Evict the cache **before** the sync (the #487 ordering rule), keep
      it serial, and raise `FanoutIncomplete` with the count written.
- [ ] `_resolved_bucket_param_update()` must use the merged resolution, and its
      `stale_limit_names` intersection must treat an inherited name as declared.
- [ ] Mirror in `zae_limiter_provisioner/bucket_sync.py` (`resolve_bucket_limits`,
      `_resolved_plan`), since a manifest apply that changes a resource or system limit must fan
      out the same way. Add a parity test that runs the async and provisioner resolutions on the
      same fixtures.
- [ ] Tests (moto): a system `tpm` change reaches a merged entity's bucket on every shard; a
      non-merging entity's bucket is not written; an excluded name is not re-added; a partial
      failure raises `FanoutIncomplete` and a re-run converges.
- [ ] Add every new bucket write to `tests/unit/test_expression_tokens.py` (#634). No new write
      shape is expected, because `_sync_bucket_params` is reused.

### Phase 4: Validation (D5) and version gate (D7)

- [ ] Timezone checks in both directions, with a `ValidationError` naming both levels. At
      resolution, a conflict becomes `RateLimiterUnavailable`.
- [ ] Writer gate plus `client_min_version` ratchet, reusing the ADR-141 helpers in `version.py`
      (add `MIN_READER_VERSION_FOR_INHERIT`). The provisioner runs the gate in
      `handler._apply_and_record` before any write.

### Phase 5: Provisioner, CLI and CloudFormation surface (D6)

- [ ] `manifest.py`: parse and validate both fields, rejected on `system`.
      `differ.py` / `applier.py`: diff and write them. `#PROVISIONER` state records them so that
      removing the flag from a manifest is a diff.
- [ ] CFN round trip: `InheritLimits` / `ExcludeLimits` in `handler` and `limits_cli`, with the
      inverse test extended.
- [ ] CLI flags and `--effective` readers, plus the effective view in `limits plan` / `diff`.
      Run the `api-cli-parity` agent.
- [ ] E2E (LocalStack): apply the user's manifest, `acquire()` against `gpt-4` is rejected on
      `tpm` at 10,000, then raise the system `tpm` and observe it on the merged entity's bucket
      without waiting for TTL.

### Phase 6: Limit patches (D8)

This phase depends on Phases 1–5. It is separable: merge without patches is useful on its own, so
this phase can ship in a later release without reworking anything before it.

- [ ] `schema.py`: `p_{name}_sched` attribute builder. Add a test that
      `config_limit_names` does not discover it.
- [ ] `repository.py`: serialize and deserialize patches into `ConfigLevel.patches:
      dict[name, tuple[ScheduleEntry, ...]]`. An undecodable patch takes the whole item, as an
      undecodable `sched` does today (`RateLimiterUnavailable`). Decide how a level with flags
      but no limits is cached (present, not `_NO_CONFIG`).
- [ ] `models.merge_limit_levels`: apply rule 3 (highest patch above the declaring level) and
      rule 5 (orphans), and return the patch's level in the source map, for example
      `tpm: (system, schedule from entity)`.
- [ ] Validation (rule 6): patch writes check the current chain. Resource and system writes check
      the dependent patches found by the Phase 3 discovery. Both reuse `Limit.__post_init__`
      rather than restating its checks.
- [ ] API: `patch_limits=` keyword on `set_resource_defaults()` / `set_limits()`, with the
      preserve-stored sentinel. Manifest: `patch_limits` on `resources.<name>` and
      `entities.<id>.resources.<name>`, schedule key only (rule 1).
      CloudFormation: a `PatchLimits` property, with the round-trip test extended.
- [ ] CLI: `-l` cannot express a schedule (#222 §1.5), so there is no setter flag, and
      `limits apply` is the CLI path, as for schedules today. Readers: `--effective` and
      `limits plan` / `diff` show the patch and any orphaned patch.
- [ ] Provisioner mirror in `bucket_sync.resolve_bucket_limits`, pinned to the client merge by the
      Phase 3 parity test.
- [ ] Tests:
  - an entity patch over a system `tpm` resolves with system numbers and the entity schedule;
  - a resource patch over a system `tpm` applies to every merging entity of that resource, and an
    entity patch above it wins (rule 3);
  - `patch_limits` on `system` is rejected by the manifest and by the API;
  - a system `tpm` change reaches the patched bucket and keeps the schedule;
  - `schedule: []` drops an inherited schedule;
  - a patch below the declaring level is shadowed; the highest of two patches wins;
  - an orphaned patch enforces nothing and is reported;
  - reset fields in a patch are rejected;
  - a system write that would make a dependent patch invalid is refused before writing;
  - a pre-feature reader (fixture with a `p_` attribute) resolves as if the patch were absent.

### Phase 7: Documentation

- [ ] New user guide page `docs/guide/limit-inheritance.md`: override vs merge, the walk, the
      "whole limit" rule and why, `exclude_limits`, schedule patches and their orphan rule, the
      timezone rule, and the mixed-version caveat.
- [ ] Update CLAUDE.md (Centralized Configuration, config attribute table, writer table if the
      fan-out gains a row, pricing note for the fan-out), `docs/cli.md`, the manifest reference,
      and `docs/infra/cloudformation.md`. Run the `docs-updater` agent.
- [ ] Changelog: this is a `feat`. Opt-in, no behaviour change for existing configs.

## Cost summary

| Operation | Today | With merge |
|-----------|-------|------------|
| `acquire()`, warm cache | unchanged | unchanged (fast path reads no config) |
| Config cache miss (batched) | 1 `BatchGetItem`, 4 keys | same |
| Config cache miss (sequential fallback) | 1–4 `GetItem` | up to 4 when a level merges |
| `set_resource_defaults` / `set_system_defaults` | 1 `PutItem` | + discovery queries; + O(merged dependent buckets) WCU, 0 when nobody merges |
| Admin write storing `inherit_limits` | — | + 1 consistent `GetItem` (gate) + ≤1 conditional `UpdateItem` (ratchet) |

## Risks

- **A system-level write becomes O(merged entities).** A namespace with many merging entities
  makes `set_system_defaults()` slow and able to fail part-way. This is mitigated by
  `FanoutIncomplete` plus idempotent re-runs, and documented in the guide and in `performance.md`.
- **Surprise is moved, not removed.** Under merge, adding a limit at system silently adds it to
  every merging entity. That is the feature, but `limits plan` must show it (Phase 5), or it
  becomes the mirror image of today's silent drop.
- **The ADR-141 holes are inherited** by D7's client ratchet (checked only on open, and v0.14
  ignores it).
- **Patches make validity depend on other levels.** A system-level write can now be refused
  because of an entity's patch (D8 rule 6). That is the correct outcome, but it is new: the
  refusal must name the entity and the patch, or the operator will not know why a system write
  failed.
- **Patches spread one limit across two levels.** An operator reading only the entity config sees
  a schedule with no numbers. The `--effective` view and `limits plan` are the mitigation, and
  the guide should lead with them.

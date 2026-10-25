# ADR-152: Acquire policies grant the item actions they write with

**Status:** Proposed (accepted at the v0.17.0 release)
**Date:** 2026-10-20
**Issue:** [#750](https://github.com/zeroae/zae-limiter/issues/750), [#756](https://github.com/zeroae/zae-limiter/issues/756)
**Supersedes:** [ADR-124](124-policy-rename-clarity.md) on acceptance (its permission matrix only; the policy names stand)
**Related:** [ADR-117](117-managed-policies-by-default.md), [ADR-150](150-move-entity-to-new-parent.md)

## Context

ADR-124 renamed the managed policies and fixed their permission matrix. That matrix gave
`AcquireOnlyPolicy` (`-acq`) `TransactWriteItems` but no `PutItem` or `UpdateItem`, on the
assumption that `TransactWriteItems` authorises the items inside a transaction. The
namespace-scoped `NamespaceAcquirePolicy` (`-ns-acq`) was given `UpdateItem` but no `PutItem`.

AWS does not authorise a transaction that way. Each item is authorised by its own action:
"Permissions for Put, Update, Delete, and Get actions are governed by the permissions used for
the underlying PutItem, UpdateItem, DeleteItem, and GetItem operations. For the ConditionCheck
action, you can use the dynamodb:ConditionCheckItem permission"
([Using IAM with DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)).
`acquire()` writes with `UpdateItem` (the speculative fast path, adjustments, rollbacks, and
every `Update` in the slow path's transaction) and with `PutItem` (every bucket create). A
principal holding only `-acq` could therefore not write at all, and one holding only `-ns-acq`
could never create a bucket. IAM defines no `TransactWriteItems` action at all: the DynamoDB
[service reference](https://servicereference.us-east-1.amazonaws.com/v1/dynamodb/dynamodb.json)
lists none, so the entry granted nothing. The optional `{stack}-app` role carries `-acq` alone. moto and
LocalStack do not evaluate IAM, so no test below real AWS could see it.

## Decision

The policy names from ADR-124 are unchanged. The table-level permission matrix must be the
following. Each namespace-scoped policy (`-ns-read`, `-ns-acq`, `-ns-full`) mirrors its
table-level counterpart within its `dynamodb:LeadingKeys` condition: it must grant the same
item actions (`GetItem`, `BatchGetItem`, `Query`, `ConditionCheckItem`, `PutItem`, `UpdateItem`,
`DeleteItem`, as the row allows), and leaves out the table-wide ones (`Scan`, `DescribeTable`,
`BatchWriteItem`). No policy lists `TransactWriteItems`, which IAM does not define.

| Action | ReadOnly (`-read`) | AcquireOnly (`-acq`) | FullAccess (`-full`) |
|--------|:--------:|:-----------:|:----------:|
| `GetItem` | ✅ | ✅ | ✅ |
| `BatchGetItem` | ✅ | ✅ | ✅ |
| `Query` | ✅ | ✅ | ✅ |
| `Scan` | ✅ | ❌ | ✅ |
| `DescribeTable` | ✅ | ❌ | ✅ |
| `ConditionCheckItem` | ❌ | ✅ | ✅ |
| `PutItem` | ❌ | ✅ bucket items | ✅ |
| `UpdateItem` | ❌ | ✅ bucket items | ✅ |
| `DeleteItem` | ❌ | ❌ | ✅ |
| `BatchWriteItem` | ❌ | ❌ | ✅ |

`-acq` gains `PutItem` and `UpdateItem`; `-ns-acq` gains `PutItem`. An acquire policy must grant
every item action `acquire()` issues, whether alone or inside a transaction, and must grant
`PutItem` and `UpdateItem` on bucket items only: `dynamodb:LeadingKeys` `*/BUCKET#*` (and
`{namespace id}/BUCKET#*` for `-ns-acq`), with `Null` false on the key. Every write `acquire()`
issues is a bucket item, whose partition key is `{ns}/BUCKET#…`; namespace and entity ids contain
no `/` or `#`, so no other item matches.

## Consequences

**Positive:**
- `acquire()` works under the template's acquire policies and the `{stack}-app` role, as their
  names promise.
- The matrix states what AWS actually evaluates, and a real-AWS test pins it.

**Negative:**
- A principal holding only an acquire policy cannot create entities, set limits, reset or top
  up a bucket, or register a namespace on a first `Repository.open()`: those write config,
  entity, audit, registry or version items, which are operator actions (`-full`, `-ns-full`).
  Breaking for an application that did them with an acquire policy alone.
- Least privilege, not a boundary: a principal that may acquire still writes its own bucket
  items, so it can raise their balances with a direct write (ADR-149).

## Alternatives Considered

### Keep ADR-124's matrix and document that `-acq` cannot write
Rejected because: a policy named for `acquire()` that cannot run `acquire()` is the misleading
name ADR-124 set out to remove.

### Grant the write actions on every item
Rejected because: it lets an application role write config, entity records, audit events and
the version record (#756). This ADR first rejected the bucket-only pattern on the belief that a
table-level pattern cannot single out buckets; `*/BUCKET#*` does, since `LeadingKeys` patterns
match across the namespace id.

### Route every `acquire()` write through `TransactWriteItems`
Rejected because: AWS authorises a transaction's items by the same per-item actions, so it would
still need `PutItem` / `UpdateItem`, and it doubles the cost of every write.

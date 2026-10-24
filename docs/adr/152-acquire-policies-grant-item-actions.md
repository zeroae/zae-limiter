# ADR-152: Acquire policies grant the item actions they write with

**Status:** Proposed (accepted at the v0.17.0 release)
**Date:** 2026-10-20
**Issue:** [#750](https://github.com/zeroae/zae-limiter/issues/750)
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
| `PutItem` | ❌ | ✅ | ✅ |
| `UpdateItem` | ❌ | ✅ | ✅ |
| `DeleteItem` | ❌ | ❌ | ✅ |
| `BatchWriteItem` | ❌ | ❌ | ✅ |

`-acq` gains `PutItem` and `UpdateItem`; `-ns-acq` gains `PutItem`. An acquire policy must grant
every item action `acquire()` issues, whether alone or inside a transaction.

## Consequences

**Positive:**
- `acquire()` works under the template's acquire policies and the `{stack}-app` role, as their
  names promise.
- The matrix states what AWS actually evaluates, and a real-AWS test pins it.

**Negative:**
- `-acq` can now `PutItem` / `UpdateItem` any item in the table, not only buckets: IAM cannot
  tell a bucket item from a config item. It still has no `DeleteItem`, `Scan` or
  `BatchWriteItem`.
- `-ns-acq` can likewise write any item in its own namespace; `LeadingKeys` still keeps it out
  of other namespaces, and it still reads only from the reserved `_` prefix.

## Alternatives Considered

### Keep ADR-124's matrix and document that `-acq` cannot write
Rejected because: a policy named for `acquire()` that cannot run `acquire()` is the misleading
name ADR-124 set out to remove.

### Restrict the new write actions to bucket items with a `LeadingKeys` pattern
Rejected because: a bucket's partition key begins with the opaque namespace id, so a table-level
pattern cannot single out buckets, and `LeadingKeys` matches only the partition key.

### Route every `acquire()` write through `TransactWriteItems`
Rejected because: AWS authorises a transaction's items by the same per-item actions, so it would
still need `PutItem` / `UpdateItem`, and it doubles the cost of every write.

"""Lease management for rate limit acquisitions."""

import asyncio
import logging
import time
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .bucket import calculate_available, calculate_retry_after, force_consume, try_consume
from .exceptions import LeaseExpiredError, RateLimitExceeded
from .models import BucketState, Limit, LimitStatus
from .schema import calculate_bucket_ttl_seconds

# TransactionConflict retry constants (Issue #332)
_CONFLICT_MAX_RETRIES = 3
_CONFLICT_BASE_DELAY_S = 0.025  # 25ms, doubles each retry: 25ms, 50ms, 100ms

if TYPE_CHECKING:
    from .repository_protocol import RepositoryProtocol

logger = logging.getLogger(__name__)


@dataclass
class LeaseEntry:
    """Tracks a single bucket within a lease."""

    entity_id: str
    resource: str
    limit: Limit
    state: BucketState
    consumed: int = 0  # total consumed during this lease (tokens, not milli)
    # Original values from DynamoDB read, for ADD delta computation (ADR-115)
    _original_tokens_milli: int = 0  # stored tk before try_consume
    _original_rf_ms: int = 0  # shared rf from composite item read
    _is_new: bool = False  # True if item needs Create path (no existing item)
    # Config source tracking for TTL calculation (Issue #271)
    _has_custom_config: bool = False  # True if entity has custom limits (no TTL)
    # Write-on-enter tracking (Issue #309)
    _initial_consumed: int = 0  # consumption written to DynamoDB on enter
    # Shard the initial consumption was written to (GHSA-76rv). Adjustments
    # and rollbacks must target that same bucket item: the speculative path
    # picks a shard at random, so assuming shard 0 debits a bucket that never
    # held the consumption and, on rollback, credits it tokens it never lost.
    # The slow path targets the shard the fast path selected (issue #439), so
    # _commit_initial() reads and creates on this shard too.
    _shard_id: int = 0
    # Cached shard_count at acquire time; stamped on the item when
    # _commit_initial() creates a new shard bucket (issue #439).
    _shard_count: int = 1
    # Denormalized entity fields for speculative writes (Issue #315)
    _cascade: bool = False
    _parent_id: str | None = None
    # Whether the caller named this limit in acquire(consume=...) (Issue #455).
    # `consume` is the declared scope of a lease: only declared entries are
    # visible through `consumed` and adjustable through adjust()/consume()/
    # release(). The slow path still carries an entry for every resolved
    # limit because _commit_initial() needs them — build_composite_create()
    # writes only the states it is handed, and build_composite_normal()
    # advances the shared `rf` while crediting refill only to the limits it
    # is handed — but those undeclared entries are write-only carriers.
    _declared: bool = True


@dataclass
class Lease:
    """
    Manages an active rate limit acquisition.

    Tracks consumption across multiple entities/limits and handles
    rollback on exception.
    """

    repository: "RepositoryProtocol"
    entries: list[LeaseEntry] = field(default_factory=list)
    _committed: bool = False
    _rolled_back: bool = False
    _initial_committed: bool = False  # True after _commit_initial() succeeds (Issue #309)
    degraded: bool = False
    """Whether this is the no-op lease yielded under ``on_unavailable=ALLOW``.

    ``True`` only when the backend was unreachable and the limiter degraded
    to allowing the request (Issue #455). Such a lease has no entries, and
    ``adjust()``, ``consume()`` and ``release()`` are silent no-ops on it —
    the declared-scope check that normally reports keys outside ``consume``
    is skipped, so an outage never turns into a warning storm. Set
    explicitly where that lease is built, never inferred from an empty
    ``entries``: a real lease with nothing declared is not degraded.
    """
    # Keys in acquire(consume=...) that named no configured limit. acquire()
    # already reported them with the right advice, so adjust()/consume()/
    # release() must not report them again with the wrong one ("name the
    # limit in consume" — the caller did). Set where the slow path builds the
    # lease (Issue #455).
    _unknown_keys: frozenset[str] = frozenset()
    # Names of the declared limits, computed once at construction so the hot
    # path (adjust/consume/release on every request) allocates nothing to
    # answer "is this key declared?". Entries are never appended after the
    # lease is built.
    _declared_names: frozenset[str] = field(init=False, default=frozenset())

    def __post_init__(self) -> None:
        self._declared_names = frozenset(
            entry.limit.name for entry in self.entries if entry._declared
        )

    @property
    def consumed(self) -> dict[str, int]:
        """Total consumed amounts by limit name (declared limits only)."""
        result: dict[str, int] = {}
        for entry in self.entries:
            if not entry._declared:
                continue
            name = entry.limit.name
            result[name] = result.get(name, 0) + entry.consumed
        return result

    def _check_declared(self, amounts: dict[str, int], method: str) -> None:
        """Report keys that name no declared limit on this lease (Issue #455).

        A limit absent from ``consume`` was never checked at admission, so
        adjusting it afterwards would drive a bucket negative that never had
        the chance to reject; a typo (``tpmm`` for ``tpm``) would otherwise
        be silent forever. Emits ``FutureWarning`` now; becomes
        ``ValidationError`` in v1.0.0.

        ``FutureWarning`` rather than ``DeprecationWarning``: Python's default
        filters hide ``DeprecationWarning`` unless it is attributed to
        ``__main__``, and ``stacklevel`` attributes this one to application
        code, so it would never surface in a real deployment — the typo
        would stay silent, the exact failure Issue #455 exists to fix.
        ``FutureWarning`` is the category documented for warnings aimed at
        end users of an application and is shown by default.

        The degraded lease yielded under ``on_unavailable=ALLOW`` is exempt:
        it has no entries by design, and warning on every call during an
        outage would turn graceful degradation into noise.
        """
        # Fast exit, no allocation: every key is declared (the common case).
        if self.degraded or amounts.keys() <= self._declared_names:
            return
        # Keys acquire() already reported as unknown are skipped silently.
        undeclared = sorted(amounts.keys() - self._declared_names - self._unknown_keys)
        if not undeclared:
            return
        declared = sorted(self._declared_names)
        warnings.warn(
            f"lease.{method}() names limit(s) {undeclared} that were not declared in "
            f"acquire(consume=...); declared limits on this lease: {declared}. "
            "Undeclared keys are ignored. Name the limit in `consume` (an estimate "
            "of 0 is valid) to make it adjustable. This becomes a ValidationError "
            "in v1.0.0.",
            FutureWarning,
            # 1 = this helper, 2 = adjust()/consume()/release(), 3 = the caller
            stacklevel=3,
        )

    @property
    def _has_adjustments(self) -> bool:
        """Whether any post-enter adjustments were made (Issue #309)."""
        return any(entry.consumed != entry._initial_consumed for entry in self.entries)

    async def consume(self, **amounts: int) -> None:
        """
        Consume additional capacity from the buckets.

        Raises RateLimitExceeded if any bucket has insufficient capacity.

        Only limits declared in ``acquire(consume=...)`` can be consumed;
        other keys are reported (Issue #455) and ignored.

        Args:
            **amounts: Mapping of limit_name -> amount to consume
        """
        if self._committed or self._rolled_back:
            raise LeaseExpiredError()

        self._check_declared(amounts, "consume")

        now_ms = int(time.time() * 1000)
        statuses: list[LimitStatus] = []
        updates: list[tuple[LeaseEntry, int, int]] = []  # (entry, new_tokens, new_refill)

        # Check all declared limits first
        for entry in self.entries:
            if not entry._declared:
                continue
            amount = amounts.get(entry.limit.name, 0)
            if amount <= 0:
                continue

            result = try_consume(entry.state, amount, now_ms)

            status = LimitStatus(
                entity_id=entry.entity_id,
                resource=entry.resource,
                limit_name=entry.limit.name,
                limit=entry.limit,
                available=result.available,
                requested=amount,
                exceeded=not result.success,
                retry_after_seconds=result.retry_after_seconds,
            )
            statuses.append(status)

            if result.success:
                updates.append((entry, result.new_tokens_milli, result.new_last_refill_ms))

        # Also include statuses for limits not being consumed (for full visibility)
        for entry in self.entries:
            if entry._declared and entry.limit.name not in amounts:
                available = calculate_available(entry.state, now_ms)
                statuses.append(
                    LimitStatus(
                        entity_id=entry.entity_id,
                        resource=entry.resource,
                        limit_name=entry.limit.name,
                        limit=entry.limit,
                        available=available,
                        requested=0,
                        exceeded=False,
                        retry_after_seconds=0.0,
                    )
                )

        # Check for violations
        violations = [s for s in statuses if s.exceeded]
        if violations:
            raise RateLimitExceeded(statuses)

        # Apply updates to local state (will be persisted on commit)
        for entry, new_tokens, new_refill in updates:
            entry.state.tokens_milli = new_tokens
            entry.state.last_refill_ms = new_refill
            amount = amounts.get(entry.limit.name, 0)
            entry.consumed += amount
            # Update consumption counter if initialized (issue #179)
            if entry.state.total_consumed_milli is not None:
                entry.state.total_consumed_milli += amount * 1000

    async def adjust(self, **amounts: int) -> None:
        """
        Adjust consumption by delta (positive or negative).

        Never raises - allows bucket to go negative.
        Use for post-hoc reconciliation (e.g., LLM token counts).

        Only limits declared in ``acquire(consume=...)`` can be adjusted;
        other keys are reported (Issue #455) and ignored.

        Args:
            **amounts: Mapping of limit_name -> delta (positive = consume more)
        """
        if self._committed or self._rolled_back:
            raise LeaseExpiredError()

        self._check_declared(amounts, "adjust")
        self._apply_adjust(amounts)

    def _apply_adjust(self, amounts: dict[str, int]) -> None:
        """Apply adjust() deltas to declared entries (shared with release())."""
        now_ms = int(time.time() * 1000)

        for entry in self.entries:
            if not entry._declared:
                continue
            amount = amounts.get(entry.limit.name, 0)
            if amount == 0:
                continue

            new_tokens, new_refill = force_consume(entry.state, amount, now_ms)
            entry.state.tokens_milli = new_tokens
            entry.state.last_refill_ms = new_refill
            entry.consumed += amount
            # Update consumption counter if initialized (issue #179)
            # Net tracking: counter decreases on release/adjust(negative)
            if entry.state.total_consumed_milli is not None:
                entry.state.total_consumed_milli += amount * 1000

    async def release(self, **amounts: int) -> None:
        """
        Return unused capacity to bucket.

        Equivalent to ``adjust()`` with every amount negated: the returned
        tokens are credited unconditionally, so the bucket can end up above
        its capacity until the next refill re-caps it.

        Only limits declared in ``acquire(consume=...)`` can be released;
        other keys are reported (Issue #455) and ignored.

        Args:
            **amounts: Mapping of limit_name -> amount to return
        """
        if self._committed or self._rolled_back:
            raise LeaseExpiredError()

        self._check_declared(amounts, "release")
        negated = {k: -v for k, v in amounts.items()}
        self._apply_adjust(negated)

    async def _commit_initial(self) -> None:
        """Write initial consumption to DynamoDB on context enter (Issue #309).

        Persists bucket state using ADD-based writes (ADR-115). Groups entries
        by (entity_id, resource, shard) to build one composite update per
        bucket item. Uses Normal write path first (ADD with refill, CONDITION
        rf=expected). On ConditionalCheckFailedException, falls back to Retry
        path.

        A new bucket is created on the shard the acquire selected, with the
        cached shard_count stamped on it (issue #439). The create is guarded
        by ``attribute_not_exists(PK)``, so if the aggregator's shard
        propagation wins the race the transaction fails its condition check
        and the Retry path debits the now-existing item under ``tk >=
        consumed`` — the same gate the speculative write uses, so the race
        can never over-admit.

        After successful write, records _initial_consumed on each entry so
        that _commit_adjustments() can compute deltas.
        """
        if self._initial_committed or self._committed or self._rolled_back:
            return

        now_ms = int(time.time() * 1000)
        repo = self.repository

        # Group entries by (entity_id, resource, shard) — one item per bucket,
        # and the shard is part of a bucket's identity (GHSA-76rv).
        groups: dict[tuple[str, str, int], list[LeaseEntry]] = {}
        for entry in self.entries:
            key = (entry.entity_id, entry.resource, entry._shard_id)
            groups.setdefault(key, []).append(entry)

        # Build transaction items
        items: list[dict[str, Any]] = []
        for (entity_id, resource, shard_id), group_entries in groups.items():
            is_new = group_entries[0]._is_new

            # Calculate TTL based on config source (Issue #271)
            has_custom_config = group_entries[0]._has_custom_config
            limits = [e.limit for e in group_entries]
            multiplier = self.repository._bucket_ttl_refill_multiplier

            if has_custom_config:
                ttl_seconds: int | None = 0  # 0 means REMOVE ttl
            elif multiplier <= 0:
                ttl_seconds = None
            else:
                ttl_seconds = calculate_bucket_ttl_seconds(limits, multiplier)

            if is_new:
                first_entry = group_entries[0]
                items.append(
                    repo.build_composite_create(
                        entity_id=entity_id,
                        resource=resource,
                        states=[e.state for e in group_entries],
                        now_ms=now_ms,
                        ttl_seconds=ttl_seconds if ttl_seconds != 0 else None,
                        cascade=first_entry._cascade,
                        parent_id=first_entry._parent_id,
                        shard_id=shard_id,
                        shard_count=first_entry._shard_count,
                    )
                )
            else:
                consumed: dict[str, int] = {}
                refill_amounts: dict[str, int] = {}
                expected_rf = group_entries[0]._original_rf_ms

                for entry in group_entries:
                    name = entry.limit.name
                    consumed[name] = entry.consumed * 1000  # to millitokens
                    consumed_milli = entry.consumed * 1000
                    refill_amounts[name] = (
                        entry.state.tokens_milli - entry._original_tokens_milli + consumed_milli
                    )

                items.append(
                    repo.build_composite_normal(
                        entity_id=entity_id,
                        resource=resource,
                        consumed=consumed,
                        refill_amounts=refill_amounts,
                        now_ms=now_ms,
                        expected_rf=expected_rf,
                        ttl_seconds=ttl_seconds,
                        shard_id=shard_id,
                    )
                )

        if not items:
            self._initial_committed = True
            return

        # Retry loop for TransactionConflict (Issue #332)
        condition_failed = False
        condition_exc: Exception | None = None
        for attempt in range(_CONFLICT_MAX_RETRIES + 1):
            try:
                await repo.transact_write(items)
                break  # success
            except Exception as exc:
                # Check ConditionalCheckFailed first — it takes priority over
                # TransactionConflict because it means the optimistic lock failed,
                # requiring the consumption-only retry path.
                if _is_condition_check_failure(exc):
                    condition_failed = True
                    condition_exc = exc
                    break
                if _is_transaction_conflict(exc):
                    if attempt < _CONFLICT_MAX_RETRIES:
                        delay = _CONFLICT_BASE_DELAY_S * (2**attempt)
                        logger.debug(
                            "TransactionConflict (attempt %d/%d), retrying in %.3fs",
                            attempt + 1,
                            _CONFLICT_MAX_RETRIES,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise  # exhausted retries, propagate
                raise  # other errors propagate unchanged

        if condition_failed:
            # Retry path: ADD consumption only, CONDITION tk>=consumed per limit
            logger.debug("Normal write failed (optimistic lock), retrying consumption-only")
            # A cancelled transaction rolls back every item, including a
            # new-shard Put whose own condition passed. Per-index reasons tell
            # the innocent Put apart from the one that lost its
            # attribute_not_exists race: re-issue the former as-is (the item
            # is still missing, so a consumption-only retry would fail its
            # tk >= consumed check and surface as a spurious rejection); debit
            # the latter consumption-only on that same shard (issue #439). A
            # single-item write has no reasons list: that group is the loser.
            reason_codes = (
                _get_cancellation_reason_codes(condition_exc) if condition_exc is not None else None
            )

            def _consumption_only(
                entity_id: str, resource: str, shard_id: int, group_entries: list[LeaseEntry]
            ) -> dict[str, Any] | None:
                consumed = {
                    e.limit.name: e.consumed * 1000 for e in group_entries if e.consumed > 0
                }
                if not consumed:
                    return None
                return repo.build_composite_retry(
                    entity_id=entity_id, resource=resource, consumed=consumed, shard_id=shard_id
                )

            retry_items: list[dict[str, Any]] = []
            # Group behind each retry item, so a re-issued Put that loses its
            # own create race on the retry can be downgraded in place.
            retry_groups: list[tuple[tuple[str, str, int], list[LeaseEntry]]] = []
            for idx, (key, group_entries) in enumerate(groups.items()):
                failed_here = (
                    reason_codes is None
                    or idx >= len(reason_codes)
                    or reason_codes[idx] == "ConditionalCheckFailed"
                )
                if group_entries[0]._is_new and not failed_here:
                    retry_items.append(items[idx])
                    retry_groups.append((key, group_entries))
                    continue
                retry_item = _consumption_only(*key, group_entries)
                if retry_item is not None:
                    retry_items.append(retry_item)
                    retry_groups.append((key, group_entries))

            # Bounded: a re-issued Put may lose the create race exactly once
            # more (the shard now exists), after which every item is a
            # consumption-only debit whose failure is a real rejection.
            for retry_attempt in range(2):
                if not retry_items:
                    break
                try:
                    await repo.transact_write(retry_items)
                    break
                except Exception as retry_exc:
                    if not _is_condition_check_failure(retry_exc):
                        raise
                    codes = _get_cancellation_reason_codes(retry_exc)
                    downgraded: list[dict[str, Any]] = []
                    lost_put = False
                    for i, item in enumerate(retry_items):
                        failed_here = (
                            codes is None
                            or i >= len(codes)
                            or codes[i] == ("ConditionalCheckFailed")
                        )
                        if isinstance(item, dict) and "Put" in item and failed_here:
                            key, group_entries = retry_groups[i]
                            fallback = _consumption_only(*key, group_entries)
                            if fallback is not None:
                                downgraded.append(fallback)
                                lost_put = True
                            continue
                        if failed_here:
                            lost_put = False  # a debit failed: truly exhausted
                            break
                        downgraded.append(item)
                    if not lost_put or retry_attempt == 1:
                        statuses = _build_retry_failure_statuses(self.entries)
                        raise RateLimitExceeded(statuses) from retry_exc
                    retry_items = downgraded

        # Record initial consumed amounts after successful write
        self._initial_committed = True
        for entry in self.entries:
            entry._initial_consumed = entry.consumed

    async def _commit_adjustments(self) -> None:
        """Write post-enter adjustment deltas to DynamoDB on context exit (Issue #309).

        No-op if no adjust/consume/release calls were made during the context.
        Uses build_composite_adjust() for unconditional ADD, dispatched via
        write_each() (independent single-item writes, 1 WCU each).
        """
        if self._committed or self._rolled_back:
            return

        if not self._has_adjustments:
            self._committed = True
            return

        repo = self.repository

        # Group entries by (entity_id, resource, shard) — one adjust item per
        # bucket item, and the shard is part of a bucket's identity.
        groups: dict[tuple[str, str, int], list[LeaseEntry]] = {}
        for entry in self.entries:
            key = (entry.entity_id, entry.resource, entry._shard_id)
            groups.setdefault(key, []).append(entry)

        items: list[dict[str, Any]] = []
        for (entity_id, resource, shard_id), group_entries in groups.items():
            deltas: dict[str, int] = {}
            for entry in group_entries:
                delta = entry.consumed - entry._initial_consumed
                if delta != 0:
                    deltas[entry.limit.name] = delta * 1000  # to millitokens

            if deltas:
                item = repo.build_composite_adjust(
                    entity_id=entity_id,
                    resource=resource,
                    deltas=deltas,
                    shard_id=shard_id,
                )
                if item:
                    items.append(item)

        if items:
            await repo.write_each(items)

        self._committed = True

    async def _rollback(self) -> None:
        """Write compensating deltas to restore consumed tokens (Issue #309).

        On error, the initial consumption was already written to DynamoDB by
        _commit_initial(). This method restores those tokens by writing
        negative deltas using build_composite_adjust() via write_each()
        (independent single-item writes, 1 WCU each).
        """
        if self._committed or self._rolled_back:
            return

        self._rolled_back = True

        # Nothing was written to DynamoDB, no compensation needed
        if not self._initial_committed:
            return

        repo = self.repository

        # Group entries by (entity_id, resource, shard) — one adjust item per
        # bucket item, and the shard is part of a bucket's identity.
        groups: dict[tuple[str, str, int], list[LeaseEntry]] = {}
        for entry in self.entries:
            key = (entry.entity_id, entry.resource, entry._shard_id)
            groups.setdefault(key, []).append(entry)

        items: list[dict[str, Any]] = []
        for (entity_id, resource, shard_id), group_entries in groups.items():
            deltas: dict[str, int] = {}
            for entry in group_entries:
                # Negate only what was written on enter
                if entry._initial_consumed != 0:
                    deltas[entry.limit.name] = -entry._initial_consumed * 1000

            if deltas:
                item = repo.build_composite_adjust(
                    entity_id=entity_id,
                    resource=resource,
                    deltas=deltas,
                    shard_id=shard_id,
                )
                if item:
                    items.append(item)

        if items:
            try:
                await repo.write_each(items)
            except Exception:
                logger.warning(
                    "Failed to rollback consumed tokens for entities: %s",
                    list(groups.keys()),
                    exc_info=True,
                )


def _get_cancellation_reason_codes(exc: Exception) -> list[str] | None:
    """Extract CancellationReasons codes from a TransactionCanceledException.

    Returns a list of reason codes (e.g. ["ConditionalCheckFailed", "None"]),
    or None if the exception is not a TransactionCanceledException or has no reasons.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    error_code = response.get("Error", {}).get("Code", "")
    exc_name = type(exc).__name__
    if exc_name != "TransactionCanceledException" and error_code != "TransactionCanceledException":
        return None
    reasons = response.get("CancellationReasons", [])
    return [r.get("Code", "None") for r in reasons]


def _is_condition_check_failure(exc: Exception) -> bool:
    """Check if an exception is a DynamoDB ConditionalCheckFailedException.

    For TransactionCanceledException, inspects CancellationReasons to distinguish
    ConditionalCheckFailed (returns True) from TransactionConflict (returns False).
    """
    exc_name = type(exc).__name__
    if exc_name == "ConditionalCheckFailedException":
        return True
    # Check CancellationReasons for TransactionCanceledException
    reason_codes = _get_cancellation_reason_codes(exc)
    if reason_codes is not None:
        return "ConditionalCheckFailed" in reason_codes
    # botocore ClientError fallback (non-transaction)
    if hasattr(exc, "response"):
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if error_code == "ConditionalCheckFailedException":
            return True
    return False


def _is_transaction_conflict(exc: Exception) -> bool:
    """Check if an exception is a DynamoDB TransactionConflict.

    TransactionConflict occurs when concurrent transactions touch the same items.
    Unlike ConditionalCheckFailed, this indicates transient contention that should
    be retried as-is (not via the consumption-only retry path).
    """
    reason_codes = _get_cancellation_reason_codes(exc)
    if reason_codes is not None:
        return "TransactionConflict" in reason_codes
    return False


def _build_retry_failure_statuses(entries: list[LeaseEntry]) -> list[LimitStatus]:
    """Build LimitStatus list for a retry failure (rate limit exceeded).

    Only declared entries are reported (Issue #455): undeclared entries are
    write-only carriers that never gate admission.
    """
    statuses: list[LimitStatus] = []
    for entry in entries:
        if not entry._declared:
            continue
        deficit_milli = max(0, entry.consumed * 1000 - entry.state.tokens_milli)
        retry_after = calculate_retry_after(
            deficit_milli=deficit_milli,
            refill_amount_milli=entry.limit.refill_amount * 1000,
            refill_period_ms=entry.limit.refill_period_seconds * 1000,
        )
        statuses.append(
            LimitStatus(
                entity_id=entry.entity_id,
                resource=entry.resource,
                limit_name=entry.limit.name,
                limit=entry.limit,
                available=entry.state.tokens_milli // 1000,
                requested=entry.consumed,
                exceeded=entry.consumed > 0,
                retry_after_seconds=retry_after,
            )
        )
    return statuses

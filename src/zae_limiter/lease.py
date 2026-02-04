"""Lease management for rate limit acquisitions."""

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .bucket import calculate_available, force_consume, try_consume
from .exceptions import RateLimitExceeded
from .models import BucketState, Limit, LimitStatus
from .schema import calculate_bucket_ttl_seconds

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
    # TTL configuration (Issue #271)
    bucket_ttl_refill_multiplier: int = 7

    @property
    def consumed(self) -> dict[str, int]:
        """Total consumed amounts by limit name."""
        result: dict[str, int] = {}
        for entry in self.entries:
            name = entry.limit.name
            result[name] = result.get(name, 0) + entry.consumed
        return result

    async def consume(self, **amounts: int) -> None:
        """
        Consume additional capacity from the buckets.

        Raises RateLimitExceeded if any bucket has insufficient capacity.

        Args:
            **amounts: Mapping of limit_name -> amount to consume
        """
        if self._committed or self._rolled_back:
            raise RuntimeError("Lease is no longer active")

        now_ms = int(time.time() * 1000)
        statuses: list[LimitStatus] = []
        updates: list[tuple[LeaseEntry, int, int]] = []  # (entry, new_tokens, new_refill)

        # Check all limits first
        for entry in self.entries:
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
        consumed_names = set(amounts.keys())
        for entry in self.entries:
            if entry.limit.name not in consumed_names:
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

        Args:
            **amounts: Mapping of limit_name -> delta (positive = consume more)
        """
        if self._committed or self._rolled_back:
            raise RuntimeError("Lease is no longer active")

        now_ms = int(time.time() * 1000)

        for entry in self.entries:
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

        Convenience wrapper for adjust() with negated values.

        Args:
            **amounts: Mapping of limit_name -> amount to return
        """
        negated = {k: -v for k, v in amounts.items()}
        await self.adjust(**negated)

    async def _commit(self) -> None:
        """Persist bucket state changes using ADD-based writes (ADR-115).

        Groups entries by (entity_id, resource) to build one composite
        update per group. Uses Normal write path first (ADD with refill,
        CONDITION rf=expected). On ConditionalCheckFailedException, falls
        back to Retry path (ADD consumption only, CONDITION tk>=consumed).
        """
        if self._committed or self._rolled_back:
            return

        self._committed = True

        now_ms = int(time.time() * 1000)
        repo = self.repository

        # Group entries by (entity_id, resource) for composite updates
        groups: dict[tuple[str, str], list[LeaseEntry]] = {}
        for entry in self.entries:
            key = (entry.entity_id, entry.resource)
            groups.setdefault(key, []).append(entry)

        # Build transaction items
        items: list[dict[str, Any]] = []
        for (entity_id, resource), group_entries in groups.items():
            is_new = group_entries[0]._is_new

            # Calculate TTL based on config source (Issue #271)
            # Entity-level config: remove TTL (ttl_seconds=0)
            # Default config (system/resource/override): set TTL
            has_custom_config = group_entries[0]._has_custom_config
            limits = [e.limit for e in group_entries]

            if has_custom_config:
                # Entity has custom limits - no TTL (persist indefinitely)
                ttl_seconds: int | None = 0  # 0 means REMOVE ttl
            elif self.bucket_ttl_refill_multiplier <= 0:
                # TTL disabled via multiplier
                ttl_seconds = None
            else:
                # Using defaults - calculate TTL based on time-to-fill (Issue #296)
                # TTL = max_time_to_fill × multiplier
                # where time_to_fill = (capacity / refill_amount) × refill_period
                ttl_seconds = calculate_bucket_ttl_seconds(
                    limits, self.bucket_ttl_refill_multiplier
                )

            if is_new:
                # Create path: PutItem with attribute_not_exists
                items.append(
                    repo.build_composite_create(
                        entity_id=entity_id,
                        resource=resource,
                        states=[e.state for e in group_entries],
                        now_ms=now_ms,
                        ttl_seconds=ttl_seconds if ttl_seconds != 0 else None,
                    )
                )
            else:
                # Normal path: ADD with refill+consumption, CONDITION rf=expected
                consumed: dict[str, int] = {}
                refill_amounts: dict[str, int] = {}
                expected_rf = group_entries[0]._original_rf_ms

                for entry in group_entries:
                    name = entry.limit.name
                    consumed[name] = entry.consumed * 1000  # to millitokens
                    # Refill = (final_tk + consumed_milli) - original_tk
                    # Because: final_tk = original_tk + refill - consumed
                    # So: refill = final_tk - original_tk + consumed
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
                    )
                )

        if not items:
            return

        try:
            await repo.transact_write(items)
        except Exception as exc:
            # Check if this is a ConditionalCheckFailedException (optimistic lock lost)
            if not _is_condition_check_failure(exc):
                raise

            # Retry path: ADD consumption only, CONDITION tk>=consumed per limit
            logger.debug("Normal write failed (optimistic lock), retrying consumption-only")
            retry_items: list[dict[str, Any]] = []
            for (entity_id, resource), group_entries in groups.items():
                if group_entries[0]._is_new:
                    # Create race: another writer created the item first.
                    # Retry as consumption-only (item now exists).
                    pass

                consumed = {}
                for entry in group_entries:
                    if entry.consumed > 0:
                        consumed[entry.limit.name] = entry.consumed * 1000

                if consumed:
                    retry_items.append(
                        repo.build_composite_retry(
                            entity_id=entity_id,
                            resource=resource,
                            consumed=consumed,
                        )
                    )

            if retry_items:
                try:
                    await repo.transact_write(retry_items)
                except Exception as retry_exc:
                    if _is_condition_check_failure(retry_exc):
                        # Retry also failed — insufficient tokens.
                        # Build statuses for the error from the local state.
                        statuses = _build_retry_failure_statuses(self.entries)
                        raise RateLimitExceeded(statuses) from retry_exc
                    raise

    async def _rollback(self) -> None:
        """Rollback is implicit - we just don't commit."""
        self._rolled_back = True


def _is_condition_check_failure(exc: Exception) -> bool:
    """Check if an exception is a DynamoDB ConditionalCheckFailedException."""
    exc_name = type(exc).__name__
    if exc_name in ("ConditionalCheckFailedException", "TransactionCanceledException"):
        return True
    # botocore wraps it in ClientError
    if hasattr(exc, "response"):
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if error_code in (
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        ):
            return True
    return False


def _build_retry_failure_statuses(entries: list[LeaseEntry]) -> list[LimitStatus]:
    """Build LimitStatus list for a retry failure (rate limit exceeded)."""
    statuses: list[LimitStatus] = []
    for entry in entries:
        statuses.append(
            LimitStatus(
                entity_id=entry.entity_id,
                resource=entry.resource,
                limit_name=entry.limit.name,
                limit=entry.limit,
                available=entry.state.tokens_milli // 1000,
                requested=entry.consumed,
                exceeded=entry.consumed > 0,
                retry_after_seconds=0.0,
            )
        )
    return statuses

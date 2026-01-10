"""Lease management for rate limit acquisitions."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .bucket import calculate_available, force_consume, try_consume
from .exceptions import RateLimitExceeded
from .models import BucketState, Limit, LimitStatus

if TYPE_CHECKING:
    from .repository import Repository


@dataclass
class LeaseEntry:
    """Tracks a single bucket within a lease."""

    entity_id: str
    resource: str
    limit: Limit
    state: BucketState
    consumed: int = 0  # total consumed during this lease


@dataclass
class Lease:
    """
    Manages an active rate limit acquisition.

    Tracks consumption across multiple entities/limits and handles
    rollback on exception.
    """

    repository: "Repository"
    entries: list[LeaseEntry] = field(default_factory=list)
    _committed: bool = False
    _rolled_back: bool = False

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
            entry.consumed += amounts.get(entry.limit.name, 0)

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
        """Persist the final bucket states to DynamoDB."""
        if self._committed or self._rolled_back:
            return

        self._committed = True

        # Build transaction items
        items: list[dict[str, Any]] = []
        for entry in self.entries:
            items.append(self.repository.build_bucket_put_item(entry.state))

        await self.repository.transact_write(items)

    async def _rollback(self) -> None:
        """Rollback is implicit - we just don't commit."""
        self._rolled_back = True


class SyncLease:
    """Synchronous wrapper for Lease."""

    def __init__(self, lease: Lease, loop: asyncio.AbstractEventLoop) -> None:
        self._lease = lease
        self._loop = loop

    @property
    def consumed(self) -> dict[str, int]:
        """Total consumed amounts by limit name."""
        return self._lease.consumed

    def consume(self, **amounts: int) -> None:
        """Consume additional capacity from the buckets."""
        self._loop.run_until_complete(self._lease.consume(**amounts))

    def adjust(self, **amounts: int) -> None:
        """Adjust consumption by delta."""
        self._loop.run_until_complete(self._lease.adjust(**amounts))

    def release(self, **amounts: int) -> None:
        """Return unused capacity to bucket."""
        self._loop.run_until_complete(self._lease.release(**amounts))

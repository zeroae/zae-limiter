"""
Token bucket algorithm implementation using integer arithmetic.

This module implements a variant of the classic token bucket algorithm
optimized for distributed systems:

- **Integer Arithmetic**: All values stored as millitokens (x1000) to avoid
  floating-point precision issues across distributed nodes.
- **Lazy Refill**: Tokens are calculated on-demand rather than continuously,
  with drift compensation to prevent accumulated rounding errors.
- **Negative Buckets**: Buckets can go into debt to support post-hoc
  reconciliation for operations with unknown cost.

Key functions:
    refill_bucket: Calculate refilled tokens with drift compensation
    try_consume: Attempt to consume tokens (atomic check-and-consume)
    force_consume: Force consume tokens (can go negative for debt)
    calculate_retry_after: Calculate wait time until tokens available

For conceptual explanation, see docs/guide/token-bucket.md
For implementation details, see docs/contributing/architecture.md
"""

from dataclasses import dataclass

from .models import BucketState, Limit, LimitStatus


@dataclass
class RefillResult:
    """Result of a bucket refill calculation."""

    new_tokens_milli: int
    new_last_refill_ms: int


@dataclass
class ConsumeResult:
    """Result of attempting to consume from a bucket."""

    success: bool
    new_tokens_milli: int
    new_last_refill_ms: int
    available: int  # tokens available before consume attempt
    retry_after_seconds: float  # 0 if success, time to wait if failed


def refill_bucket(
    tokens_milli: int,
    last_refill_ms: int,
    now_ms: int,
    burst_milli: int,
    refill_amount_milli: int,
    refill_period_ms: int,
) -> RefillResult:
    """
    Calculate refilled tokens using integer arithmetic.

    The refill is calculated as:
        tokens_to_add = elapsed_ms * refill_amount_milli / refill_period_ms

    We track how much time we "used" for the refill to avoid drift
    from accumulated rounding errors.

    Args:
        tokens_milli: Current tokens in millitokens
        last_refill_ms: Last refill timestamp in epoch milliseconds
        now_ms: Current timestamp in epoch milliseconds
        burst_milli: Maximum bucket capacity in millitokens
        refill_amount_milli: Refill amount numerator in millitokens
        refill_period_ms: Refill period denominator in milliseconds

    Returns:
        RefillResult with new token count and timestamp
    """
    elapsed_ms = now_ms - last_refill_ms

    if elapsed_ms <= 0:
        return RefillResult(tokens_milli, last_refill_ms)

    # Integer division for tokens to add
    tokens_to_add = (elapsed_ms * refill_amount_milli) // refill_period_ms

    if tokens_to_add == 0:
        # Not enough time has passed for even 1 millitoken
        return RefillResult(tokens_milli, last_refill_ms)

    # Track how much time we "consumed" for this refill to avoid drift
    # This is the inverse: time_used = tokens_added * period / amount
    time_used_ms = (tokens_to_add * refill_period_ms) // refill_amount_milli

    # Cap at burst and update timestamp
    new_tokens = min(burst_milli, tokens_milli + tokens_to_add)
    new_last_refill = last_refill_ms + time_used_ms

    return RefillResult(new_tokens, new_last_refill)


def try_consume(
    state: BucketState,
    requested: int,
    now_ms: int,
) -> ConsumeResult:
    """
    Attempt to consume tokens from a bucket.

    First refills the bucket based on elapsed time, then checks if
    there's enough capacity for the request.

    Args:
        state: Current bucket state
        requested: Number of tokens to consume
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        ConsumeResult indicating success/failure and new state
    """
    # First, refill the bucket
    refill = refill_bucket(
        tokens_milli=state.tokens_milli,
        last_refill_ms=state.last_refill_ms,
        now_ms=now_ms,
        burst_milli=state.burst_milli,
        refill_amount_milli=state.refill_amount_milli,
        refill_period_ms=state.refill_period_ms,
    )

    current_tokens_milli = refill.new_tokens_milli
    requested_milli = requested * 1000
    available = current_tokens_milli // 1000

    if current_tokens_milli >= requested_milli:
        # Success - consume the tokens
        return ConsumeResult(
            success=True,
            new_tokens_milli=current_tokens_milli - requested_milli,
            new_last_refill_ms=refill.new_last_refill_ms,
            available=available,
            retry_after_seconds=0.0,
        )
    else:
        # Failure - calculate retry time
        deficit_milli = requested_milli - current_tokens_milli
        retry_after = calculate_retry_after(
            deficit_milli=deficit_milli,
            refill_amount_milli=state.refill_amount_milli,
            refill_period_ms=state.refill_period_ms,
        )
        return ConsumeResult(
            success=False,
            new_tokens_milli=current_tokens_milli,
            new_last_refill_ms=refill.new_last_refill_ms,
            available=available,
            retry_after_seconds=retry_after,
        )


def calculate_retry_after(
    deficit_milli: int,
    refill_amount_milli: int,
    refill_period_ms: int,
) -> float:
    """
    Calculate seconds until deficit is refilled.

    Args:
        deficit_milli: How many millitokens we're short
        refill_amount_milli: Refill rate numerator
        refill_period_ms: Refill rate denominator

    Returns:
        Seconds until deficit is recovered (float)
    """
    if deficit_milli <= 0:
        return 0.0

    # time_ms = deficit * period / amount
    time_ms = (deficit_milli * refill_period_ms) // refill_amount_milli
    # Add 1ms to ensure we've fully refilled (rounding)
    return (time_ms + 1) / 1000.0


def calculate_available(
    state: BucketState,
    now_ms: int,
) -> int:
    """
    Calculate currently available tokens (can be negative).

    Args:
        state: Current bucket state
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        Available tokens (may be negative if bucket is in debt)
    """
    refill = refill_bucket(
        tokens_milli=state.tokens_milli,
        last_refill_ms=state.last_refill_ms,
        now_ms=now_ms,
        burst_milli=state.burst_milli,
        refill_amount_milli=state.refill_amount_milli,
        refill_period_ms=state.refill_period_ms,
    )
    return refill.new_tokens_milli // 1000


def calculate_time_until_available(
    state: BucketState,
    needed: int,
    now_ms: int,
) -> float:
    """
    Calculate seconds until `needed` tokens are available.

    Args:
        state: Current bucket state
        needed: Number of tokens needed
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        Seconds until available (0.0 if already available)
    """
    refill = refill_bucket(
        tokens_milli=state.tokens_milli,
        last_refill_ms=state.last_refill_ms,
        now_ms=now_ms,
        burst_milli=state.burst_milli,
        refill_amount_milli=state.refill_amount_milli,
        refill_period_ms=state.refill_period_ms,
    )

    needed_milli = needed * 1000
    if refill.new_tokens_milli >= needed_milli:
        return 0.0

    deficit_milli = needed_milli - refill.new_tokens_milli
    return calculate_retry_after(
        deficit_milli=deficit_milli,
        refill_amount_milli=state.refill_amount_milli,
        refill_period_ms=state.refill_period_ms,
    )


def force_consume(
    state: BucketState,
    amount: int,
    now_ms: int,
) -> tuple[int, int]:
    """
    Force consume tokens without checking limits (can go negative).

    Used for post-hoc adjustments like LLM token reconciliation.

    Args:
        state: Current bucket state
        amount: Amount to consume (positive) or return (negative)
        now_ms: Current timestamp in epoch milliseconds

    Returns:
        Tuple of (new_tokens_milli, new_last_refill_ms)
    """
    # First refill
    refill = refill_bucket(
        tokens_milli=state.tokens_milli,
        last_refill_ms=state.last_refill_ms,
        now_ms=now_ms,
        burst_milli=state.burst_milli,
        refill_amount_milli=state.refill_amount_milli,
        refill_period_ms=state.refill_period_ms,
    )

    # Consume (can go negative)
    new_tokens_milli = refill.new_tokens_milli - (amount * 1000)

    return new_tokens_milli, refill.new_last_refill_ms


def build_limit_status(
    entity_id: str,
    resource: str,
    limit: Limit,
    state: BucketState,
    requested: int,
    now_ms: int,
) -> LimitStatus:
    """
    Build a LimitStatus for a bucket check.

    Args:
        entity_id: Entity being checked
        resource: Resource being accessed
        limit: Limit configuration
        state: Current bucket state
        requested: Amount requested
        now_ms: Current timestamp

    Returns:
        LimitStatus with full details
    """
    result = try_consume(state, requested, now_ms)

    return LimitStatus(
        entity_id=entity_id,
        resource=resource,
        limit_name=limit.name,
        limit=limit,
        available=result.available,
        requested=requested,
        exceeded=not result.success,
        retry_after_seconds=result.retry_after_seconds,
    )

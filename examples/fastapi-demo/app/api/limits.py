"""Rate limit configuration endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from zae_limiter import Limit, RateLimiter

from ..dependencies import get_limiter
from ..models import LimitConfig, LimitsResponse, SetLimitsRequest

router = APIRouter()


@router.get("/{entity_id}", response_model=LimitsResponse)
async def get_limits(
    entity_id: str,
    resource: str = "gpt-4",
    limiter: RateLimiter = Depends(get_limiter),
) -> LimitsResponse:
    """Get stored limits for an entity."""
    entity = await limiter.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

    # Get limits from the entity
    limits = await limiter.get_limits(entity_id=entity_id, resource=resource)

    return LimitsResponse(
        entity_id=entity_id,
        resource=resource,
        limits=[
            LimitConfig(
                name=limit.name,
                capacity=limit.capacity,
                refill_rate=limit.refill_amount / limit.refill_period_seconds,
                burst=limit.burst if limit.burst != limit.capacity else None,
            )
            for limit in limits
        ],
    )


@router.put("/{entity_id}", response_model=LimitsResponse)
async def set_limits(
    entity_id: str,
    request: SetLimitsRequest,
    limiter: RateLimiter = Depends(get_limiter),
) -> LimitsResponse:
    """Set or update limits for an entity."""
    entity = await limiter.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

    # Convert to Limit objects
    limits = []
    for lc in request.limits:
        # Calculate refill parameters from rate
        # rate = refill_amount / refill_period_seconds
        # For simplicity, use per-minute limits
        refill_period = 60  # 1 minute
        refill_amount = int(lc.refill_rate * refill_period)

        limit = Limit(
            name=lc.name,
            capacity=lc.capacity,
            refill_amount=refill_amount,
            refill_period_seconds=refill_period,
            burst=lc.burst or lc.capacity,
        )
        limits.append(limit)

    await limiter.set_limits(
        entity_id=entity_id,
        resource=request.resource,
        limits=limits,
    )

    return LimitsResponse(
        entity_id=entity_id,
        resource=request.resource,
        limits=request.limits,
    )


@router.delete("/{entity_id}")
async def clear_limits(
    entity_id: str,
    resource: str = "gpt-4",
    limiter: RateLimiter = Depends(get_limiter),
) -> dict[str, str]:
    """Clear stored limits for an entity (reverts to defaults)."""
    await limiter.clear_limits(entity_id=entity_id, resource=resource)
    return {"status": "cleared", "entity_id": entity_id, "resource": resource}

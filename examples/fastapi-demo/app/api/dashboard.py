"""Dashboard data endpoints."""

from fastapi import APIRouter, Depends

from zae_limiter import Limit, RateLimiter

from ..dependencies import get_limiter
from ..models import AvailabilityInfo, DashboardResponse, EntityResponse, EntityStatus

router = APIRouter()

# Default limits for availability checks
DEFAULT_LIMITS = [
    Limit.per_minute("rpm", 60),
    Limit.per_minute("tpm", 100_000),
]

# Known demo entities (in production, you'd query this from DB)
DEMO_ENTITIES = ["proj-demo", "key-alice", "key-bob", "key-charlie"]


@router.get("/entities", response_model=DashboardResponse)
async def list_entities(
    limiter: RateLimiter = Depends(get_limiter),
) -> DashboardResponse:
    """
    List all demo entities with their current rate limit status.

    Note: This is a simplified implementation for the demo.
    In production, you'd want pagination and efficient querying.
    """
    results: list[EntityStatus] = []

    for entity_id in DEMO_ENTITIES:
        entity = await limiter.get_entity(entity_id)
        if not entity:
            continue

        # Get current availability
        try:
            available = await limiter.available(
                entity_id=entity_id,
                resource="gpt-4",
                limits=DEFAULT_LIMITS,
                use_stored_limits=True,
            )

            # Get stored limits or use defaults
            stored_limits = await limiter.get_limits(entity_id=entity_id, resource="gpt-4")
            limit_capacities = {lim.name: lim.capacity for lim in stored_limits}

            # Build availability info
            limits_info: dict[str, AvailabilityInfo] = {}
            for name, avail in available.items():
                capacity = limit_capacities.get(name, 0)
                # Use default capacity if not found
                if capacity == 0:
                    for default_limit in DEFAULT_LIMITS:
                        if default_limit.name == name:
                            capacity = default_limit.capacity
                            break

                utilization = ((capacity - avail) / capacity * 100) if capacity > 0 else 0
                limits_info[name] = AvailabilityInfo(
                    available=avail,
                    capacity=capacity,
                    utilization_pct=round(utilization, 1),
                )

            results.append(
                EntityStatus(
                    entity=EntityResponse(
                        id=entity.id,
                        name=entity.name,
                        parent_id=entity.parent_id,
                        metadata=entity.metadata or {},
                        created_at=entity.created_at,
                    ),
                    limits=limits_info,
                )
            )
        except Exception:
            # Skip entities with errors
            continue

    return DashboardResponse(entities=results)


@router.get("/availability/{entity_id}")
async def get_availability(
    entity_id: str,
    resource: str = "gpt-4",
    limiter: RateLimiter = Depends(get_limiter),
) -> dict[str, int]:
    """Get current token availability for an entity."""
    available = await limiter.available(
        entity_id=entity_id,
        resource=resource,
        limits=DEFAULT_LIMITS,
        use_stored_limits=True,
    )
    return available


@router.get("/wait-time/{entity_id}")
async def get_wait_time(
    entity_id: str,
    rpm: int = 1,
    tpm: int = 100,
    resource: str = "gpt-4",
    limiter: RateLimiter = Depends(get_limiter),
) -> dict[str, float]:
    """Get time until tokens are available for a request."""
    wait_time = await limiter.time_until_available(
        entity_id=entity_id,
        resource=resource,
        limits=DEFAULT_LIMITS,
        needed={"rpm": rpm, "tpm": tpm},
        use_stored_limits=True,
    )
    return {"wait_seconds": wait_time}

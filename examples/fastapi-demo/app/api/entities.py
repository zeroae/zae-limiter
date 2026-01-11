"""Entity management endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from zae_limiter import EntityExistsError, RateLimiter

from ..dependencies import get_limiter
from ..models import CreateEntityRequest, EntityResponse

router = APIRouter()


@router.post("/", response_model=EntityResponse, status_code=201)
async def create_entity(
    request: CreateEntityRequest,
    limiter: RateLimiter = Depends(get_limiter),
) -> EntityResponse:
    """Create a new entity (project or API key)."""
    try:
        entity = await limiter.create_entity(
            entity_id=request.entity_id,
            name=request.name,
            parent_id=request.parent_id,
            metadata=request.metadata or {},
        )
        return EntityResponse(
            id=entity.id,
            name=entity.name,
            parent_id=entity.parent_id,
            metadata=entity.metadata or {},
            created_at=entity.created_at,
        )
    except EntityExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/{entity_id}", response_model=EntityResponse)
async def get_entity(
    entity_id: str,
    limiter: RateLimiter = Depends(get_limiter),
) -> EntityResponse:
    """Get entity details."""
    entity = await limiter.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")
    return EntityResponse(
        id=entity.id,
        name=entity.name,
        parent_id=entity.parent_id,
        metadata=entity.metadata or {},
        created_at=entity.created_at,
    )


@router.get("/{parent_id}/children", response_model=list[EntityResponse])
async def get_children(
    parent_id: str,
    limiter: RateLimiter = Depends(get_limiter),
) -> list[EntityResponse]:
    """Get all children of a parent entity."""
    children = await limiter.get_children(parent_id)
    return [
        EntityResponse(
            id=e.id,
            name=e.name,
            parent_id=e.parent_id,
            metadata=e.metadata or {},
            created_at=e.created_at,
        )
        for e in children
    ]


@router.delete("/{entity_id}")
async def delete_entity(
    entity_id: str,
    limiter: RateLimiter = Depends(get_limiter),
) -> dict[str, str]:
    """Delete an entity and all related data."""
    await limiter.delete_entity(entity_id)
    return {"status": "deleted", "entity_id": entity_id}

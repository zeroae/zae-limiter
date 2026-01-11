"""Pydantic models for request/response schemas."""

from pydantic import BaseModel, Field


# Chat Completions Models (OpenAI-compatible)
class ChatMessage(BaseModel):
    """A chat message."""

    role: str = Field(..., description="The role of the message author")
    content: str = Field(..., description="The content of the message")


class ChatRequest(BaseModel):
    """Request body for chat completions."""

    model: str = Field(default="gpt-4", description="Model to use")
    messages: list[ChatMessage] = Field(..., description="List of messages")
    max_tokens: int | None = Field(default=None, description="Maximum tokens to generate")
    temperature: float = Field(default=1.0, description="Sampling temperature")


class Usage(BaseModel):
    """Token usage information."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatChoice(BaseModel):
    """A chat completion choice."""

    index: int
    message: ChatMessage
    finish_reason: str


class ChatResponse(BaseModel):
    """Response from chat completions."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: Usage


# Entity Models
class CreateEntityRequest(BaseModel):
    """Request to create an entity."""

    entity_id: str = Field(..., description="Unique entity identifier")
    name: str | None = Field(default=None, description="Human-readable name")
    parent_id: str | None = Field(default=None, description="Parent entity ID")
    metadata: dict[str, str] | None = Field(default=None, description="Custom metadata")


class EntityResponse(BaseModel):
    """Entity information response."""

    id: str
    name: str | None
    parent_id: str | None
    metadata: dict[str, str]
    created_at: str | None = None


# Limits Models
class LimitConfig(BaseModel):
    """Rate limit configuration."""

    name: str = Field(..., description="Limit name (e.g., rpm, tpm)")
    capacity: int = Field(..., description="Maximum tokens/requests")
    refill_rate: float = Field(..., description="Tokens per second")
    burst: int | None = Field(default=None, description="Burst allowance")


class SetLimitsRequest(BaseModel):
    """Request to set limits for an entity."""

    resource: str = Field(default="gpt-4", description="Resource name")
    limits: list[LimitConfig] = Field(..., description="List of limits")


class LimitsResponse(BaseModel):
    """Response containing entity limits."""

    entity_id: str
    resource: str
    limits: list[LimitConfig]


# Dashboard Models
class AvailabilityInfo(BaseModel):
    """Current availability for a limit."""

    available: int
    capacity: int
    utilization_pct: float


class EntityStatus(BaseModel):
    """Entity status with current availability."""

    entity: EntityResponse
    limits: dict[str, AvailabilityInfo]


class DashboardResponse(BaseModel):
    """Dashboard data response."""

    entities: list[EntityStatus]


# Error Models
class RateLimitError(BaseModel):
    """Rate limit exceeded error response."""

    error: str = "rate_limit_exceeded"
    message: str
    retry_after_seconds: float
    violations: list[dict]
    passed: list[dict]

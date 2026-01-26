"""Chat completions endpoint simulating LLM API."""

import time
import uuid

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from zae_limiter import Limit, RateLimiter, RateLimitExceeded

from ..dependencies import get_limiter
from ..models import ChatChoice, ChatMessage, ChatRequest, ChatResponse, Usage
from ..services.llm_simulator import simulate_completion

router = APIRouter()

# Default limits (can be overridden per-entity via stored limits)
# Balanced: 100 tokens/request threshold (< 100: hit RPM, > 100: hit TPM)
DEFAULT_LIMITS = [
    Limit.per_minute("rpm", 20),  # 20 requests per minute
    Limit.per_minute("tpm", 2_000),  # 2k tokens per minute
]


def estimate_tokens(messages: list[ChatMessage]) -> int:
    """Estimate input tokens from messages."""
    # Simple estimation: ~4 chars per token
    total_chars = sum(len(m.content) for m in messages)
    return max(10, total_chars // 4) + 50  # +50 for completion buffer


@router.post("/chat/completions", response_model=ChatResponse)
async def chat_completions(
    request: ChatRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
    limiter: RateLimiter = Depends(get_limiter),
) -> ChatResponse | JSONResponse:
    """
    OpenAI-compatible chat completions endpoint.

    Demonstrates:
    - Token estimation before call
    - Rate limit check with multiple limits (rpm + tpm)
    - Cascade mode for hierarchical limits
    - Token reconciliation after completion
    - Proper 429 response handling

    Headers:
        X-API-Key: API key for rate limiting (e.g., key-alice, key-bob, key-charlie)
    """
    # Estimate tokens for rate limiting
    estimated_input = estimate_tokens(request.messages)
    estimated_output = 100  # Conservative estimate
    estimated_total = estimated_input + estimated_output

    try:
        async with limiter.acquire(
            entity_id=x_api_key,
            resource=request.model,
            limits=DEFAULT_LIMITS,
            consume={"rpm": 1, "tpm": estimated_total},
            use_stored_limits=True,  # Use entity-specific limits if set
        ) as lease:
            # Simulate LLM completion
            content, prompt_tokens, completion_tokens = await simulate_completion(
                messages=[m.model_dump() for m in request.messages],
                model=request.model,
                max_tokens=request.max_tokens,
            )
            actual_tokens = prompt_tokens + completion_tokens

            # Reconcile actual token usage
            token_delta = actual_tokens - estimated_total
            await lease.adjust(tpm=token_delta)

            # Build response
            return ChatResponse(
                id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
                created=int(time.time()),
                model=request.model,
                choices=[
                    ChatChoice(
                        index=0,
                        message=ChatMessage(role="assistant", content=content),
                        finish_reason="stop",
                    )
                ],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=actual_tokens,
                ),
            )

    except RateLimitExceeded as e:
        # Return proper 429 response
        return JSONResponse(
            status_code=429,
            content=e.as_dict(),
            headers={"Retry-After": e.retry_after_header},
        )

# LLM Integration

LLM APIs are a natural fit for zae-limiter's estimate-then-reconcile pattern, especially when token counts are unknown until after the call completes. This guide covers patterns for integrating with LLM providers.

!!! tip "Understanding the Algorithm"
    This guide assumes familiarity with how rate limiting works. If you're new to token buckets, start with [Token Bucket Algorithm](token-bucket.md) to understand concepts like negative buckets (debt) that enable the estimate-then-reconcile pattern.

## The Challenge

LLM APIs present unique rate limiting challenges:

1. **Token counts are unknown upfront** - You don't know how many tokens a response will use
2. **Multiple limits** - Providers often limit both requests and tokens
3. **Variable costs** - Different models have different token limits
4. **Streaming responses** - Token count only known after stream completes

## Basic Pattern: Estimate and Reconcile

```{.python .lint-only}
async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    limits=[
        Limit.per_minute("rpm", 100),       # Requests per minute
        Limit.per_minute("tpm", 10_000),    # Tokens per minute
    ],
    consume={"rpm": 1, "tpm": 500},  # Estimate 500 tokens
) as lease:
    response = await openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
    )

    # Reconcile with actual usage
    actual_tokens = response.usage.total_tokens
    await lease.adjust(tpm=actual_tokens - 500)
```

## Estimation Strategies

### Fixed Estimate

Simple but may over/under-estimate:

```python
consume={"tpm": 500}  # Always estimate 500 tokens
```

### Input-Based Estimate

Estimate based on input length:

```{.python .lint-only}
import tiktoken

def estimate_tokens(messages: list, model: str = "gpt-4") -> int:
    """Estimate tokens for input messages."""
    encoding = tiktoken.encoding_for_model(model)
    total = 0
    for msg in messages:
        total += len(encoding.encode(msg["content"]))
        total += 4  # Message overhead
    total += 2  # Completion priming
    return total

# Use in rate limiting
input_tokens = estimate_tokens(messages)
estimated_output = 500  # Rough estimate for output
total_estimate = input_tokens + estimated_output

async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    consume={"tpm": total_estimate},
) as lease:
    response = await call_llm()
    actual = response.usage.total_tokens
    await lease.adjust(tpm=actual - total_estimate)
```

### Max Tokens Estimate

Use max_tokens as upper bound:

```{.python .lint-only}
max_tokens = 1000

async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    consume={"tpm": input_tokens + max_tokens},
) as lease:
    response = await openai.chat.completions.create(
        model="gpt-4",
        messages=messages,
        max_tokens=max_tokens,
    )
    actual = response.usage.total_tokens
    await lease.adjust(tpm=actual - (input_tokens + max_tokens))
```

## Handling Streaming Responses

For streaming responses, token count is only available after the stream completes:

```{.python .lint-only}
async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    limits=[
        Limit.per_minute("rpm", 100),
        Limit.per_minute("tpm", 10_000),
    ],
    consume={"rpm": 1, "tpm": estimated_tokens},
) as lease:
    chunks = []
    async for chunk in await openai.chat.completions.create(
        model="gpt-4",
        messages=messages,
        stream=True,
    ):
        chunks.append(chunk)
        yield chunk  # Stream to client

    # Get final usage from last chunk (OpenAI includes it)
    if chunks[-1].usage:
        actual = chunks[-1].usage.total_tokens
        await lease.adjust(tpm=actual - estimated_tokens)
```

## Per-Model Rate Limits

Different models have different limits. Use the `resource` parameter:

```{.python .lint-only}
MODEL_LIMITS = {
    "gpt-4": [
        Limit.per_minute("rpm", 100),
        Limit.per_minute("tpm", 10_000),
    ],
    "gpt-4-turbo": [
        Limit.per_minute("rpm", 500),
        Limit.per_minute("tpm", 150_000),
    ],
    "gpt-3.5-turbo": [
        Limit.per_minute("rpm", 3500),
        Limit.per_minute("tpm", 90_000),
    ],
}

async def rate_limited_completion(
    entity_id: str,
    model: str,
    messages: list,
    estimated_tokens: int,
):
    limits = MODEL_LIMITS.get(model, MODEL_LIMITS["gpt-3.5-turbo"])

    async with limiter.acquire(
        entity_id=entity_id,
        resource=model,  # Different bucket per model
        limits=limits,
        consume={"rpm": 1, "tpm": estimated_tokens},
    ) as lease:
        response = await openai.chat.completions.create(
            model=model,
            messages=messages,
        )
        actual = response.usage.total_tokens
        await lease.adjust(tpm=actual - estimated_tokens)
        return response
```

## Negative Buckets (Debt)

zae-limiter allows buckets to go negative, which is useful when actual usage exceeds estimates:

```{.python .lint-only}
# Estimate: 500 tokens
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    consume={"tpm": 500},
) as lease:
    response = await call_llm()

    # Actual: 2000 tokens
    # Adjustment: 2000 - 500 = 1500
    await lease.adjust(tpm=1500)
    # Bucket now at -1500 tokens (in debt)
```

The debt is repaid as tokens refill over time. This ensures accurate accounting while allowing requests to complete.

## Pre-Flight Capacity Check

Check capacity before making expensive calls:

```python
async def call_with_capacity_check(
    entity_id: str,
    model: str,
    messages: list,
    estimated_tokens: int,
):
    limits = MODEL_LIMITS[model]

    # Check available capacity
    available = await limiter.available(
        entity_id=entity_id,
        resource=model,
        limits=limits,
    )

    if available["tpm"] < estimated_tokens:
        # Not enough capacity - check when it will be available
        wait_time = await limiter.time_until_available(
            entity_id=entity_id,
            resource=model,
            limits=limits,
            needed={"tpm": estimated_tokens},
        )
        raise RetryAfter(seconds=wait_time)

    # Proceed with rate-limited call
    async with limiter.acquire(...):
        ...
```

## Integration with Retry Libraries

Combine with retry libraries like `tenacity`:

```{.python .requires-external}
from tenacity import retry, retry_if_exception_type, wait_fixed

@retry(
    retry=retry_if_exception_type(RateLimitExceeded),
    wait=wait_fixed(1),
)
async def resilient_llm_call(entity_id: str, messages: list):
    async with limiter.acquire(
        entity_id=entity_id,
        resource="gpt-4",
        limits=[Limit.per_minute("rpm", 100)],
        consume={"rpm": 1},
    ):
        return await openai.chat.completions.create(
            model="gpt-4",
            messages=messages,
        )
```

Or use the retry information from the exception:

```python
async def smart_retry_llm_call(entity_id: str, messages: list):
    while True:
        try:
            async with limiter.acquire(...):
                return await call_llm()
        except RateLimitExceeded as e:
            await asyncio.sleep(e.retry_after_seconds)
```

## Next Steps

- [Unavailability Handling](unavailability.md) - Handling service outages
- [API Reference](../api/limiter.md) - Complete API documentation

"""LLM completion simulator."""

import asyncio
import random

RESPONSES = [
    "I'd be happy to help you with that question. Let me provide a detailed explanation.",
    "That's an interesting topic. Here's what I know about it.",
    "Based on my knowledge, I can offer the following insights.",
    "Let me break this down for you step by step.",
    "Great question! Here's a comprehensive answer.",
    "I understand your query. Here's my response based on the context provided.",
    "Thank you for asking. Let me address your question thoroughly.",
    "This is a nuanced topic that deserves careful consideration.",
]

# Padding phrases for generating longer responses
PADDING_PHRASES = [
    "This additional context helps provide a more complete answer.",
    "Furthermore, it's important to consider the broader implications.",
    "Additionally, there are several related aspects worth exploring.",
    "To elaborate further on this point, let me add some context.",
    "It's also worth noting that this topic connects to many areas.",
    "From a practical standpoint, there are key considerations.",
    "The underlying principles here are quite fundamental.",
    "This perspective helps illustrate the overall concept.",
    "Taking a step back, we can see the bigger picture emerge.",
    "In practice, these ideas manifest in various ways.",
]


async def simulate_completion(
    messages: list[dict],
    model: str,
    max_tokens: int | None = None,
) -> tuple[str, int, int]:
    """
    Simulate an LLM completion with realistic token usage.

    Args:
        messages: List of chat messages
        model: Model name (affects simulated latency)
        max_tokens: Maximum tokens to generate (default: 100, max: 4000)

    Returns:
        Tuple of (response_content, prompt_tokens, completion_tokens)
    """
    # Default and cap max_tokens
    if max_tokens is None:
        max_tokens = 100
    max_tokens = min(max(1, max_tokens), 4000)

    # Simulate API latency (scales with token count)
    base_latency = 0.05 if "gpt-4" not in model else 0.1
    token_latency = max_tokens * 0.0001  # ~0.1ms per token
    await asyncio.sleep(base_latency + token_latency + random.uniform(0, 0.05))

    # Calculate input tokens (rough estimate: ~4 chars per token)
    input_chars = sum(len(m.get("content", "")) for m in messages)
    prompt_tokens = max(10, input_chars // 4)

    # Generate response to match max_tokens
    # Target: ~2 tokens per word, so we need max_tokens // 2 words
    target_words = max_tokens // 2
    words_generated = 0
    response_parts = [random.choice(RESPONSES)]
    words_generated += len(response_parts[0].split())

    # Add padding phrases until we reach target word count
    while words_generated < target_words:
        phrase = random.choice(PADDING_PHRASES)
        response_parts.append(phrase)
        words_generated += len(phrase.split())

    response = " ".join(response_parts)

    # Trim if we overshot
    words = response.split()
    if len(words) > target_words:
        response = " ".join(words[:target_words])

    # Calculate completion tokens (match the requested max_tokens with some variance)
    completion_tokens = int(max_tokens * random.uniform(0.9, 1.0))
    completion_tokens = max(10, completion_tokens)

    return response, prompt_tokens, completion_tokens

#!/usr/bin/env python3
"""Seed demo data for the FastAPI example."""

import asyncio
import os

from zae_limiter import EntityExistsError, Limit, RateLimiter


async def seed_data() -> None:
    """Create demo entities and limits."""
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
    table_name = os.environ.get("TABLE_NAME", "rate_limits")

    print(f"Connecting to {endpoint_url}, table: {table_name}")

    limiter = RateLimiter(
        table_name=table_name,
        endpoint_url=endpoint_url,
        skip_version_check=True,
    )

    # Create project (parent entity)
    try:
        await limiter.create_entity(
            entity_id="proj-demo",
            name="Demo Project",
            metadata={"tier": "enterprise"},
        )
        print("  Created project: proj-demo")
    except EntityExistsError:
        print("  Project proj-demo already exists")

    # Create API keys (child entities)
    api_keys = [
        ("key-alice", "Alice's API Key"),
        ("key-bob", "Bob's API Key"),
        ("key-charlie", "Charlie's API Key"),
    ]

    for key_id, name in api_keys:
        try:
            await limiter.create_entity(
                entity_id=key_id,
                name=name,
                parent_id="proj-demo",
            )
            print(f"  Created API key: {key_id}")
        except EntityExistsError:
            print(f"  API key {key_id} already exists")

    # Set project-level limits (shared across all keys)
    await limiter.set_limits(
        entity_id="proj-demo",
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 200),  # 200 rpm for entire project
            Limit.per_minute("tpm", 500_000),  # 500k tpm for entire project
        ],
    )
    print("  Set project limits: 200 rpm, 500k tpm")

    # Set individual key limits - Alice (premium)
    await limiter.set_limits(
        entity_id="key-alice",
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 100),  # Alice: 100 rpm
            Limit.per_minute("tpm", 200_000),  # Alice: 200k tpm
        ],
    )
    print("  Set Alice limits: 100 rpm, 200k tpm")

    # Set individual key limits - Bob (standard)
    await limiter.set_limits(
        entity_id="key-bob",
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 50),  # Bob: 50 rpm (lower tier)
            Limit.per_minute("tpm", 50_000),  # Bob: 50k tpm
        ],
    )
    print("  Set Bob limits: 50 rpm, 50k tpm")

    # Charlie uses default limits (no stored limits)
    print("  Charlie uses default limits (60 rpm, 100k tpm)")

    await limiter.close()
    print("\nDemo data seeded successfully!")


if __name__ == "__main__":
    asyncio.run(seed_data())

#!/usr/bin/env python3
"""
Hierarchical Rate Limits Example

Demonstrates parent/child entity relationships and cascade mode, useful for:
- Project-level limits shared across API keys
- Organization → Team → User hierarchies
- Tier-based rate limiting (premium vs standard)

Key patterns:
- Creating parent entities (projects) and children (API keys)
- Cascade mode: consuming from both child and parent
- Parent limits blocking child requests
- Stored limits for different tiers (premium vs standard)
- Querying entity relationships

Setup:
    # Start LocalStack (from project root)
    docker compose up -d
    uv run python examples/hierarchical_limits.py
"""

import asyncio

from zae_limiter import (
    EntityExistsError,
    Limit,
    RateLimiter,
    RateLimitExceeded,
    StackOptions,
)

# Configuration
ENDPOINT_URL = "http://localhost:4566"
NAME = "hierarchical"  # Creates ZAEL-hierarchical resources


async def main() -> None:
    """Demonstrate hierarchical rate limiting."""
    print("=== Hierarchical Rate Limits Example ===\n")

    limiter = RateLimiter(
        name=NAME,  # Creates ZAEL-hierarchical resources
        endpoint_url=ENDPOINT_URL,
        stack_options=StackOptions(),
    )

    # -------------------------------------------------------------------------
    # Step 1: Create entity hierarchy
    # -------------------------------------------------------------------------
    print("Step 1: Creating entity hierarchy\n")

    # Create parent entity (project)
    try:
        project = await limiter.create_entity(
            entity_id="proj-acme",
            name="ACME Corp Production",
            metadata={"tier": "enterprise"},
        )
        print(f"Created project: {project.id} ({project.name})")
    except EntityExistsError:
        print("Project already exists, continuing...")

    # Create child entities (API keys)
    for key_id, key_name in [
        ("key-alice", "Alice's Key"),
        ("key-bob", "Bob's Key"),
    ]:
        try:
            key = await limiter.create_entity(
                entity_id=key_id,
                name=key_name,
                parent_id="proj-acme",  # Link to parent
                cascade=True,  # Enable cascade to parent
            )
            print(f"Created API key: {key.id} (parent: {key.parent_id})")
        except EntityExistsError:
            print(f"API key {key_id} already exists, continuing...")

    # Query the hierarchy
    children = await limiter.get_children("proj-acme")
    print(f"\nProject has {len(children)} API keys: {[c.id for c in children]}")

    # -------------------------------------------------------------------------
    # Step 2: Cascade mode - consume from both child and parent
    # -------------------------------------------------------------------------
    print("\n" + "=" * 50)
    print("Step 2: Cascade mode\n")

    # Default limits for all entities
    default_limits = [Limit.per_minute("rpm", 100)]

    # Make requests - cascade is enabled on the entity, so
    # this consumes from BOTH the API key AND the project
    for i in range(3):
        try:
            async with limiter.acquire(
                entity_id="key-alice",
                resource="api",
                limits=default_limits,
                consume={"rpm": 1},
            ) as lease:
                print(f"Request {i + 1} from key-alice: consumed {lease.consumed}")
        except RateLimitExceeded as e:
            print(f"Request {i + 1}: Rate limited by {[v.entity_id for v in e.violations]}")

    # Check capacity on both entities
    key_available = await limiter.available("key-alice", "api", default_limits)
    proj_available = await limiter.available("proj-acme", "api", default_limits)
    print("\nAfter 3 cascaded requests:")
    print(f"  key-alice available: {key_available}")
    print(f"  proj-acme available: {proj_available}")

    # -------------------------------------------------------------------------
    # Step 3: Parent limits block child
    # -------------------------------------------------------------------------
    print("\n" + "=" * 50)
    print("Step 3: Parent limits blocking child\n")

    # Set a low limit on the project (shared across all keys)
    await limiter.set_limits(
        entity_id="proj-acme",
        limits=[Limit.per_minute("rpm", 5)],  # Only 5 rpm for entire project!
    )
    print("Set project limit: 5 rpm (shared across all keys)")

    # Try to make requests - project limit will block
    for i in range(8):
        try:
            async with limiter.acquire(
                entity_id="key-bob",  # Different key, same project
                resource="api",
                limits=[Limit.per_minute("rpm", 100)],  # Key's own limit
                consume={"rpm": 1},
                use_stored_limits=True,  # Use project's stored limits
            ) as lease:
                print(f"Request {i + 1} from key-bob: OK")
        except RateLimitExceeded as e:
            blocked_by = [v.entity_id for v in e.violations]
            print(f"Request {i + 1} from key-bob: BLOCKED by {blocked_by}")

    # -------------------------------------------------------------------------
    # Step 4: Tier-based limits (premium vs standard)
    # -------------------------------------------------------------------------
    print("\n" + "=" * 50)
    print("Step 4: Tier-based limits\n")

    # Create entities for different tiers
    try:
        await limiter.create_entity("user-premium", name="Premium User")
        await limiter.create_entity("user-standard", name="Standard User")
    except EntityExistsError:
        pass

    # Set different limits per tier
    await limiter.set_limits(
        entity_id="user-premium",
        limits=[
            Limit.per_minute("rpm", 1000),
            Limit.per_minute("tpm", 100_000, burst=150_000),  # Higher burst!
        ],
    )
    await limiter.set_limits(
        entity_id="user-standard",
        limits=[
            Limit.per_minute("rpm", 10),
            Limit.per_minute("tpm", 1000),
        ],
    )

    print("Set limits:")
    print("  Premium: 1000 rpm, 100k tpm (150k burst)")
    print("  Standard: 10 rpm, 1k tpm")

    # Default limits (fallback for users without stored limits)
    default_api_limits = [
        Limit.per_minute("rpm", 5),
        Limit.per_minute("tpm", 500),
    ]

    # Make requests using stored limits
    for user_id in ["user-premium", "user-standard"]:
        available = await limiter.available(
            entity_id=user_id,
            resource="api",
            limits=default_api_limits,  # Fallback (not used if stored limits exist)
            use_stored_limits=True,  # Use stored limits if available
        )
        print(f"\n{user_id} available: {available}")

    await limiter.close()
    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())

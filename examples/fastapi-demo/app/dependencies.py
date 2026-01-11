"""FastAPI dependencies for rate limiter."""

from zae_limiter import RateLimiter

from .config import settings

# Global rate limiter instance
_limiter: RateLimiter | None = None


async def get_limiter() -> RateLimiter:
    """Get or create the rate limiter instance."""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(
            table_name=settings.table_name,
            endpoint_url=settings.aws_endpoint_url,
            skip_version_check=True,
        )
    return _limiter


async def close_limiter() -> None:
    """Close the rate limiter connection."""
    global _limiter
    if _limiter is not None:
        await _limiter.close()
        _limiter = None

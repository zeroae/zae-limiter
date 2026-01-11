"""FastAPI Demo for zae-limiter."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import chat, dashboard, entities, limits
from .dependencies import close_limiter, get_limiter


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan."""
    # Startup: Initialize rate limiter
    await get_limiter()
    yield
    # Shutdown: Close connections
    await close_limiter()


app = FastAPI(
    title="zae-limiter Demo API",
    description="Simulated LLM API with rate limiting powered by zae-limiter",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(chat.router, prefix="/v1", tags=["Chat"])
app.include_router(entities.router, prefix="/api/entities", tags=["Entities"])
app.include_router(limits.router, prefix="/api/limits", tags=["Limits"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])

# Static files for dashboard UI
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/dashboard", StaticFiles(directory=str(static_path), html=True), name="static")


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint with links."""
    return {
        "message": "zae-limiter Demo API",
        "docs": "/docs",
        "dashboard": "/dashboard",
        "health": "/health",
    }

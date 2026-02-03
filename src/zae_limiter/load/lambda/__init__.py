"""Lambda handlers for load testing."""

from .worker import handler as worker_handler

__all__ = ["worker_handler"]

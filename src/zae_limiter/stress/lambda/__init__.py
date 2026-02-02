"""Lambda handlers for stress testing."""

from .setup import handler as setup_handler
from .worker import handler as worker_handler

__all__ = ["setup_handler", "worker_handler"]

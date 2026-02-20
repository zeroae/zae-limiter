"""Lambda provisioner for declarative limits management."""

from .applier import ApplyResult
from .differ import Change, compute_diff
from .handler import on_event
from .manifest import LimitsManifest

__all__ = ["ApplyResult", "Change", "LimitsManifest", "compute_diff", "on_event"]

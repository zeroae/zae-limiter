"""Hatch build hook to generate sync code before building."""

import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class SyncGeneratorHook(BuildHookInterface):
    """Generate sync code before building wheel/sdist."""

    PLUGIN_NAME = "sync-generator"

    def initialize(self, version: str, build_data: dict) -> None:
        """Run sync code generation."""
        root = Path(self.root)
        script = root / "scripts" / "generate_sync.py"

        if script.exists():
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Sync generation failed:\n{result.stderr}")

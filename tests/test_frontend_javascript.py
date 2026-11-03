"""Run the dependency-free DOM/event unit suite; never starts a browser/server."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_interactions_with_node():
    node = shutil.which("node")
    if not node:
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        if bundled.is_file():
            node = str(bundled)
    if not node:
        pytest.skip("Existing Node.js is required for the offline DOM/event suite")
    suite = Path(__file__).with_name("frontend_interactions.cjs")
    result = subprocess.run([node, "--test", str(suite)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr

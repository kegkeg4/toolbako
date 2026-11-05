"""Offline recovery DOM checks, using the already installed Node runtime only."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_recovery_form_javascript():
    node = shutil.which('node')
    if not node:
        bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        if bundled.is_file():
            node = str(bundled)
    if not node:
        pytest.skip('Existing Node.js is required for the offline DOM tests')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('recovery_form_interactions.cjs'))],
                            capture_output=True, text=True, encoding='utf8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_server_recovery_error_has_a_distinct_marker_and_login_path():
    template = (ROOT / 'templates/password_recovery.html').read_text(encoding='utf8')
    assert 'data-recovery-server-error' in template
    assert 'href="/login"' in template
    assert 'name="password"' in template and 'name="password_confirmation"' in template


def test_recovery_javascript_uses_a_new_cache_version():
    template = (ROOT / 'templates/base.html').read_text(encoding='utf8')
    assert '-forms-5c18e4219a85' not in template
    assert '/static/app.js?v={{ asset_version }}-recovery-' in template

"""The files consumed by pip/Railpack are complete, hashed, and agree with CI."""
from pathlib import Path
import re

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def locked_requirements(filename):
    text = (ROOT / filename).read_text().replace("\\\n", "")
    result = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("--hash=")
        requirement = Requirement(parts[0].strip())
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "=="
        assert not requirement.url and not requirement.extras
        assert len(parts) > 1
        hashes = {part.strip() for part in parts[1:]}
        assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in hashes)
        name = canonicalize_name(requirement.name)
        assert name not in result
        result[name] = (str(requirement.specifier), str(requirement.marker), hashes)
    assert result
    return result


def test_runtime_and_test_locks_pin_every_dependency_and_hash():
    runtime = locked_requirements("requirements.txt")
    development = locked_requirements("requirements-dev.txt")
    assert len(runtime) > 14 and len(development) > len(runtime)
    assert "pytest" not in runtime and "pytest" in development
    for name, specification in runtime.items():
        assert development[name] == specification


def test_direct_dependencies_match_the_deployment_lock():
    locked = locked_requirements("requirements.txt")
    for line in (ROOT / "requirements.in").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        assert str(requirement.specifier) == locked[canonicalize_name(requirement.name)][0]


def test_ci_installs_the_hashed_lock_and_checks_compatibility():
    workflow = (ROOT / ".github/workflows/test.yml").read_text()
    assert "pip install --require-hashes -r requirements-dev.txt" in workflow
    assert "python -m pip check" in workflow
    assert "cache-dependency-path: requirements-dev.txt" in workflow

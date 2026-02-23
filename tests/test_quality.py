"""
Build-time quality tests.

These tests verify code style and that every package in requirements.txt
is actually used somewhere in the api/ source tree.  They run in CI
before the Docker image is built.
"""

import ast
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
API_DIR = ROOT / "api"
REQUIREMENTS_FILE = ROOT / "requirements.txt"

# Packages that are legitimate runtime requirements but are never directly
# imported in application code (used implicitly by a framework).
IMPLICIT_DEPS = {
    "uvicorn",  # ASGI server — launched via CLI, never imported in app code
    "aiosqlite",  # SQLAlchemy async dialect — loaded from connection string, not imported
    "python-multipart",  # FastAPI requires it for UploadFile / Form support
}


def _parse_requirement_names() -> list[str]:
    names = []
    for line in REQUIREMENTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip version specifiers, extras, markers:
        # "sqlalchemy[asyncio]==2.0.37" → "sqlalchemy"
        name = re.split(r"[=<>!\[;]", line)[0].strip().lower()
        names.append(name)
    return names


def _dist_to_top_level_modules() -> dict[str, list[str]]:
    """
    Return a mapping of distribution name → list of top-level import names,
    built from the currently installed packages.

    Uses importlib.metadata.packages_distributions() which returns the inverse
    mapping (import_name → [dist_name, …]).
    """
    pkg_to_dists = importlib.metadata.packages_distributions()
    dist_to_modules: dict[str, list[str]] = {}
    for module_name, dist_names in pkg_to_dists.items():
        for dist in dist_names:
            dist_lower = dist.lower().replace("_", "-")
            dist_to_modules.setdefault(dist_lower, []).append(module_name)
    return dist_to_modules


def _top_level_imports_in_api() -> set[str]:
    """Collect every top-level module name imported anywhere under api/."""
    imports: set[str] = set()
    for py_file in API_DIR.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
    return imports


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_requirements_are_used():
    """Every package listed in requirements.txt must be imported in api/."""
    packages = _parse_requirement_names()
    dist_modules = _dist_to_top_level_modules()
    imported = _top_level_imports_in_api()

    unused = []
    for pkg in packages:
        if pkg in IMPLICIT_DEPS:
            continue
        modules = dist_modules.get(pkg, [pkg.replace("-", "_")])
        if not any(m in imported for m in modules):
            unused.append(pkg)

    assert unused == [], (
        f"Packages listed in requirements.txt but not imported in api/: {unused}"
    )


def test_ruff_formatting():
    """All files under api/ must be formatted with ruff."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--diff", str(API_DIR)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ruff found formatting issues:\n{result.stdout}\n{result.stderr}"
    )


def test_ruff_linting():
    """All files under api/ must pass ruff linting."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(API_DIR)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ruff found issues:\n{result.stdout}\n{result.stderr}"
    )

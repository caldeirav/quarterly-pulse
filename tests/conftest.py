"""Shared pytest fixtures for quarterly-pulse tests."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def template_path() -> Path:
    """Path to the canonical quarterly template shipped in the repository."""
    return REPO_ROOT / "templates" / "quarterly-template.md"


@pytest.fixture()
def template_text(template_path: Path) -> str:
    """Raw text of the canonical quarterly template."""
    return template_path.read_text(encoding="utf-8")

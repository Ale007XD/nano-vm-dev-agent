"""
tests/conftest.py
=================
Shared fixtures for nano-vm-dev-agent tests.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def tmp_py_file(tmp_path: Path):
    """Returns a factory that writes a .py file into tmp_path."""
    def _make(name: str, content: str) -> str:
        p = tmp_path / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return str(p)
    return _make


@pytest.fixture
def valid_py(tmp_py_file) -> str:
    """A mypy-clean Python file."""
    return tmp_py_file("clean.py", """\
        def add(a: int, b: int) -> int:
            return a + b
    """)


@pytest.fixture
def invalid_py(tmp_py_file) -> str:
    """A Python file with a mypy type error."""
    return tmp_py_file("broken.py", """\
        def add(a: int, b: int) -> int:
            return "wrong"  # type error
    """)


@pytest.fixture
def passing_test(tmp_py_file) -> str:
    """A pytest file with a passing test."""
    return tmp_py_file("test_pass.py", """\
        def test_ok():
            assert 1 + 1 == 2
    """)


@pytest.fixture
def failing_test(tmp_py_file) -> str:
    """A pytest file with a failing test."""
    return tmp_py_file("test_fail.py", """\
        def test_bad():
            assert 1 == 2
    """)

"""
Pytest configuration and fixtures for Code Intelligence MCP Server tests.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def project_root():
    """Return the project root directory."""
    return PROJECT_ROOT


@pytest.fixture
def sample_symbols():
    """Sample symbols for testing."""
    return [
        "AuthService",
        "UserRepository",
        "LoginController",
        "ConfigLoader",
        "DatabaseHelper",
        "SessionManager",
    ]


@pytest.fixture
def sample_query_frame():
    """Create a sample QueryFrame for testing."""
    from tools.query_frame import QueryFrame

    return QueryFrame(
        raw_query="ログイン機能でパスワードが空のときエラーが出ない",
        target_feature="ログイン機能",
        trigger_condition="パスワードが空のとき",
        observed_issue="エラーが出ない",
        desired_action=None,
    )


@pytest.fixture
def empty_query_frame():
    """Create an empty QueryFrame for testing."""
    from tools.query_frame import QueryFrame

    return QueryFrame(
        raw_query="バグを直して",
    )

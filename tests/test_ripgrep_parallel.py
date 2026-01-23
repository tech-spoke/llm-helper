"""Tests for ripgrep parallel search functionality."""

import asyncio
import pytest
from pathlib import Path
import sys

# Add parent directory to path to import tools
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.ripgrep_tool import search_text


@pytest.mark.asyncio
async def test_search_text_single_pattern():
    """Test single pattern search (backward compatibility)."""
    result = await search_text("import", path=".")

    assert "matches" in result or "error" in result
    if "matches" in result:
        assert result["pattern"] == "import"
        assert "total_matches" in result
        assert isinstance(result["matches"], list)


@pytest.mark.asyncio
async def test_search_text_multiple_patterns():
    """Test multiple pattern parallel search."""
    result = await search_text(["import", "async", "def"], path=".")

    assert "results" in result or "error" in result
    if "results" in result:
        assert len(result["results"]) == 3
        assert all(p in result["results"] for p in ["import", "async", "def"])
        assert "total_patterns" in result
        assert result["total_patterns"] == 3


@pytest.mark.asyncio
async def test_search_text_pattern_limit():
    """Test pattern count limit (max 5)."""
    # 6 patterns (exceeds limit)
    result = await search_text(["p1", "p2", "p3", "p4", "p5", "p6"], path=".")

    assert "error" in result
    assert "Maximum 5 patterns" in result["error"]
    assert "provided_patterns" in result
    assert len(result["provided_patterns"]) == 6


@pytest.mark.asyncio
async def test_search_text_five_patterns():
    """Test 5 patterns (at limit, should succeed)."""
    result = await search_text(
        ["import", "async", "def", "class", "return"],
        path="."
    )

    assert "results" in result or "error" in result
    if "results" in result:
        assert len(result["results"]) == 5
        assert result["total_patterns"] == 5


@pytest.mark.asyncio
async def test_search_text_empty_pattern_list():
    """Test empty pattern list."""
    result = await search_text([], path=".")

    # Empty list should return empty results
    assert "results" in result
    assert len(result["results"]) == 0
    assert result["total_patterns"] == 0


@pytest.mark.asyncio
async def test_search_text_nonexistent_path():
    """Test search in nonexistent path."""
    result = await search_text("import", path="/nonexistent/path")

    assert "error" in result
    assert "does not exist" in result["error"]


@pytest.mark.asyncio
async def test_search_text_parallel_performance():
    """Test that parallel search is actually parallel."""
    import time

    # Single pattern search
    start = time.time()
    await search_text("import", path=".")
    single_time = time.time() - start

    # Multiple pattern search (should not be 3x slower)
    start = time.time()
    await search_text(["import", "async", "def"], path=".")
    multi_time = time.time() - start

    # Parallel execution should be much faster than sequential
    # (Not exactly 3x because of overhead, but should be < 2x)
    assert multi_time < single_time * 2, \
        f"Parallel search ({multi_time:.2f}s) should be faster than sequential " \
        f"(expected < {single_time * 2:.2f}s)"


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])

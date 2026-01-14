"""Tests for context_provider.py (v1.1 Essential Context)."""

import tempfile
from pathlib import Path

import pytest
import yaml

from tools.context_provider import (
    ContextProvider,
    DocSummary,
    EssentialContext,
)


class TestEssentialContext:
    """Tests for EssentialContext dataclass."""

    def test_empty_context_to_dict(self):
        """Empty context returns empty dict."""
        ctx = EssentialContext()
        result = ctx.to_dict()
        assert result == {}

    def test_design_docs_to_dict(self):
        """Design docs are properly serialized."""
        ctx = EssentialContext(
            design_docs=[
                DocSummary(
                    file="test.md",
                    path="docs/test.md",
                    summary="Test summary",
                )
            ],
            design_docs_source="docs/",
        )
        result = ctx.to_dict()
        assert "design_docs" in result
        assert result["design_docs"]["source"] == "docs/"
        assert len(result["design_docs"]["summaries"]) == 1
        assert result["design_docs"]["summaries"][0]["file"] == "test.md"

    def test_project_rules_to_dict(self):
        """Project rules are properly serialized."""
        ctx = EssentialContext(
            project_rules_source="CLAUDE.md",
            project_rules_summary="DO:\n- Test\nDON'T:\n- Bad",
        )
        result = ctx.to_dict()
        assert "project_rules" in result
        assert result["project_rules"]["source"] == "CLAUDE.md"
        assert "DO:" in result["project_rules"]["summary"]

    def test_extra_notes_included(self):
        """Extra notes are included when present."""
        ctx = EssentialContext(
            design_docs=[
                DocSummary(
                    file="test.md",
                    path="docs/test.md",
                    summary="Summary",
                    extra_notes="Manual note",
                )
            ],
            design_docs_source="docs/",
            project_rules_source="CLAUDE.md",
            project_rules_summary="Rules",
            project_rules_extra_notes="Extra rule note",
        )
        result = ctx.to_dict()
        assert result["design_docs"]["summaries"][0]["extra_notes"] == "Manual note"
        assert result["project_rules"]["extra_notes"] == "Extra rule note"


class TestContextProvider:
    """Tests for ContextProvider class."""

    def test_load_context_no_file(self):
        """Returns None when context.yml doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)
            result = provider.load_context()
            assert result is None

    def test_load_context_empty_file(self):
        """Returns None when context.yml is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()
            context_file = code_intel_dir / "context.yml"
            context_file.write_text("")

            provider = ContextProvider(tmpdir)
            result = provider.load_context()
            assert result is None

    def test_load_context_with_design_docs(self):
        """Loads design docs from context.yml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()
            context_file = code_intel_dir / "context.yml"

            config = {
                "essential_docs": {
                    "source": "docs/",
                    "summaries": [
                        {
                            "file": "arch.md",
                            "path": "docs/arch.md",
                            "summary": "Architecture overview",
                        }
                    ],
                }
            }
            with open(context_file, "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            result = provider.load_context()
            assert result is not None
            assert len(result.design_docs) == 1
            assert result.design_docs[0].file == "arch.md"
            assert result.design_docs[0].summary == "Architecture overview"

    def test_load_context_with_project_rules(self):
        """Loads project rules from context.yml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()
            context_file = code_intel_dir / "context.yml"

            config = {
                "project_rules": {
                    "source": "CLAUDE.md",
                    "summary": "DO:\n- Follow patterns",
                }
            }
            with open(context_file, "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            result = provider.load_context()
            assert result is not None
            assert result.project_rules_source == "CLAUDE.md"
            assert "DO:" in result.project_rules_summary

    def test_generate_initial_context_detects_docs(self):
        """Detects common design doc directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create docs directory with markdown files
            docs_dir = Path(tmpdir) / "docs"
            docs_dir.mkdir()
            (docs_dir / "README.md").write_text("# Docs")

            provider = ContextProvider(tmpdir)
            result = provider.generate_initial_context()
            assert "essential_docs" in result
            assert result["essential_docs"]["source"] == "docs"

    def test_generate_initial_context_detects_claude_md(self):
        """Detects CLAUDE.md for project rules."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create CLAUDE.md
            (Path(tmpdir) / "CLAUDE.md").write_text("# Rules")

            provider = ContextProvider(tmpdir)
            result = provider.generate_initial_context()
            assert "project_rules" in result
            assert result["project_rules"]["source"] == "CLAUDE.md"

    def test_extract_doc_summary(self):
        """Extracts summary from markdown file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = Path(tmpdir) / "test.md"
            md_file.write_text("""# Architecture

This is the overview paragraph.

## Components

Component details here.

```python
# Code block should be skipped
def foo():
    pass
```

## Patterns

Pattern description.
""")

            provider = ContextProvider(tmpdir)
            result = provider.extract_doc_summary(md_file)
            assert "# Architecture" in result
            assert "overview paragraph" in result
            assert "## Components" in result
            assert "def foo" not in result  # Code block skipped

    def test_extract_project_rules(self):
        """Extracts DO/DON'T rules from project rules file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rules_file = Path(tmpdir) / "CLAUDE.md"
            rules_file.write_text("""# Project Rules

## DO
- Use Service layer for business logic
- Write tests for all features

## DON'T
- Write complex logic in Controllers
- Skip code review
""")

            provider = ContextProvider(tmpdir)
            result = provider.extract_project_rules(rules_file)
            assert "DO:" in result
            assert "Service layer" in result
            assert "DON'T:" in result
            assert "Controllers" in result


class TestContextProviderFileHash:
    """Tests for file hashing functionality."""

    def test_file_hash_consistent(self):
        """Same content produces same hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "file1.txt"
            file2 = Path(tmpdir) / "file2.txt"
            content = "Same content"
            file1.write_text(content)
            file2.write_text(content)

            provider = ContextProvider(tmpdir)
            hash1 = provider._file_hash(file1)
            hash2 = provider._file_hash(file2)
            assert hash1 == hash2

    def test_file_hash_different(self):
        """Different content produces different hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "file1.txt"
            file2 = Path(tmpdir) / "file2.txt"
            file1.write_text("Content A")
            file2.write_text("Content B")

            provider = ContextProvider(tmpdir)
            hash1 = provider._file_hash(file1)
            hash2 = provider._file_hash(file2)
            assert hash1 != hash2

    def test_file_hash_nonexistent(self):
        """Returns empty string for nonexistent file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)
            result = provider._file_hash(Path(tmpdir) / "nonexistent.txt")
            assert result == ""

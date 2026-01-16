"""
Tests for Context Provider module.

Tests the context provision functionality including:
- Loading context from context.yml
- Document change detection
- Summary extraction
- Initial context generation
"""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from tools.context_provider import (
    ContextProvider,
    DocSummary,
    EssentialContext,
    get_summary_prompts,
    DESIGN_DOC_SUMMARY_PROMPT,
    PROJECT_RULES_SUMMARY_PROMPT,
)


class TestContextProviderInit:
    """Tests for ContextProvider initialization."""

    def test_init_with_default_path(self):
        """Should initialize with current directory."""
        provider = ContextProvider()
        # ContextProvider stores Path(".") without resolving
        assert provider.repo_path == Path(".")

    def test_init_with_custom_path(self):
        """Should initialize with custom path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)
            assert provider.repo_path == Path(tmpdir)
            assert provider.code_intel_dir == Path(tmpdir) / ".code-intel"


class TestLoadContext:
    """Tests for loading context from context.yml."""

    def test_load_nonexistent_file(self):
        """Should return None when context.yml doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)
            assert provider.load_context() is None

    def test_load_empty_config(self):
        """Should return None for empty config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            # Empty YAML file
            (code_intel_dir / "context.yml").write_text("")

            provider = ContextProvider(tmpdir)
            assert provider.load_context() is None

    def test_load_design_docs_only(self):
        """Should load design docs summaries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "essential_docs": {
                    "source": "docs",
                    "summaries": [
                        {
                            "file": "architecture.md",
                            "path": "docs/architecture.md",
                            "summary": "3-layer architecture",
                            "content_hash": "abc123",
                        }
                    ]
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            context = provider.load_context()

            assert context is not None
            assert len(context.design_docs) == 1
            assert context.design_docs[0].file == "architecture.md"
            assert context.design_docs[0].summary == "3-layer architecture"

    def test_load_project_rules_only(self):
        """Should load project rules summary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "project_rules": {
                    "source": "CLAUDE.md",
                    "summary": "DO: Test everything\nDON'T: Skip reviews",
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            context = provider.load_context()

            assert context is not None
            assert context.project_rules_source == "CLAUDE.md"
            assert "DO:" in context.project_rules_summary

    def test_load_with_extra_notes(self):
        """Should load extra_notes fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "essential_docs": {
                    "source": "docs",
                    "summaries": [
                        {
                            "file": "arch.md",
                            "path": "docs/arch.md",
                            "summary": "Summary",
                            "extra_notes": "Exception: Simple CRUD can bypass Service",
                        }
                    ]
                },
                "project_rules": {
                    "source": "CLAUDE.md",
                    "summary": "Rules",
                    "extra_notes": "Legacy code exception",
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            context = provider.load_context()

            assert context.design_docs[0].extra_notes == "Exception: Simple CRUD can bypass Service"
            assert context.project_rules_extra_notes == "Legacy code exception"


class TestGetContextConfig:
    """Tests for getting raw context config."""

    def test_get_config_returns_raw_dict(self):
        """Should return the raw YAML as dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "essential_docs": {"source": "docs"},
                "document_search": {"include_patterns": ["**/*.md"]}
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            raw_config = provider.get_context_config()

            assert raw_config["document_search"]["include_patterns"] == ["**/*.md"]


class TestCheckDocsChanged:
    """Tests for document change detection."""

    def test_detect_new_doc(self):
        """Should detect new documents not in summaries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create directory structure
            docs_dir = Path(tmpdir) / "docs"
            docs_dir.mkdir()
            (docs_dir / "new.md").write_text("# New Document")

            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "essential_docs": {
                    "source": "docs",
                    "summaries": []  # No existing summaries
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            changes = provider.check_docs_changed()

            assert len(changes) == 1
            assert changes[0]["change"] == "new"
            assert "new.md" in changes[0]["path"]

    def test_detect_modified_doc(self):
        """Should detect modified documents via hash change."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create directory structure
            docs_dir = Path(tmpdir) / "docs"
            docs_dir.mkdir()
            doc_file = docs_dir / "arch.md"
            doc_file.write_text("# Updated Content")

            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "essential_docs": {
                    "source": "docs",
                    "summaries": [
                        {
                            "file": "arch.md",
                            "path": "docs/arch.md",
                            "summary": "Old summary",
                            "content_hash": "old_hash_12345678",  # Different hash
                        }
                    ]
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            changes = provider.check_docs_changed()

            assert len(changes) == 1
            assert changes[0]["change"] == "modified"

    def test_detect_project_rules_change(self):
        """Should detect changes in project rules file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create CLAUDE.md
            (Path(tmpdir) / "CLAUDE.md").write_text("# Project Rules")

            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "project_rules": {
                    "source": "CLAUDE.md",
                    "summary": "Old rules",
                    "content_hash": "old_hash_12345678",
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            provider = ContextProvider(tmpdir)
            changes = provider.check_docs_changed()

            assert len(changes) == 1
            assert changes[0]["type"] == "project_rules"


class TestExtractDocSummary:
    """Tests for fallback document summary extraction."""

    def test_extract_headers(self):
        """Should extract headers from markdown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = Path(tmpdir) / "test.md"
            doc.write_text("# Title\n\nFirst paragraph.\n\n## Section\n\nSection content.")

            provider = ContextProvider(tmpdir)
            summary = provider.extract_doc_summary(doc)

            assert "# Title" in summary
            assert "## Section" in summary
            assert "First paragraph" in summary

    def test_skip_code_blocks(self):
        """Should skip content in code blocks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            doc = Path(tmpdir) / "test.md"
            doc.write_text("# Title\n\n```python\n# This is code\nprint('hello')\n```\n\nReal content.")

            provider = ContextProvider(tmpdir)
            summary = provider.extract_doc_summary(doc)

            # Code block content should not be in summary
            assert "print('hello')" not in summary
            assert "Real content" in summary


class TestExtractProjectRules:
    """Tests for project rules extraction."""

    def test_extract_do_dont_sections(self):
        """Should extract DO/DON'T sections."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rules = Path(tmpdir) / "CLAUDE.md"
            rules.write_text(
                "# Project Rules\n\n"
                "## DO\n"
                "- Write tests\n"
                "- Use type hints\n\n"
                "## DON'T\n"
                "- Skip reviews\n"
                "- Push to main\n"
            )

            provider = ContextProvider(tmpdir)
            summary = provider.extract_project_rules(rules)

            assert "DO:" in summary
            assert "Write tests" in summary
            assert "DON'T:" in summary
            assert "Skip reviews" in summary

    def test_fallback_to_bullet_points(self):
        """Should extract bullet points as fallback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rules = Path(tmpdir) / "CLAUDE.md"
            rules.write_text(
                "# Guidelines\n\n"
                "- Always test\n"
                "- Use consistent naming\n"
                "- Document changes\n"
            )

            provider = ContextProvider(tmpdir)
            summary = provider.extract_project_rules(rules)

            assert "Rules:" in summary
            assert "Always test" in summary


class TestGenerateInitialContext:
    """Tests for initial context generation."""

    def test_detect_docs_directory(self):
        """Should detect docs directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_dir = Path(tmpdir) / "docs"
            docs_dir.mkdir()
            (docs_dir / "README.md").write_text("# Docs")

            provider = ContextProvider(tmpdir)
            config = provider.generate_initial_context()

            assert "essential_docs" in config
            assert config["essential_docs"]["source"] == "docs"

    def test_detect_claude_md(self):
        """Should detect CLAUDE.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "CLAUDE.md").write_text("# Rules")

            provider = ContextProvider(tmpdir)
            config = provider.generate_initial_context()

            assert "project_rules" in config
            assert config["project_rules"]["source"] == "CLAUDE.md"

    def test_include_document_search_defaults(self):
        """Should include document_search defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)
            config = provider.generate_initial_context()

            assert "document_search" in config
            assert "**/*.md" in config["document_search"]["include_patterns"]
            assert "node_modules/**" in config["document_search"]["exclude_patterns"]


class TestSaveContext:
    """Tests for saving context configuration."""

    def test_save_creates_directory(self):
        """Should create .code-intel directory if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)

            config = {"test": "value"}
            provider.save_context(config)

            assert (Path(tmpdir) / ".code-intel" / "context.yml").exists()

    def test_save_preserves_unicode(self):
        """Should preserve Unicode in YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)

            config = {"title": "日本語テスト"}
            provider.save_context(config)

            # Read back
            with open(provider.context_file, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)

            assert loaded["title"] == "日本語テスト"


class TestUpdateSummaries:
    """Tests for updating summaries."""

    def test_update_preserves_extra_notes(self):
        """Should preserve existing extra_notes when updating summaries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            # Initial config with extra_notes
            initial_config = {
                "essential_docs": {
                    "source": "docs",
                    "summaries": [
                        {
                            "file": "arch.md",
                            "path": "docs/arch.md",
                            "summary": "Old summary",
                            "extra_notes": "Keep this note",
                            "content_hash": "old",
                        }
                    ]
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(initial_config, f)

            provider = ContextProvider(tmpdir)

            # Update with new summary
            new_summaries = [
                DocSummary(
                    file="arch.md",
                    path="docs/arch.md",
                    summary="New summary",
                    content_hash="new",
                )
            ]

            provider.update_summaries(new_summaries)

            # Check extra_notes preserved
            updated = provider.get_context_config()
            assert updated["essential_docs"]["summaries"][0]["extra_notes"] == "Keep this note"
            assert updated["essential_docs"]["summaries"][0]["summary"] == "New summary"


class TestEssentialContext:
    """Tests for EssentialContext dataclass."""

    def test_to_dict_empty(self):
        """Empty context should return empty dict."""
        context = EssentialContext()
        assert context.to_dict() == {}

    def test_to_dict_with_design_docs(self):
        """Should serialize design docs correctly."""
        context = EssentialContext(
            design_docs=[
                DocSummary(
                    file="arch.md",
                    path="docs/arch.md",
                    summary="Architecture overview",
                )
            ],
            design_docs_source="docs",
        )

        d = context.to_dict()

        assert d["design_docs"]["source"] == "docs"
        assert len(d["design_docs"]["summaries"]) == 1

    def test_to_dict_excludes_empty_extra_notes(self):
        """Should not include empty extra_notes."""
        context = EssentialContext(
            design_docs=[
                DocSummary(
                    file="arch.md",
                    path="docs/arch.md",
                    summary="Summary",
                    extra_notes="",  # Empty
                )
            ],
            design_docs_source="docs",
        )

        d = context.to_dict()

        assert "extra_notes" not in d["design_docs"]["summaries"][0]


class TestFileHash:
    """Tests for file hash calculation."""

    def test_hash_consistency(self):
        """Same content should produce same hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)

            file1 = Path(tmpdir) / "file1.txt"
            file2 = Path(tmpdir) / "file2.txt"

            file1.write_text("Same content")
            file2.write_text("Same content")

            hash1 = provider._file_hash(file1)
            hash2 = provider._file_hash(file2)

            assert hash1 == hash2

    def test_hash_different_content(self):
        """Different content should produce different hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)

            file1 = Path(tmpdir) / "file1.txt"
            file2 = Path(tmpdir) / "file2.txt"

            file1.write_text("Content A")
            file2.write_text("Content B")

            hash1 = provider._file_hash(file1)
            hash2 = provider._file_hash(file2)

            assert hash1 != hash2

    def test_hash_returns_truncated(self):
        """Hash should be truncated to 16 chars."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = ContextProvider(tmpdir)

            file = Path(tmpdir) / "file.txt"
            file.write_text("Test content")

            hash_value = provider._file_hash(file)

            assert len(hash_value) == 16


class TestSummaryPrompts:
    """Tests for summary generation prompts."""

    def test_get_summary_prompts(self):
        """Should return prompts dict."""
        prompts = get_summary_prompts()

        assert "design_doc" in prompts
        assert "project_rules" in prompts

    def test_design_doc_prompt_has_placeholder(self):
        """Design doc prompt should have content placeholder."""
        assert "{document_content}" in DESIGN_DOC_SUMMARY_PROMPT

    def test_project_rules_prompt_has_placeholder(self):
        """Project rules prompt should have content placeholder."""
        assert "{document_content}" in PROJECT_RULES_SUMMARY_PROMPT

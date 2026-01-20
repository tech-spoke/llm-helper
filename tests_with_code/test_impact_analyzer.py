"""
Tests for Impact Analyzer module.

Tests the impact analysis functionality including:
- Static reference detection
- Naming convention matching
- Markup relaxation
- Document keyword search (v1.1.1)
"""

import tempfile
from pathlib import Path

import pytest
import yaml

from tools.impact_analyzer import (
    ImpactAnalyzer,
    ImpactAnalysisResult,
    analyze_impact,
    RELAXED_MARKUP_EXTENSIONS,
    LOGIC_MARKUP_EXTENSIONS,
    DEFAULT_DOCUMENT_PATTERNS,
    DEFAULT_DOCUMENT_EXCLUDE_PATTERNS,
)


class TestMarkupRelaxation:
    """Tests for markup relaxation feature."""

    def test_should_relax_pure_markup_files(self):
        """Pure markup files should trigger relaxation."""
        analyzer = ImpactAnalyzer()

        # Pure HTML
        assert analyzer._should_relax_markup(["styles.css"]) is True
        assert analyzer._should_relax_markup(["page.html"]) is True
        assert analyzer._should_relax_markup(["README.md"]) is True

        # Multiple pure markup
        assert analyzer._should_relax_markup(["a.css", "b.html"]) is True

    def test_should_not_relax_logic_files(self):
        """Logic-containing files should NOT trigger relaxation."""
        analyzer = ImpactAnalyzer()

        # Logic files
        assert analyzer._should_relax_markup(["app.py"]) is False
        assert analyzer._should_relax_markup(["script.js"]) is False
        assert analyzer._should_relax_markup(["Component.tsx"]) is False

        # Blade templates (contain PHP logic)
        assert analyzer._should_relax_markup(["view.blade.php"]) is False

        # Vue single-file components
        assert analyzer._should_relax_markup(["App.vue"]) is False

    def test_should_not_relax_mixed_files(self):
        """Mixed markup and logic files should NOT trigger relaxation."""
        analyzer = ImpactAnalyzer()

        # CSS + JS = not relaxed
        assert analyzer._should_relax_markup(["styles.css", "app.js"]) is False

        # HTML + PHP = not relaxed
        assert analyzer._should_relax_markup(["page.html", "handler.php"]) is False

    @pytest.mark.asyncio
    async def test_relaxed_result_structure(self):
        """Relaxed result should have correct structure."""
        analyzer = ImpactAnalyzer()
        result = await analyzer._create_relaxed_result(
            target_files=["styles.css"],
            change_description="Test markup change"
        )

        assert result.mode == "relaxed_markup"
        assert result.depth == "direct_only"
        assert result.static_references == {}
        assert result.naming_convention_matches == {}
        assert result.confirmation_required["must_verify"] == []
        # Note: should_verify may contain cross-reference suggestions for CSS


class TestBaseNameExtraction:
    """Tests for base name extraction from file paths."""

    def test_simple_extraction(self):
        """Basic file name extraction."""
        analyzer = ImpactAnalyzer()

        assert analyzer._extract_base_name("Product.php") == "Product"
        assert analyzer._extract_base_name("UserService.py") == "UserService"
        assert analyzer._extract_base_name("styles.css") == "styles"

    def test_path_extraction(self):
        """Extraction from full paths."""
        analyzer = ImpactAnalyzer()

        assert analyzer._extract_base_name("app/Models/Product.php") == "Product"
        assert analyzer._extract_base_name("src/services/UserService.ts") == "UserService"

    def test_snake_case_conversion(self):
        """Snake_case should be converted to PascalCase."""
        analyzer = ImpactAnalyzer()

        assert analyzer._extract_base_name("user_service.py") == "UserService"
        assert analyzer._extract_base_name("cart_item_handler.php") == "CartItemHandler"

    def test_multi_part_extension(self):
        """Multi-part extensions like .blade.php should be handled."""
        analyzer = ImpactAnalyzer()

        assert analyzer._extract_base_name("welcome.blade.php") == "welcome"
        assert analyzer._extract_base_name("test.spec.ts") == "test"


class TestTypehintDetection:
    """Tests for type hint detection in references."""

    def test_python_type_hints(self):
        """Python-style type hints should be detected."""
        analyzer = ImpactAnalyzer()

        # Type hints pattern expects terminator after symbol (space/comma/bracket)
        assert analyzer._looks_like_type_hint(": Product,", "Product") is True
        assert analyzer._looks_like_type_hint(": Product)", "Product") is True
        assert analyzer._looks_like_type_hint("def foo() -> Product", "Product") is True
        assert analyzer._looks_like_type_hint("List[Product]", "Product") is True

    def test_phpdoc_type_hints(self):
        """PHPDoc-style type hints should be detected."""
        analyzer = ImpactAnalyzer()

        assert analyzer._looks_like_type_hint("@param Product $item", "Product") is True
        assert analyzer._looks_like_type_hint("@return Product", "Product") is True
        assert analyzer._looks_like_type_hint("@var Product", "Product") is True

    def test_generic_type_hints(self):
        """Generic type syntax should be detected."""
        analyzer = ImpactAnalyzer()

        assert analyzer._looks_like_type_hint("Collection<Product>", "Product") is True

    def test_non_type_hints(self):
        """Non-type-hint usage should not be detected as type hints."""
        analyzer = ImpactAnalyzer()

        assert analyzer._looks_like_type_hint("$product = new Product()", "Product") is False
        assert analyzer._looks_like_type_hint("product.save()", "Product") is False


class TestKeywordExtraction:
    """Tests for keyword extraction from change descriptions."""

    def test_quoted_strings_high_priority(self):
        """Quoted strings should be extracted as high priority."""
        analyzer = ImpactAnalyzer()

        keywords = analyzer._extract_keywords(
            'Change "auto_billing" field type',
            []
        )
        assert "auto_billing" in keywords
        # Quoted strings should appear early (high priority)
        assert keywords.index("auto_billing") < 3

    def test_camel_case_extraction(self):
        """CamelCase terms should be extracted."""
        analyzer = ImpactAnalyzer()

        keywords = analyzer._extract_keywords(
            "Modify ProductPrice calculation",
            []
        )
        assert "ProductPrice" in keywords

    def test_snake_case_extraction(self):
        """snake_case terms should be extracted."""
        analyzer = ImpactAnalyzer()

        keywords = analyzer._extract_keywords(
            "Update user_account field",
            []
        )
        assert "user_account" in keywords

    def test_file_base_names_low_priority(self):
        """File base names should be extracted but with low priority."""
        analyzer = ImpactAnalyzer()

        keywords = analyzer._extract_keywords(
            "Change field",
            ["app/Models/Product.php"]
        )
        assert "Product" in keywords

    def test_stop_words_filtered(self):
        """Common stop words should be filtered out."""
        analyzer = ImpactAnalyzer()

        keywords = analyzer._extract_keywords(
            "Add the file to update",
            []
        )
        assert "the" not in keywords
        assert "add" not in keywords
        assert "update" not in keywords

    def test_keyword_limit(self):
        """Keywords should be limited to MAX_KEYWORDS."""
        analyzer = ImpactAnalyzer()

        # Generate many potential keywords
        keywords = analyzer._extract_keywords(
            '"key1" "key2" "key3" "key4" "key5" "key6" "key7" "key8" "key9" "key10" "key11" "key12"',
            ["File1.py", "File2.py", "File3.py", "File4.py", "File5.py"]
        )
        assert len(keywords) <= 10


class TestDocumentConfig:
    """Tests for document_search configuration loading."""

    def test_default_patterns_when_no_config(self):
        """Default patterns should be used when no config exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = ImpactAnalyzer(tmpdir)

            # No .code-intel directory, should use defaults
            assert analyzer._document_config == {}

    def test_load_config_from_context_yml(self):
        """Config should be loaded from context.yml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_intel_dir = Path(tmpdir) / ".code-intel"
            code_intel_dir.mkdir()

            config = {
                "document_search": {
                    "include_patterns": ["**/*.md"],
                    "exclude_patterns": ["CHANGELOG.md"],
                }
            }

            with open(code_intel_dir / "context.yml", "w") as f:
                yaml.dump(config, f)

            analyzer = ImpactAnalyzer(tmpdir)

            assert analyzer._document_config["include_patterns"] == ["**/*.md"]
            assert analyzer._document_config["exclude_patterns"] == ["CHANGELOG.md"]


class TestExcludePatternMatching:
    """Tests for exclude pattern matching."""

    def test_glob_pattern_matching(self):
        """Glob patterns should match correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = ImpactAnalyzer(tmpdir)

            # Create a test path structure
            test_file = Path(tmpdir) / "docs" / "test.md"

            assert analyzer._matches_exclude_pattern(
                str(test_file),
                ["docs/**"]
            ) is True

            assert analyzer._matches_exclude_pattern(
                str(test_file),
                ["other/**"]
            ) is False

    def test_filename_pattern_matching(self):
        """Simple filename patterns should also work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = ImpactAnalyzer(tmpdir)

            test_file = Path(tmpdir) / "CHANGELOG.md"

            assert analyzer._matches_exclude_pattern(
                str(test_file),
                ["CHANGELOG*"]
            ) is True


class TestAnalyzeImpact:
    """Integration tests for analyze_impact function."""

    @pytest.mark.asyncio
    async def test_analyze_markup_only(self):
        """Markup-only analysis should return relaxed mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a CSS file
            css_file = Path(tmpdir) / "styles.css"
            css_file.write_text("body { margin: 0; }")

            result = await analyze_impact(
                target_files=[str(css_file)],
                change_description="Update styles",
                repo_path=tmpdir
            )

            assert result["impact_analysis"]["mode"] == "relaxed_markup"

    @pytest.mark.asyncio
    async def test_analyze_returns_dict(self):
        """analyze_impact should return a dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = Path(tmpdir) / "test.py"
            py_file.write_text("def hello(): pass")

            result = await analyze_impact(
                target_files=[str(py_file)],
                change_description="Add function",
                repo_path=tmpdir
            )

            assert isinstance(result, dict)
            assert "impact_analysis" in result
            assert "confirmation_required" in result


class TestImpactAnalysisResult:
    """Tests for ImpactAnalysisResult dataclass."""

    def test_to_dict_minimal(self):
        """Minimal result should serialize correctly."""
        result = ImpactAnalysisResult(
            mode="standard",
            depth="direct_only",
        )

        d = result.to_dict()

        assert d["impact_analysis"]["mode"] == "standard"
        assert d["impact_analysis"]["depth"] == "direct_only"

    def test_to_dict_with_references(self):
        """Result with references should serialize correctly."""
        result = ImpactAnalysisResult(
            mode="standard",
            depth="direct_only",
            static_references={
                "callers": [{"file": "test.py", "line": 10}]
            },
        )

        d = result.to_dict()

        assert len(d["impact_analysis"]["static_references"]["callers"]) == 1

    def test_to_dict_with_document_mentions(self):
        """Document mentions should be included when present."""
        result = ImpactAnalysisResult(
            mode="standard",
            depth="direct_only",
            document_mentions={
                "files": [{"file": "docs/README.md", "match_count": 3}],
                "keywords_searched": ["test"]
            },
        )

        d = result.to_dict()

        assert "document_mentions" in d["impact_analysis"]
        assert d["impact_analysis"]["document_mentions"]["keywords_searched"] == ["test"]

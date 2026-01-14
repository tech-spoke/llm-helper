"""Tests for impact_analyzer.py (v1.1 Impact Analysis)."""

import tempfile
from pathlib import Path

import pytest

from tools.impact_analyzer import (
    ImpactAnalyzer,
    ImpactAnalysisResult,
    RELAXED_MARKUP_EXTENSIONS,
    LOGIC_MARKUP_EXTENSIONS,
)


class TestImpactAnalysisResult:
    """Tests for ImpactAnalysisResult dataclass."""

    def test_standard_result_to_dict(self):
        """Standard result is properly serialized."""
        result = ImpactAnalysisResult(
            mode="standard",
            depth="direct_only",
            static_references={"callers": [{"file": "test.py", "line": 10}]},
            naming_convention_matches={"tests": ["test_foo.py"]},
            confirmation_required={
                "must_verify": ["test.py"],
                "should_verify": ["test_foo.py"],
            },
        )
        output = result.to_dict()
        assert output["impact_analysis"]["mode"] == "standard"
        assert output["impact_analysis"]["depth"] == "direct_only"
        assert "callers" in output["impact_analysis"]["static_references"]
        assert output["confirmation_required"]["must_verify"] == ["test.py"]

    def test_relaxed_result_to_dict(self):
        """Relaxed markup result is properly serialized."""
        result = ImpactAnalysisResult(
            mode="relaxed_markup",
            reason="Only markup files",
            static_references={},
            naming_convention_matches={},
            confirmation_required={"must_verify": [], "should_verify": []},
        )
        output = result.to_dict()
        assert output["impact_analysis"]["mode"] == "relaxed_markup"
        assert output["impact_analysis"]["reason"] == "Only markup files"


class TestMarkupRelaxation:
    """Tests for markup relaxation logic."""

    def test_relaxed_extensions(self):
        """Verify relaxed extension list."""
        expected = {".html", ".htm", ".css", ".scss", ".md", ".markdown"}
        assert RELAXED_MARKUP_EXTENSIONS == expected

    def test_logic_extensions(self):
        """Verify logic-containing extension list."""
        expected = {".blade.php", ".vue", ".jsx", ".tsx"}
        assert LOGIC_MARKUP_EXTENSIONS == expected

    def test_should_relax_html(self):
        """HTML files should be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["test.html"]) is True
        assert analyzer._should_relax_markup(["test.htm"]) is True

    def test_should_relax_css(self):
        """CSS files should be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["style.css"]) is True
        assert analyzer._should_relax_markup(["style.scss"]) is True

    def test_should_relax_md(self):
        """Markdown files should be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["README.md"]) is True
        assert analyzer._should_relax_markup(["docs.markdown"]) is True

    def test_should_not_relax_vue(self):
        """Vue files should NOT be relaxed (contain logic)."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["Component.vue"]) is False

    def test_should_not_relax_jsx(self):
        """JSX files should NOT be relaxed (contain logic)."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["Component.jsx"]) is False

    def test_should_not_relax_tsx(self):
        """TSX files should NOT be relaxed (contain logic)."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["Component.tsx"]) is False

    def test_should_not_relax_blade(self):
        """Blade.php files should NOT be relaxed (contain logic)."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["view.blade.php"]) is False

    def test_should_not_relax_python(self):
        """Python files should NOT be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["script.py"]) is False

    def test_should_not_relax_php(self):
        """PHP files should NOT be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["index.php"]) is False

    def test_mixed_files_not_relaxed(self):
        """Mixed markup and logic files should NOT be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["style.css", "app.js"]) is False
        assert analyzer._should_relax_markup(["index.html", "script.py"]) is False

    def test_empty_list_not_relaxed(self):
        """Empty file list should NOT be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup([]) is False

    def test_multiple_markup_relaxed(self):
        """Multiple pure markup files should be relaxed."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._should_relax_markup(["style.css", "index.html", "README.md"]) is True


class TestBaseNameExtraction:
    """Tests for base name extraction."""

    def test_extract_simple_name(self):
        """Extract simple file name."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._extract_base_name("Product.php") == "Product"
        assert analyzer._extract_base_name("User.py") == "User"

    def test_extract_with_path(self):
        """Extract name from path."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._extract_base_name("app/Models/Product.php") == "Product"
        assert analyzer._extract_base_name("src/components/UserProfile.tsx") == "UserProfile"

    def test_extract_snake_case(self):
        """Convert snake_case to PascalCase."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._extract_base_name("cart_service.py") == "CartService"
        assert analyzer._extract_base_name("user_profile_controller.php") == "UserProfileController"

    def test_extract_blade_php(self):
        """Handle multi-extension files like .blade.php."""
        analyzer = ImpactAnalyzer(".")
        # .blade.php should extract "test" not "test.blade"
        assert analyzer._extract_base_name("test.blade.php") == "test"


class TestTypeHintDetection:
    """Tests for type hint detection heuristics."""

    def test_colon_type_hint(self):
        """Detect Python/PHP type hints with colon."""
        analyzer = ImpactAnalyzer(".")
        # Type hints typically have something after the symbol
        assert analyzer._looks_like_type_hint("def foo(x: Product)", "Product") is True
        assert analyzer._looks_like_type_hint("param: Product,", "Product") is True
        assert analyzer._looks_like_type_hint("x: Product)", "Product") is True
        assert analyzer._looks_like_type_hint("x: Product]", "Product") is True

    def test_arrow_return_type(self):
        """Detect return type hints with arrow."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._looks_like_type_hint("-> Product", "Product") is True

    def test_generic_type_hint(self):
        """Detect generic type hints."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._looks_like_type_hint("List<Product>", "Product") is True
        assert analyzer._looks_like_type_hint("Collection<Product>", "Product") is True

    def test_bracket_type_hint(self):
        """Detect bracket type hints (Python typing)."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._looks_like_type_hint("List[Product]", "Product") is True

    def test_phpdoc_param(self):
        """Detect PHPDoc @param."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._looks_like_type_hint("@param Product", "Product") is True

    def test_phpdoc_return(self):
        """Detect PHPDoc @return."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._looks_like_type_hint("@return Product", "Product") is True

    def test_phpdoc_var(self):
        """Detect PHPDoc @var."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._looks_like_type_hint("@var Product", "Product") is True

    def test_not_type_hint(self):
        """Regular usage is not a type hint."""
        analyzer = ImpactAnalyzer(".")
        assert analyzer._looks_like_type_hint("$product = new Product()", "Product") is False
        assert analyzer._looks_like_type_hint("product.save()", "product") is False


class TestDeduplication:
    """Tests for reference deduplication."""

    def test_deduplicate_refs(self):
        """Duplicate references are removed."""
        analyzer = ImpactAnalyzer(".")
        refs = [
            {"file": "a.py", "line": 10},
            {"file": "a.py", "line": 10},  # Duplicate
            {"file": "a.py", "line": 20},
            {"file": "b.py", "line": 10},
        ]
        result = analyzer._deduplicate_refs(refs)
        assert len(result) == 3

    def test_deduplicate_preserves_order(self):
        """Deduplication preserves first occurrence order."""
        analyzer = ImpactAnalyzer(".")
        refs = [
            {"file": "c.py", "line": 30},
            {"file": "a.py", "line": 10},
            {"file": "b.py", "line": 20},
            {"file": "a.py", "line": 10},  # Duplicate
        ]
        result = analyzer._deduplicate_refs(refs)
        assert result[0]["file"] == "c.py"
        assert result[1]["file"] == "a.py"
        assert result[2]["file"] == "b.py"


class TestGlobFiles:
    """Tests for glob file matching."""

    def test_glob_files_finds_matches(self):
        """Glob finds matching files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            (Path(tmpdir) / "ProductTest.py").write_text("")
            (Path(tmpdir) / "UserTest.py").write_text("")
            (Path(tmpdir) / "Other.py").write_text("")

            analyzer = ImpactAnalyzer(tmpdir)
            result = analyzer._glob_files("*Test.py")
            assert len(result) == 2

    def test_glob_files_no_matches(self):
        """Glob returns empty list when no matches."""
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = ImpactAnalyzer(tmpdir)
            result = analyzer._glob_files("*Factory.py")
            assert result == []


class TestRelaxedResult:
    """Tests for relaxed result creation."""

    def test_create_relaxed_result(self):
        """Relaxed result has correct structure."""
        analyzer = ImpactAnalyzer(".")
        result = analyzer._create_relaxed_result()
        assert result.mode == "relaxed_markup"
        assert result.static_references == {}
        assert result.naming_convention_matches == {}
        assert result.confirmation_required["must_verify"] == []
        assert result.confirmation_required["should_verify"] == []

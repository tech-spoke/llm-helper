"""
Tests for tools/embedding.py - EmbeddingValidator

v3.7: Tests for Embedding-based semantic similarity validation.
"""

import pytest

from tools.embedding import (
    EmbeddingValidator,
    ValidationResult,
    get_embedding_validator,
    is_embedding_available,
)


class TestEmbeddingValidator:
    """Test EmbeddingValidator class."""

    def test_split_camel_case_basic(self):
        """Test camel case splitting."""
        assert EmbeddingValidator.split_camel_case("AuthService") == "Auth Service"
        assert EmbeddingValidator.split_camel_case("getUserName") == "get User Name"
        assert EmbeddingValidator.split_camel_case("XMLParser") == "XML Parser"
        assert EmbeddingValidator.split_camel_case("simple") == "simple"

    def test_split_camel_case_edge_cases(self):
        """Test edge cases for camel case splitting."""
        assert EmbeddingValidator.split_camel_case("") == ""
        assert EmbeddingValidator.split_camel_case("ABC") == "ABC"
        assert EmbeddingValidator.split_camel_case("HTTPSConnection") == "HTTPS Connection"

    def test_is_available(self):
        """Test is_available method."""
        validator = EmbeddingValidator()
        # Should return True since sentence-transformers is installed
        assert validator.is_available() is True

    def test_lazy_loading(self):
        """Test that model is lazily loaded."""
        validator = EmbeddingValidator()
        # Model should not be loaded yet
        assert validator._model is None
        # After checking availability, model should still not be loaded
        validator.is_available()
        assert validator._model is None

    @pytest.mark.slow
    def test_get_similarity_basic(self):
        """Test basic similarity calculation."""
        validator = EmbeddingValidator()
        # Same text should have high similarity
        sim = validator.get_similarity("login", "login")
        assert sim > 0.9

        # Related terms should have moderate similarity
        sim = validator.get_similarity("login", "authentication")
        assert sim > 0.3

        # Very unrelated terms should have lower similarity
        sim_related = validator.get_similarity("login", "authentication")
        sim_unrelated = validator.get_similarity("login", "weather forecast")
        # Related pair should have higher similarity than unrelated
        assert sim_related > sim_unrelated

    @pytest.mark.slow
    def test_validate_relevance_high_similarity(self):
        """Test validation with high similarity (>0.6 -> FACT)."""
        validator = EmbeddingValidator()
        result = validator.validate_relevance("認証", "Auth Service")
        assert isinstance(result, ValidationResult)
        # High similarity should be approved
        if result.similarity > 0.6:
            assert result.approved is True
            assert result.status == "FACT"
            assert result.risk_adjustment is None

    @pytest.mark.slow
    def test_validate_relevance_medium_similarity(self):
        """Test validation with medium similarity (0.3-0.6 -> FACT + HIGH risk)."""
        validator = EmbeddingValidator()
        # Test a pair that might be in gray zone
        result = validator.validate_relevance("設定", "ConfigLoader")
        assert isinstance(result, ValidationResult)
        if 0.3 <= result.similarity <= 0.6:
            assert result.approved is True
            assert result.status == "FACT"
            assert result.risk_adjustment == "HIGH"
            assert result.warning is not None

    @pytest.mark.slow
    def test_validate_relevance_low_similarity(self):
        """Test validation with low similarity (<0.3 -> REJECTED)."""
        validator = EmbeddingValidator()
        result = validator.validate_relevance("ログイン", "DatabaseHelper")
        assert isinstance(result, ValidationResult)
        # If similarity is very low, should be rejected
        if result.similarity < 0.3:
            assert result.approved is False
            assert result.status == "REJECTED"
            assert result.reinvestigation_guidance is not None
            assert "next_actions" in result.reinvestigation_guidance

    @pytest.mark.slow
    def test_validate_multiple_symbols(self):
        """Test validation of multiple symbols at once."""
        validator = EmbeddingValidator()
        symbols = ["AuthService", "LoginController", "ConfigLoader", "DatabaseHelper"]
        result = validator.validate_multiple("ログイン機能", symbols)

        assert "approved" in result
        assert "rejected" in result
        assert isinstance(result["approved"], list)
        assert isinstance(result["rejected"], list)
        # Total should equal input count
        assert len(result["approved"]) + len(result["rejected"]) == len(symbols)

    @pytest.mark.slow
    def test_find_related_symbols(self):
        """Test finding related symbols by vector search."""
        validator = EmbeddingValidator()
        symbols = ["AuthService", "UserRepository", "ConfigLoader", "SessionManager"]
        results = validator.find_related_symbols("ログイン", symbols, top_k=3)

        assert len(results) <= 3
        for r in results:
            assert "symbol" in r
            assert "similarity" in r
            assert "rank" in r
        # Should be sorted by similarity
        if len(results) >= 2:
            assert results[0]["similarity"] >= results[1]["similarity"]

    @pytest.mark.slow
    def test_thresholds(self):
        """Test that thresholds are correctly defined."""
        validator = EmbeddingValidator()
        assert validator.THRESHOLD_HIGH == 0.6
        assert validator.THRESHOLD_LOW == 0.3
        assert validator.THRESHOLD_HIGH > validator.THRESHOLD_LOW


class TestValidationResult:
    """Test ValidationResult dataclass."""

    def test_to_dict_basic(self):
        """Test basic to_dict conversion."""
        result = ValidationResult(
            approved=True,
            status="FACT",
            similarity=0.75,
        )
        d = result.to_dict()
        assert d["approved"] is True
        assert d["status"] == "FACT"
        assert d["similarity"] == 0.75
        assert "risk_adjustment" not in d
        assert "warning" not in d

    def test_to_dict_with_optional_fields(self):
        """Test to_dict with optional fields."""
        result = ValidationResult(
            approved=True,
            status="FACT",
            similarity=0.45,
            risk_adjustment="HIGH",
            warning="Gray zone warning",
        )
        d = result.to_dict()
        assert d["risk_adjustment"] == "HIGH"
        assert d["warning"] == "Gray zone warning"

    def test_to_dict_with_guidance(self):
        """Test to_dict with reinvestigation guidance."""
        guidance = {
            "reason": "Low similarity",
            "next_actions": ["search_text", "find_references"],
        }
        result = ValidationResult(
            approved=False,
            status="REJECTED",
            similarity=0.15,
            reinvestigation_guidance=guidance,
        )
        d = result.to_dict()
        assert d["reinvestigation_guidance"] == guidance


class TestModuleFunctions:
    """Test module-level functions."""

    def test_is_embedding_available(self):
        """Test is_embedding_available function."""
        assert is_embedding_available() is True

    def test_get_embedding_validator_singleton(self):
        """Test that get_embedding_validator returns singleton."""
        v1 = get_embedding_validator()
        v2 = get_embedding_validator()
        assert v1 is v2

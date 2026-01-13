"""
Tests for tools/query_frame.py

Tests cover:
- Slot validation and hallucination detection
- NL to symbol mapping
- QueryFrame operations
- Risk level assessment
- Investigation guidance generation
"""

import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.query_frame import (
    SlotSource,
    SlotData,
    SlotEvidence,
    MappedSymbol,
    QueryFrame,
    QueryDecomposer,
    validate_slot,
    _is_semantically_consistent,
    validate_nl_symbol_mapping,
    generate_investigation_guidance,
    assess_risk_level,
    get_exploration_requirements,
    validate_for_ready,
    INVESTIGATION_HINTS,
)


# =============================================================================
# Test: SlotSource enum
# =============================================================================

class TestSlotSource:
    def test_slot_source_values(self):
        """SlotSource should have correct values."""
        assert SlotSource.FACT.value == "FACT"
        assert SlotSource.HYPOTHESIS.value == "HYPOTHESIS"
        assert SlotSource.UNRESOLVED.value == "UNRESOLVED"


# =============================================================================
# Test: validate_slot (hallucination detection)
# =============================================================================

class TestValidateSlot:
    def test_valid_slot_returns_value_and_quote(self):
        """Valid slot should return value and quote."""
        raw_query = "ログイン機能でエラーが発生する"
        extracted = {
            "value": "ログイン機能",
            "quote": "ログイン機能で"
        }
        value, quote = validate_slot("target_feature", extracted, raw_query)
        assert value == "ログイン機能"
        assert quote == "ログイン機能で"

    def test_missing_value_returns_none(self):
        """Missing value should return None."""
        raw_query = "ログイン機能でエラーが発生する"
        extracted = {"value": None, "quote": "ログイン機能"}
        value, quote = validate_slot("target_feature", extracted, raw_query)
        assert value is None
        assert quote is None

    def test_missing_quote_returns_none(self):
        """Missing quote should return None (hallucination)."""
        raw_query = "ログイン機能でエラーが発生する"
        extracted = {"value": "ログイン機能", "quote": None}
        value, quote = validate_slot("target_feature", extracted, raw_query)
        assert value is None
        assert quote is None

    def test_quote_not_in_query_returns_none(self):
        """Quote not in original query should return None (hallucination)."""
        raw_query = "ログイン機能でエラーが発生する"
        extracted = {
            "value": "認証システム",
            "quote": "認証システムで"  # Not in raw_query
        }
        value, quote = validate_slot("target_feature", extracted, raw_query)
        assert value is None
        assert quote is None

    def test_english_slot_validation(self):
        """Should work with English queries."""
        raw_query = "Fix the login function when password is empty"
        extracted = {
            "value": "login function",
            "quote": "login function"
        }
        value, quote = validate_slot("target_feature", extracted, raw_query)
        assert value == "login function"
        assert quote == "login function"


# =============================================================================
# Test: _is_semantically_consistent
# =============================================================================

class TestIsSemanticallyCosistent:
    def test_exact_match_is_consistent(self):
        """Exact match should be consistent."""
        assert _is_semantically_consistent("login", "login") is True

    def test_value_in_quote_is_consistent(self):
        """Value contained in quote should be consistent."""
        assert _is_semantically_consistent("login", "login function") is True

    def test_quote_in_value_is_consistent(self):
        """Quote contained in value should be consistent."""
        assert _is_semantically_consistent("login function", "login") is True

    def test_common_words_is_consistent(self):
        """Common words should be consistent."""
        assert _is_semantically_consistent("user login", "login system") is True

    def test_no_common_is_inconsistent(self):
        """No common elements should be inconsistent."""
        assert _is_semantically_consistent("apple", "banana") is False

    def test_japanese_char_overlap_is_consistent(self):
        """Japanese character overlap should be consistent."""
        assert _is_semantically_consistent("ログイン機能", "ログイン処理") is True

    def test_japanese_particles_ignored(self):
        """Japanese particles should be ignored."""
        # 助詞を除いて共通文字があればOK
        assert _is_semantically_consistent("機能を追加", "機能の実装") is True


# =============================================================================
# Test: validate_nl_symbol_mapping
# =============================================================================

class TestValidateNlSymbolMapping:
    def test_exact_match(self):
        """Exact match should return True."""
        has_match, matched = validate_nl_symbol_mapping(
            "AuthService",
            ["AuthService", "UserRepo", "Config"]
        )
        assert has_match is True
        assert "AuthService" in matched

    def test_partial_match(self):
        """Partial match should return True."""
        has_match, matched = validate_nl_symbol_mapping(
            "auth",
            ["AuthService", "UserRepo", "AuthController"]
        )
        assert has_match is True
        assert "AuthService" in matched
        assert "AuthController" in matched

    def test_no_match(self):
        """No match should return False."""
        has_match, matched = validate_nl_symbol_mapping(
            "payment",
            ["AuthService", "UserRepo", "Config"]
        )
        assert has_match is False
        assert matched == []

    def test_word_level_match(self):
        """Word level match (underscored) should work."""
        has_match, matched = validate_nl_symbol_mapping(
            "user",
            ["user_service", "auth_controller", "config"]
        )
        assert has_match is True
        assert "user_service" in matched

    def test_case_insensitive(self):
        """Matching should be case insensitive."""
        has_match, matched = validate_nl_symbol_mapping(
            "AUTH",
            ["authService", "UserRepo"]
        )
        assert has_match is True
        assert "authService" in matched


# =============================================================================
# Test: QueryFrame
# =============================================================================

class TestQueryFrame:
    def test_get_missing_slots_all_empty(self):
        """All slots empty should return all slot names."""
        frame = QueryFrame(raw_query="test query")
        missing = frame.get_missing_slots()
        assert "target_feature" in missing
        assert "trigger_condition" in missing
        assert "observed_issue" in missing
        assert "desired_action" in missing
        assert len(missing) == 4

    def test_get_missing_slots_partial(self):
        """Partial slots should return only missing ones."""
        frame = QueryFrame(
            raw_query="test query",
            target_feature="login",
            observed_issue="error"
        )
        missing = frame.get_missing_slots()
        assert "target_feature" not in missing
        assert "observed_issue" not in missing
        assert "trigger_condition" in missing
        assert "desired_action" in missing
        assert len(missing) == 2

    def test_get_hypothesis_slots(self):
        """Should return slots with HYPOTHESIS source."""
        frame = QueryFrame(raw_query="test")
        frame.slot_source["target_feature"] = SlotSource.FACT
        frame.slot_source["trigger_condition"] = SlotSource.HYPOTHESIS
        frame.slot_source["observed_issue"] = SlotSource.HYPOTHESIS

        hypothesis_slots = frame.get_hypothesis_slots()
        assert "trigger_condition" in hypothesis_slots
        assert "observed_issue" in hypothesis_slots
        assert "target_feature" not in hypothesis_slots

    def test_add_mapped_symbol_new(self):
        """Adding new symbol should work."""
        frame = QueryFrame(raw_query="test")
        frame.add_mapped_symbol(
            name="AuthService",
            source=SlotSource.FACT,
            confidence=0.9
        )
        assert len(frame.mapped_symbols) == 1
        assert frame.mapped_symbols[0].name == "AuthService"
        assert frame.mapped_symbols[0].confidence == 0.9

    def test_add_mapped_symbol_duplicate_updates(self):
        """Adding duplicate symbol should update existing."""
        frame = QueryFrame(raw_query="test")
        frame.add_mapped_symbol(
            name="AuthService",
            source=SlotSource.HYPOTHESIS,
            confidence=0.5
        )
        frame.add_mapped_symbol(
            name="AuthService",
            source=SlotSource.FACT,
            confidence=0.9
        )
        assert len(frame.mapped_symbols) == 1
        assert frame.mapped_symbols[0].source == SlotSource.FACT
        assert frame.mapped_symbols[0].confidence == 0.9

    def test_get_fact_symbols(self):
        """Should return only FACT symbols."""
        frame = QueryFrame(raw_query="test")
        frame.add_mapped_symbol("Auth", SlotSource.FACT, 0.9)
        frame.add_mapped_symbol("User", SlotSource.HYPOTHESIS, 0.5)
        frame.add_mapped_symbol("Config", SlotSource.FACT, 0.8)

        facts = frame.get_fact_symbols()
        assert len(facts) == 2
        assert all(s.source == SlotSource.FACT for s in facts)

    def test_get_hypothesis_symbols(self):
        """Should return only HYPOTHESIS symbols."""
        frame = QueryFrame(raw_query="test")
        frame.add_mapped_symbol("Auth", SlotSource.FACT, 0.9)
        frame.add_mapped_symbol("User", SlotSource.HYPOTHESIS, 0.5)

        hypotheses = frame.get_hypothesis_symbols()
        assert len(hypotheses) == 1
        assert hypotheses[0].name == "User"

    def test_to_dict(self):
        """to_dict should serialize frame correctly."""
        frame = QueryFrame(
            raw_query="test query",
            target_feature="login",
        )
        frame.slot_source["target_feature"] = SlotSource.FACT

        d = frame.to_dict()
        assert d["raw_query"] == "test query"
        assert d["target_feature"] == "login"
        assert "FACT" in d["slot_source"]["target_feature"]
        assert "target_feature" not in d["missing_slots"]


# =============================================================================
# Test: QueryDecomposer
# =============================================================================

class TestQueryDecomposer:
    def test_get_extraction_prompt_contains_query(self):
        """Extraction prompt should contain the query."""
        query = "ログイン機能を修正して"
        prompt = QueryDecomposer.get_extraction_prompt(query)
        assert query in prompt
        assert "target_feature" in prompt
        assert "quote" in prompt

    def test_validate_extraction_with_valid_data(self):
        """Valid extraction should create correct QueryFrame."""
        raw_query = "ログイン機能でエラーが発生する"
        extracted = {
            "target_feature": {"value": "ログイン機能", "quote": "ログイン機能で"},
            "observed_issue": {"value": "エラーが発生", "quote": "エラーが発生する"},
        }

        frame = QueryDecomposer.validate_extraction(raw_query, extracted)

        assert frame.target_feature == "ログイン機能"
        assert frame.observed_issue == "エラーが発生"
        assert frame.slot_source["target_feature"] == SlotSource.FACT

    def test_validate_extraction_filters_hallucinations(self):
        """Hallucinations should be filtered out."""
        raw_query = "ログイン機能でエラーが発生する"
        extracted = {
            "target_feature": {"value": "認証システム", "quote": "認証システム"},  # Not in query
            "observed_issue": {"value": "エラー", "quote": "エラーが発生"},  # Valid
        }

        frame = QueryDecomposer.validate_extraction(raw_query, extracted)

        assert frame.target_feature is None  # Filtered as hallucination
        assert frame.observed_issue == "エラー"  # Valid


# =============================================================================
# Test: generate_investigation_guidance
# =============================================================================

class TestGenerateInvestigationGuidance:
    def test_generates_hints_for_missing_slots(self):
        """Should generate hints for each missing slot."""
        missing = ["target_feature", "observed_issue"]
        guidance = generate_investigation_guidance(missing)

        assert "target_feature" in guidance["missing_slots"]
        assert "observed_issue" in guidance["missing_slots"]
        assert len(guidance["hints"]) == 2

    def test_recommends_tools(self):
        """Should recommend appropriate tools."""
        missing = ["target_feature"]
        guidance = generate_investigation_guidance(missing)

        # target_feature hints should include query, get_symbols
        assert "query" in guidance["recommended_tools"]
        assert "get_symbols" in guidance["recommended_tools"]

    def test_no_duplicates_in_tools(self):
        """Recommended tools should not have duplicates."""
        missing = ["target_feature", "observed_issue"]
        guidance = generate_investigation_guidance(missing)

        tools = guidance["recommended_tools"]
        assert len(tools) == len(set(tools))

    def test_empty_missing_returns_empty_hints(self):
        """Empty missing slots should return empty hints."""
        guidance = generate_investigation_guidance([])
        assert guidance["hints"] == []
        assert guidance["recommended_tools"] == []


# =============================================================================
# Test: assess_risk_level
# =============================================================================

class TestAssessRiskLevel:
    def test_high_risk_when_action_without_issue(self):
        """Should be HIGH when desired_action but no observed_issue."""
        frame = QueryFrame(
            raw_query="test",
            desired_action="fix it",
            observed_issue=None,
        )
        assert assess_risk_level(frame, "MODIFY") == "HIGH"

    def test_high_risk_when_modify_without_target(self):
        """Should be HIGH when MODIFY without target_feature."""
        frame = QueryFrame(
            raw_query="test",
            target_feature=None,
        )
        assert assess_risk_level(frame, "MODIFY") == "HIGH"

    def test_high_risk_when_implement_with_nothing(self):
        """Should be HIGH when IMPLEMENT with no slots filled."""
        frame = QueryFrame(raw_query="test")
        assert assess_risk_level(frame, "IMPLEMENT") == "HIGH"

    def test_medium_risk_with_hypothesis_slots(self):
        """Should be MEDIUM when there are HYPOTHESIS slots."""
        frame = QueryFrame(
            raw_query="test",
            target_feature="login",
            observed_issue="error occurs",
        )
        frame.slot_source["target_feature"] = SlotSource.HYPOTHESIS
        assert assess_risk_level(frame, "IMPLEMENT") == "MEDIUM"

    def test_low_risk_when_all_good(self):
        """Should be LOW when all conditions are met."""
        frame = QueryFrame(
            raw_query="test",
            target_feature="login function",
            observed_issue="error occurs when clicking",
            desired_action="show error message",
        )
        assert assess_risk_level(frame, "IMPLEMENT") == "LOW"


# =============================================================================
# Test: get_exploration_requirements
# =============================================================================

class TestGetExplorationRequirements:
    def test_high_risk_has_strict_requirements(self):
        """HIGH risk should have strict requirements."""
        reqs = get_exploration_requirements("HIGH", "IMPLEMENT")
        assert reqs["symbols_identified"] == 5
        assert reqs["files_analyzed"] == 4
        assert "target_feature" in reqs["required_slot_evidence"]

    def test_low_risk_has_base_requirements(self):
        """LOW risk should have base requirements."""
        reqs = get_exploration_requirements("LOW", "IMPLEMENT")
        assert reqs["symbols_identified"] == 3
        assert reqs["files_analyzed"] == 2

    def test_investigate_has_minimal_requirements(self):
        """INVESTIGATE should have minimal requirements."""
        reqs = get_exploration_requirements("HIGH", "INVESTIGATE")
        assert reqs["symbols_identified"] == 1
        assert reqs["entry_points"] == 0


# =============================================================================
# Test: validate_for_ready
# =============================================================================

class TestValidateForReady:
    def test_no_errors_when_all_fact(self):
        """Should return empty errors when all slots are FACT."""
        frame = QueryFrame(raw_query="test")
        frame.slot_source["target_feature"] = SlotSource.FACT
        frame.slot_source["observed_issue"] = SlotSource.FACT

        errors = validate_for_ready(frame)
        assert errors == []

    def test_error_when_hypothesis_slot(self):
        """Should return error when HYPOTHESIS slot exists."""
        frame = QueryFrame(raw_query="test")
        frame.slot_source["target_feature"] = SlotSource.HYPOTHESIS

        errors = validate_for_ready(frame)
        assert len(errors) == 1
        assert "HYPOTHESIS" in errors[0]
        assert "target_feature" in errors[0]

    def test_error_when_hypothesis_symbol(self):
        """Should return error when HYPOTHESIS symbol exists."""
        frame = QueryFrame(raw_query="test")
        frame.add_mapped_symbol("AuthService", SlotSource.HYPOTHESIS, 0.5)

        errors = validate_for_ready(frame)
        assert len(errors) == 1
        assert "HYPOTHESIS" in errors[0]
        assert "AuthService" in errors[0]

    def test_multiple_errors(self):
        """Should return all errors."""
        frame = QueryFrame(raw_query="test")
        frame.slot_source["target_feature"] = SlotSource.HYPOTHESIS
        frame.slot_source["observed_issue"] = SlotSource.HYPOTHESIS
        frame.add_mapped_symbol("Auth", SlotSource.HYPOTHESIS, 0.5)

        errors = validate_for_ready(frame)
        assert len(errors) == 3


# =============================================================================
# Test: MappedSymbol
# =============================================================================

class TestMappedSymbol:
    def test_to_dict(self):
        """MappedSymbol should serialize correctly."""
        evidence = SlotEvidence(
            tool="find_definitions",
            params={"symbol": "Auth"},
            result_summary="Found in auth.py:10"
        )
        symbol = MappedSymbol(
            name="AuthService",
            source=SlotSource.FACT,
            confidence=0.9,
            evidence=evidence
        )

        d = symbol.to_dict()
        assert d["name"] == "AuthService"
        assert d["source"] == "FACT"
        assert d["confidence"] == 0.9
        assert d["evidence"]["tool"] == "find_definitions"

    def test_to_dict_without_evidence(self):
        """MappedSymbol without evidence should serialize."""
        symbol = MappedSymbol(
            name="AuthService",
            source=SlotSource.HYPOTHESIS,
            confidence=0.5,
            evidence=None
        )

        d = symbol.to_dict()
        assert d["evidence"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

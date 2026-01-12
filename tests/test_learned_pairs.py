"""
Tests for tools/learned_pairs.py - LearnedPairsCache

v3.7: Tests for NL→Symbol pair caching.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools.learned_pairs import (
    LearnedPair,
    LearnedPairsCache,
    cache_successful_pair,
    find_cached_matches,
    get_learned_pairs_cache,
)


class TestLearnedPair:
    """Test LearnedPair dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        pair = LearnedPair(
            nl_term="ログイン",
            symbol="AuthService",
            similarity=0.85,
            code_evidence="AuthService.login() handles authentication",
            session_id="sess_123",
            learned_at="2025-01-12T10:00:00",
        )
        d = pair.to_dict()
        assert d["nl_term"] == "ログイン"
        assert d["symbol"] == "AuthService"
        assert d["similarity"] == 0.85
        assert d["code_evidence"] == "AuthService.login() handles authentication"
        assert d["session_id"] == "sess_123"
        assert d["learned_at"] == "2025-01-12T10:00:00"

    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "nl_term": "認証",
            "symbol": "LoginController",
            "similarity": 0.72,
            "code_evidence": None,
            "session_id": "sess_456",
            "learned_at": "2025-01-12T11:00:00",
        }
        pair = LearnedPair.from_dict(data)
        assert pair.nl_term == "認証"
        assert pair.symbol == "LoginController"
        assert pair.similarity == 0.72
        assert pair.code_evidence is None


class TestLearnedPairsCache:
    """Test LearnedPairsCache class."""

    def test_init(self, temp_dir):
        """Test cache initialization."""
        cache = LearnedPairsCache(project_root=temp_dir)
        assert cache.project_root == Path(temp_dir).resolve()
        assert cache._loaded is False
        assert len(cache._pairs) == 0

    def test_add_pair(self, temp_dir):
        """Test adding a pair to cache."""
        cache = LearnedPairsCache(project_root=temp_dir)
        cache.add_pair(
            nl_term="ログイン",
            symbol="AuthService",
            similarity=0.85,
            code_evidence="Evidence here",
            session_id="sess_001",
        )
        assert len(cache._pairs) == 1
        assert cache._pairs[0].nl_term == "ログイン"
        assert cache._pairs[0].symbol == "AuthService"

    def test_add_pair_updates_existing(self, temp_dir):
        """Test that adding duplicate pair updates existing."""
        cache = LearnedPairsCache(project_root=temp_dir)
        cache.add_pair(
            nl_term="ログイン",
            symbol="AuthService",
            similarity=0.85,
            code_evidence="Old evidence",
            session_id="sess_001",
        )
        cache.add_pair(
            nl_term="ログイン",
            symbol="AuthService",
            similarity=0.90,
            code_evidence="New evidence",
            session_id="sess_002",
        )
        # Should still have only one pair
        assert len(cache._pairs) == 1
        assert cache._pairs[0].similarity == 0.90
        assert cache._pairs[0].code_evidence == "New evidence"
        assert cache._pairs[0].session_id == "sess_002"

    def test_find_matches(self, temp_dir):
        """Test finding matching pairs."""
        cache = LearnedPairsCache(project_root=temp_dir)
        cache.add_pair("ログイン", "AuthService", 0.85, None, "sess_001")
        cache.add_pair("ログイン", "LoginController", 0.75, None, "sess_001")
        cache.add_pair("設定", "ConfigLoader", 0.70, None, "sess_002")

        # Find matches for "ログイン"
        matches = cache.find_matches("ログイン", ["AuthService", "ConfigLoader"])
        assert len(matches) == 1
        assert matches[0].symbol == "AuthService"

        # Find matches with multiple hits
        matches = cache.find_matches(
            "ログイン", ["AuthService", "LoginController", "ConfigLoader"]
        )
        assert len(matches) == 2

        # No matches
        matches = cache.find_matches("データベース", ["AuthService"])
        assert len(matches) == 0

    def test_save_and_load(self, temp_dir):
        """Test saving and loading cache from file."""
        cache1 = LearnedPairsCache(project_root=temp_dir)
        cache1.add_pair("ログイン", "AuthService", 0.85, "Evidence", "sess_001")
        cache1.add_pair("設定", "ConfigLoader", 0.70, None, "sess_002")

        # Create new cache instance and load
        cache2 = LearnedPairsCache(project_root=temp_dir)
        cache2.load()

        assert len(cache2._pairs) == 2
        assert any(p.nl_term == "ログイン" for p in cache2._pairs)
        assert any(p.nl_term == "設定" for p in cache2._pairs)

    def test_cleanup_old_pairs(self, temp_dir):
        """Test cleanup of old pairs."""
        cache = LearnedPairsCache(project_root=temp_dir)

        # Add a recent pair
        cache.add_pair("ログイン", "AuthService", 0.85, None, "sess_001")

        # Manually add an old pair
        old_date = (datetime.now() - timedelta(days=35)).isoformat()
        cache._pairs.append(
            LearnedPair(
                nl_term="古い",
                symbol="OldService",
                similarity=0.80,
                code_evidence=None,
                session_id="old_sess",
                learned_at=old_date,
            )
        )
        cache.save()

        # Cleanup
        removed = cache.cleanup_old_pairs()
        assert removed == 1
        assert len(cache._pairs) == 1
        assert cache._pairs[0].nl_term == "ログイン"

    def test_get_stats(self, temp_dir):
        """Test getting cache statistics."""
        cache = LearnedPairsCache(project_root=temp_dir)
        cache.add_pair("ログイン", "AuthService", 0.85, None, "sess_001")
        cache.add_pair("ログイン", "LoginController", 0.75, None, "sess_001")
        cache.add_pair("設定", "ConfigLoader", 0.70, None, "sess_002")

        stats = cache.get_stats()
        assert stats["total_pairs"] == 3
        assert stats["unique_nl_terms"] == 2  # ログイン, 設定
        assert stats["unique_symbols"] == 3
        assert "cache_path" in stats

    def test_clear(self, temp_dir):
        """Test clearing cache."""
        cache = LearnedPairsCache(project_root=temp_dir)
        cache.add_pair("ログイン", "AuthService", 0.85, None, "sess_001")
        assert cache.cache_path.exists()

        cache.clear()
        assert len(cache._pairs) == 0
        assert not cache.cache_path.exists()

    def test_load_corrupted_file(self, temp_dir):
        """Test loading from corrupted JSON file."""
        cache = LearnedPairsCache(project_root=temp_dir)
        cache._ensure_dir()
        cache.cache_path.write_text("invalid json", encoding="utf-8")

        cache.load()
        # Should handle gracefully with empty pairs
        assert len(cache._pairs) == 0


class TestModuleFunctions:
    """Test module-level helper functions."""

    def test_cache_successful_pair(self, temp_dir):
        """Test cache_successful_pair helper."""
        cache_successful_pair(
            nl_term="テスト",
            symbol="TestService",
            similarity=0.80,
            code_evidence="Evidence",
            session_id="sess_test",
            project_root=temp_dir,
        )

        cache = get_learned_pairs_cache(temp_dir)
        assert len(cache._pairs) == 1
        assert cache._pairs[0].nl_term == "テスト"

    def test_find_cached_matches(self, temp_dir):
        """Test find_cached_matches helper."""
        cache_successful_pair("ログイン", "AuthService", 0.85, None, "sess_001", temp_dir)

        matches = find_cached_matches("ログイン", ["AuthService", "Other"], temp_dir)
        assert len(matches) == 1
        assert matches[0]["symbol"] == "AuthService"
        assert matches[0]["similarity"] == 0.85

    def test_get_learned_pairs_cache_singleton(self, temp_dir):
        """Test singleton behavior with same project root."""
        cache1 = get_learned_pairs_cache(temp_dir)
        cache2 = get_learned_pairs_cache(temp_dir)
        assert cache1 is cache2

    def test_cache_file_format(self, temp_dir):
        """Test the JSON file format."""
        cache = LearnedPairsCache(project_root=temp_dir)
        cache.add_pair("ログイン", "AuthService", 0.85, "Evidence", "sess_001")

        # Read and verify file format
        data = json.loads(cache.cache_path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert "updated_at" in data
        assert "pairs" in data
        assert len(data["pairs"]) == 1
        assert data["pairs"][0]["nl_term"] == "ログイン"

"""
Learned Pairs Cache for v3.7.

v3.7: 成功したNL→Symbolペアを自動キャッシュ
- 成功時に mapped_symbols を learned_pairs に追加
- 次回探索時にキャッシュから優先的に提示
- 有効期限とクリーンアップ機構
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class LearnedPair:
    """学習されたNL→Symbolペア"""
    nl_term: str
    symbol: str
    similarity: float
    code_evidence: Optional[str]
    session_id: str
    learned_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LearnedPair":
        return cls(**data)


class LearnedPairsCache:
    """
    成功ペアのキャッシュ管理。

    プロジェクトルートの .code-intel/learned_pairs.json に保存。
    """

    DEFAULT_PATH = ".code-intel/learned_pairs.json"
    MAX_AGE_DAYS = 30  # 30日経過したペアは削除

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        self.cache_path = self.project_root / self.DEFAULT_PATH
        self._pairs: list[LearnedPair] = []
        self._loaded = False

    def _ensure_dir(self) -> None:
        """キャッシュディレクトリを作成"""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """キャッシュをロード"""
        if self._loaded:
            return

        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self._pairs = [
                    LearnedPair.from_dict(p)
                    for p in data.get("pairs", [])
                ]
            except (json.JSONDecodeError, KeyError):
                self._pairs = []

        self._loaded = True

    def save(self) -> None:
        """キャッシュを保存"""
        self._ensure_dir()
        data = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "pairs": [p.to_dict() for p in self._pairs],
        }
        self.cache_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add_pair(
        self,
        nl_term: str,
        symbol: str,
        similarity: float,
        code_evidence: Optional[str],
        session_id: str,
    ) -> None:
        """ペアを追加（重複チェック付き）"""
        self.load()

        # 既存のペアをチェック
        for pair in self._pairs:
            if pair.nl_term == nl_term and pair.symbol == symbol:
                # 既存ペアを更新
                pair.similarity = similarity
                pair.code_evidence = code_evidence
                pair.session_id = session_id
                pair.learned_at = datetime.now().isoformat()
                self.save()
                return

        # 新規ペアを追加
        self._pairs.append(LearnedPair(
            nl_term=nl_term,
            symbol=symbol,
            similarity=similarity,
            code_evidence=code_evidence,
            session_id=session_id,
            learned_at=datetime.now().isoformat(),
        ))
        self.save()

    def find_matches(
        self,
        nl_term: str,
        symbols: list[str],
    ) -> list[LearnedPair]:
        """
        キャッシュからマッチするペアを検索。

        Args:
            nl_term: 自然言語の用語
            symbols: 探索で見つかったシンボルのリスト

        Returns:
            マッチしたペアのリスト
        """
        self.load()
        matches = []

        for pair in self._pairs:
            if pair.nl_term == nl_term and pair.symbol in symbols:
                matches.append(pair)

        return matches

    def cleanup_old_pairs(self) -> int:
        """古いペアを削除"""
        self.load()
        cutoff = datetime.now() - timedelta(days=self.MAX_AGE_DAYS)
        original_count = len(self._pairs)

        self._pairs = [
            p for p in self._pairs
            if datetime.fromisoformat(p.learned_at) > cutoff
        ]

        removed = original_count - len(self._pairs)
        if removed > 0:
            self.save()

        return removed

    def get_stats(self) -> dict:
        """キャッシュ統計を取得"""
        self.load()
        return {
            "total_pairs": len(self._pairs),
            "unique_nl_terms": len(set(p.nl_term for p in self._pairs)),
            "unique_symbols": len(set(p.symbol for p in self._pairs)),
            "cache_path": str(self.cache_path),
        }

    def clear(self) -> None:
        """キャッシュをクリア"""
        self._pairs = []
        if self.cache_path.exists():
            self.cache_path.unlink()
        self._loaded = False


# シングルトンインスタンス
_cache_instance: Optional[LearnedPairsCache] = None


def get_learned_pairs_cache(project_root: str = ".") -> LearnedPairsCache:
    """LearnedPairsCacheのシングルトンを取得"""
    global _cache_instance
    if _cache_instance is None or str(_cache_instance.project_root) != str(Path(project_root).resolve()):
        _cache_instance = LearnedPairsCache(project_root)
    return _cache_instance


def cache_successful_pair(
    nl_term: str,
    symbol: str,
    similarity: float,
    code_evidence: Optional[str],
    session_id: str,
    project_root: str = ".",
) -> None:
    """成功ペアをキャッシュに追加（ヘルパー関数）"""
    cache = get_learned_pairs_cache(project_root)
    cache.add_pair(nl_term, symbol, similarity, code_evidence, session_id)


def find_cached_matches(
    nl_term: str,
    symbols: list[str],
    project_root: str = ".",
) -> list[dict]:
    """キャッシュからマッチを検索（ヘルパー関数）"""
    cache = get_learned_pairs_cache(project_root)
    matches = cache.find_matches(nl_term, symbols)
    return [m.to_dict() for m in matches]

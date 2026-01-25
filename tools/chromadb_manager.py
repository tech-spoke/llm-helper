"""
ChromaDB Manager for Code Intelligence MCP Server v3.9

プロジェクトごとの ChromaDB を管理し、ソースコードの意味検索を提供する。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ChromaDB のインポート（オプショナル）
try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logger.warning("chromadb not installed. Install with: pip install chromadb")

from tools.ast_chunker import ASTChunker, Chunk, detect_language, chunk_directory
from tools.sync_state import SyncStateManager, SyncResult


@dataclass
class SearchHit:
    """検索結果を表すデータクラス"""
    id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content[:500],  # 長すぎる場合は切り詰め
            "score": self.score,
            "metadata": self.metadata,
        }


@dataclass
class SearchResult:
    """検索結果全体を表すデータクラス"""
    source: str  # "map" or "forest"
    hits: list[SearchHit]
    skip_forest: bool = False
    confidence: str = "medium"  # "high", "medium", "low"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "hits": [h.to_dict() for h in self.hits],
            "skip_forest": self.skip_forest,
            "confidence": self.confidence,
        }


class ChromaDBManager:
    """プロジェクトごとの ChromaDB 管理"""

    # Short-circuit のスコア閾値
    MAP_SHORTCIRCUIT_THRESHOLD = 0.7

    def __init__(
        self,
        project_root: str | Path,
        config: dict[str, Any] | None = None,
    ):
        self.project_root = Path(project_root)
        self.code_intel_dir = self.project_root / ".code-intel"
        self.db_path = self.code_intel_dir / "chroma"
        self.config = config or self._load_config()

        # 同期状態マネージャー
        self.sync_state = SyncStateManager(self.project_root)

        # AST チャンカー
        self.chunker = ASTChunker(self.config)

        # ChromaDB クライアント（遅延初期化）
        self._client: Any = None
        self._map_collection: Any = None
        self._forest_collection: Any = None

    def _load_config(self) -> dict[str, Any]:
        """設定ファイルを読み込み"""
        config_file = self.code_intel_dir / "config.json"
        if config_file.exists():
            try:
                return json.loads(config_file.read_text(encoding='utf-8'))
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to load config: {e}")
        return self._default_config()

    def _default_config(self) -> dict[str, Any]:
        """デフォルト設定"""
        return {
            "embedding_model": "multilingual-e5-small",
            "source_dirs": ["app", "src", "lib"],
            "exclude_patterns": [
                "**/node_modules/**",
                "**/__pycache__/**",
                "**/venv/**",
                "**/vendor/**",
            ],
            "chunk_strategy": "ast",
            "chunk_max_tokens": 512,
            "sync_ttl_hours": 1,
            "sync_on_start": True,
            "max_chunks": 10000,
            "search_weights": {
                "vector": 0.4,
                "keyword": 0.2,
                "definition": 0.3,
                "reference": 0.1,
            },
        }

    def _ensure_client(self) -> None:
        """ChromaDB クライアントを初期化"""
        if self._client is not None:
            return

        if not CHROMADB_AVAILABLE:
            raise RuntimeError("chromadb is not installed")

        self.db_path.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=str(self.db_path),
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True,
            )
        )

        # コレクション初期化
        self._map_collection = self._client.get_or_create_collection(
            name="map",
            metadata={"description": "Agreements - successful NL→Symbol pairs"}
        )
        self._forest_collection = self._client.get_or_create_collection(
            name="forest",
            metadata={"description": "Source code chunks"}
        )

        logger.info(f"ChromaDB initialized at {self.db_path}")

    @property
    def client(self) -> Any:
        """ChromaDB クライアントを取得"""
        self._ensure_client()
        return self._client

    @property
    def map_collection(self) -> Any:
        """map コレクションを取得"""
        self._ensure_client()
        return self._map_collection

    @property
    def forest_collection(self) -> Any:
        """forest コレクションを取得"""
        self._ensure_client()
        return self._forest_collection

    # =========================================================================
    # Sync Operations
    # =========================================================================

    def sync_forest(self, force: bool = False) -> SyncResult:
        """
        ソースコードを forest にインデックス

        Args:
            force: True なら全ファイルを再インデックス
        """
        start_time = time.time()
        result = SyncResult()

        source_dirs = self.config.get("source_dirs", ["app", "src"])
        exclude_patterns = self.config.get("exclude_patterns", [
            "**/node_modules/**",
            "**/__pycache__/**",
            "**/venv/**",
            "**/vendor/**",
            "**/.git/**",
            "**/.code-intel/**",
            "**/*.pyc",
        ])

        def should_exclude(file_path: Path) -> bool:
            """Check if file should be excluded"""
            for pattern in exclude_patterns:
                if file_path.match(pattern):
                    return True
            return False

        # 変更検出
        if force:
            # 強制再インデックス: 全ファイルを追加扱い
            added = []
            for dir_name in source_dirs:
                dir_path = self.project_root / dir_name
                if dir_path.exists():
                    for f in dir_path.rglob("*"):
                        if f.is_file() and not should_exclude(f):
                            added.append(f)
            modified = []
            deleted = []
        else:
            added, modified, deleted = self.sync_state.get_changed_files(
                source_dirs=[self.project_root / d for d in source_dirs],
                exclude_patterns=exclude_patterns,
            )

        logger.info(f"Sync: {len(added)} added, {len(modified)} modified, {len(deleted)} deleted")

        # v1.7: Invalidate ctags cache for changed/deleted files
        try:
            from code_intel_server import get_ctags_cache_manager
            ctags_cache = get_ctags_cache_manager(self.project_root)
            for file_path in modified:
                ctags_cache.invalidate_file(file_path)
            for rel_path in deleted:
                # Reconstruct Path from relative path
                file_path = self.project_root / rel_path
                ctags_cache.invalidate_file(file_path)
        except Exception as e:
            logger.debug(f"Failed to invalidate ctags cache: {e}")

        # 削除されたファイルのチャンクを削除
        for rel_path in deleted:
            try:
                self._delete_chunks_for_file(rel_path)
                self.sync_state.mark_deleted(rel_path)
                result.deleted += 1
            except Exception as e:
                logger.error(f"Failed to delete chunks for {rel_path}: {e}")
                result.errors += 1

        # v1.9: 変更されたファイルをバッチ処理で再インデックス
        if modified:
            # 古いチャンクを一括削除
            for file_path in modified:
                try:
                    rel_path = self.sync_state.get_relative_path(file_path)
                    self._delete_chunks_for_file(rel_path)
                except Exception as e:
                    logger.error(f"Failed to delete chunks for {file_path}: {e}")
                    result.errors += 1

            # バッチでインデックス
            file_chunk_counts = self._index_files_batch(modified)
            for file_path, chunk_count in file_chunk_counts.items():
                try:
                    self.sync_state.mark_indexed(file_path, chunk_count)
                    result.modified += 1
                except Exception as e:
                    logger.error(f"Failed to mark indexed {file_path}: {e}")
                    result.errors += 1

        # v1.9: 新規ファイルをバッチ処理でインデックス
        if added:
            file_chunk_counts = self._index_files_batch(added)
            for file_path, chunk_count in file_chunk_counts.items():
                try:
                    self.sync_state.mark_indexed(file_path, chunk_count)
                    result.added += 1
                except Exception as e:
                    logger.error(f"Failed to mark indexed {file_path}: {e}")
                    result.errors += 1

        # 同期完了時刻を記録
        self.sync_state.mark_sync_completed()

        result.duration_ms = (time.time() - start_time) * 1000
        logger.info(f"Sync completed in {result.duration_ms:.0f}ms: {result.to_dict()}")

        return result

    def sync_map(self) -> SyncResult:
        """agreements を map にインデックス"""
        start_time = time.time()
        result = SyncResult()

        agreements_dir = self.code_intel_dir / "agreements"
        if not agreements_dir.exists():
            agreements_dir.mkdir(parents=True, exist_ok=True)
            return result

        for md_file in agreements_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding='utf-8')
                self._upsert_to_map(md_file.stem, content)
                result.added += 1
            except Exception as e:
                logger.error(f"Failed to index agreement {md_file}: {e}")
                result.errors += 1

        result.duration_ms = (time.time() - start_time) * 1000
        return result

    def _sanitize_metadata(self, metadata: dict) -> dict:
        """ChromaDB 用にメタデータをサニタイズ（リスト/dict を JSON 文字列に変換）"""
        sanitized = {}
        for key, value in metadata.items():
            if value is None:
                continue  # None はスキップ
            elif isinstance(value, (str, int, float, bool)):
                sanitized[key] = value
            elif isinstance(value, (list, dict)):
                sanitized[key] = json.dumps(value, ensure_ascii=False)
            else:
                sanitized[key] = str(value)
        return sanitized

    def _index_file(self, file_path: Path) -> int:
        """単一ファイルをインデックス"""
        chunks = self.chunker.chunk_file(file_path)

        if not chunks:
            return 0

        # ChromaDB に追加
        ids = [chunk.id for chunk in chunks]
        documents = [chunk.content for chunk in chunks]
        metadatas = [
            self._sanitize_metadata({
                **chunk.metadata,
                "type": chunk.type,
                "name": chunk.name,
                "file": chunk.file,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
            })
            for chunk in chunks
        ]

        # ID の重複を除去
        seen_ids = set()
        unique_data = []
        for i, id_ in enumerate(ids):
            if id_ not in seen_ids:
                seen_ids.add(id_)
                unique_data.append((ids[i], documents[i], metadatas[i]))

        if unique_data:
            self.forest_collection.upsert(
                ids=[d[0] for d in unique_data],
                documents=[d[1] for d in unique_data],
                metadatas=[d[2] for d in unique_data],
            )

        return len(unique_data)

    def _index_files_batch(self, file_paths: list[Path]) -> dict[Path, int]:
        """
        v1.9: 複数ファイルをバッチ処理でインデックス

        Args:
            file_paths: インデックス対象のファイルパスリスト

        Returns:
            ファイルパスとチャンク数のマッピング
        """
        if not file_paths:
            return {}

        # 全ファイルのチャンクを収集
        all_ids = []
        all_documents = []
        all_metadatas = []
        file_chunk_counts = {}

        for file_path in file_paths:
            try:
                chunks = self.chunker.chunk_file(file_path)

                if not chunks:
                    file_chunk_counts[file_path] = 0
                    continue

                # チャンクをリストに追加
                for chunk in chunks:
                    all_ids.append(chunk.id)
                    all_documents.append(chunk.content)
                    all_metadatas.append(
                        self._sanitize_metadata({
                            **chunk.metadata,
                            "type": chunk.type,
                            "name": chunk.name,
                            "file": chunk.file,
                            "line_start": chunk.line_start,
                            "line_end": chunk.line_end,
                        })
                    )

                file_chunk_counts[file_path] = len(chunks)

            except Exception as e:
                logger.error(f"Failed to chunk {file_path}: {e}")
                file_chunk_counts[file_path] = 0

        # ID の重複を除去
        seen_ids = set()
        unique_data = []
        for i, id_ in enumerate(all_ids):
            if id_ not in seen_ids:
                seen_ids.add(id_)
                unique_data.append((all_ids[i], all_documents[i], all_metadatas[i]))

        # バッチで一括追加
        if unique_data:
            self.forest_collection.upsert(
                ids=[d[0] for d in unique_data],
                documents=[d[1] for d in unique_data],
                metadatas=[d[2] for d in unique_data],
            )

        return file_chunk_counts

    def _delete_chunks_for_file(self, rel_path: str) -> None:
        """特定ファイルのチャンクを削除"""
        # file メタデータでフィルタリングして削除
        try:
            self.forest_collection.delete(
                where={"file": {"$contains": rel_path}}
            )
        except Exception:
            # フィルタリングが効かない場合は ID ベースで削除
            pass

    def _upsert_to_map(self, doc_id: str, content: str, metadata: dict | None = None) -> None:
        """map コレクションにドキュメントを追加/更新"""
        # ChromaDB requires non-empty metadata
        final_metadata = metadata.copy() if metadata else {}
        if not final_metadata:
            final_metadata = {"source": "agreement", "doc_id": doc_id}

        self.map_collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[final_metadata],
        )

    # =========================================================================
    # Search Operations
    # =========================================================================

    def search_map(self, query: str, n_results: int = 5) -> list[SearchHit]:
        """地図（agreements）を検索"""
        try:
            results = self.map_collection.query(
                query_texts=[query],
                n_results=n_results,
            )
            return self._to_search_hits(results)
        except Exception as e:
            logger.error(f"Map search failed: {e}")
            return []

    def search_forest(self, query: str, n_results: int = 10) -> list[SearchHit]:
        """森（ソースコード）を検索"""
        try:
            results = self.forest_collection.query(
                query_texts=[query],
                n_results=n_results,
            )
            return self._to_search_hits(results)
        except Exception as e:
            logger.error(f"Forest search failed: {e}")
            return []

    def search(
        self,
        query: str,
        target_feature: str | None = None,
        collection: str = "auto",
        n_results: int = 10,
    ) -> SearchResult:
        """
        統合検索（Short-circuit ロジック付き）

        Args:
            query: 検索クエリ
            target_feature: ターゲット機能（QueryFrame から）
            collection: "map", "forest", or "auto"
            n_results: 結果数

        Returns:
            SearchResult
        """
        search_query = f"{query} {target_feature}" if target_feature else query

        # collection 指定がある場合
        if collection == "map":
            hits = self.search_map(search_query, n_results)
            return SearchResult(source="map", hits=hits)

        if collection == "forest":
            hits = self.search_forest(search_query, n_results)
            return SearchResult(source="forest", hits=hits)

        # auto: Short-circuit ロジック
        # 1. 地図を検索
        map_hits = self.search_map(search_query, n_results=5)

        # 2. 高スコアなら森をスキップ
        if map_hits and map_hits[0].score >= self.MAP_SHORTCIRCUIT_THRESHOLD:
            return SearchResult(
                source="map",
                hits=map_hits,
                skip_forest=True,
                confidence="high",
            )

        # 3. 森を検索
        forest_hits = self.search_forest(search_query, n_results)

        # 結果をマージ（地図のヒットも参考情報として含める）
        return SearchResult(
            source="forest",
            hits=forest_hits,
            skip_forest=False,
            confidence="medium" if forest_hits else "low",
        )

    def _to_search_hits(self, results: dict) -> list[SearchHit]:
        """ChromaDB の結果を SearchHit リストに変換"""
        hits = []

        if not results or not results.get("ids") or not results["ids"][0]:
            return hits

        ids = results["ids"][0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for i, id_ in enumerate(ids):
            # ChromaDB の distance を score に変換（小さいほど良い → 大きいほど良いに）
            # L2 distance の場合: score = 1 / (1 + distance)
            distance = distances[i] if i < len(distances) else 1.0
            score = 1.0 / (1.0 + distance)

            hits.append(SearchHit(
                id=id_,
                content=documents[i] if i < len(documents) else "",
                score=score,
                metadata=metadatas[i] if i < len(metadatas) else {},
            ))

        return hits

    # =========================================================================
    # Agreement Operations
    # =========================================================================

    def add_agreement(
        self,
        nl_term: str,
        symbols: list[str],
        code_evidence: str,
        session_id: str,
        similarity: float = 0.0,
    ) -> Path:
        """
        新しい agreement を追加

        Returns:
            作成された agreement ファイルのパス
        """
        agreements_dir = self.code_intel_dir / "agreements"
        agreements_dir.mkdir(parents=True, exist_ok=True)

        # ファイル名を生成
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_term = "".join(c if c.isalnum() else "_" for c in nl_term[:30])
        filename = f"{timestamp}_{safe_term}.md"
        file_path = agreements_dir / filename

        # Markdown コンテンツを生成
        content = f"""---
doc_type: agreement
nl_term: {nl_term}
symbols: {json.dumps(symbols)}
similarity: {similarity:.2f}
session_id: {session_id}
created_at: {datetime.now().isoformat()}
---

# {nl_term} → {', '.join(symbols)}

## 根拠 (Code Evidence)

{code_evidence}

## 関連シンボル

{chr(10).join(f'- `{s}`' for s in symbols)}
"""

        file_path.write_text(content, encoding='utf-8')

        # map コレクションに追加
        # Note: ChromaDB metadata doesn't support lists, so convert to JSON string
        self._upsert_to_map(
            doc_id=file_path.stem,
            content=content,
            metadata={
                "nl_term": nl_term,
                "symbols": json.dumps(symbols),  # Convert list to JSON string
                "similarity": similarity,
                "session_id": session_id,
            }
        )

        logger.info(f"Agreement created: {file_path}")
        return file_path

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def get_stats(self) -> dict[str, Any]:
        """統計情報を取得"""
        try:
            map_count = self.map_collection.count()
            forest_count = self.forest_collection.count()
        except Exception:
            map_count = 0
            forest_count = 0

        sync_stats = self.sync_state.get_stats()

        return {
            "map_count": map_count,
            "forest_count": forest_count,
            "sync": sync_stats,
            "config": {
                "source_dirs": self.config.get("source_dirs"),
                "embedding_model": self.config.get("embedding_model"),
            }
        }

    def needs_sync(self) -> bool:
        """同期が必要かどうか"""
        ttl_hours = self.config.get("sync_ttl_hours", 1)
        return self.sync_state.needs_sync(ttl_hours)

    def reset(self) -> None:
        """すべてのデータをリセット"""
        if self._client:
            self._client.reset()
            self._client = None
            self._map_collection = None
            self._forest_collection = None

        self.sync_state.clear()

        # DB ディレクトリを削除
        import shutil
        if self.db_path.exists():
            shutil.rmtree(self.db_path)

        logger.info("ChromaDB reset completed")

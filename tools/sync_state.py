"""
Sync State Manager for Code Intelligence MCP Server v3.9

ファイルごとの同期状態を管理し、指紋（SHA256）ベースの増分同期を実現する。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FileFingerprint:
    """ファイルの同期状態を表すデータクラス"""
    path: str
    hash: str
    mtime: float
    indexed_at: str
    chunk_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileFingerprint:
        return cls(**data)


@dataclass
class SyncResult:
    """同期結果を表すデータクラス"""
    added: int = 0
    modified: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def total_processed(self) -> int:
        return self.added + self.modified + self.deleted

    @property
    def has_changes(self) -> bool:
        return self.added > 0 or self.modified > 0 or self.deleted > 0


class SyncStateManager:
    """ファイルごとの同期状態を管理"""

    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root)
        self.code_intel_dir = self.project_root / ".code-intel"
        self.state_file = self.code_intel_dir / "sync_state.json"
        self.last_sync_file = self.code_intel_dir / ".last_sync"
        self.state: dict[str, FileFingerprint] = {}
        self._load_state()

    def _ensure_directory(self) -> None:
        """ディレクトリを確保"""
        self.code_intel_dir.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> None:
        """同期状態をファイルから読み込み"""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding='utf-8'))
                self.state = {
                    k: FileFingerprint.from_dict(v)
                    for k, v in data.items()
                }
                logger.debug(f"Loaded sync state: {len(self.state)} files")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load sync state: {e}")
                self.state = {}
        else:
            self.state = {}

    def _save_state(self) -> None:
        """同期状態をファイルに保存"""
        self._ensure_directory()
        data = {k: v.to_dict() for k, v in self.state.items()}
        self.state_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )

    def compute_hash(self, file_path: Path) -> str:
        """ファイルの SHA256 ハッシュを計算（先頭16文字）"""
        try:
            content = file_path.read_bytes()
            return hashlib.sha256(content).hexdigest()[:16]
        except Exception as e:
            logger.warning(f"Failed to compute hash for {file_path}: {e}")
            return ""

    def get_relative_path(self, file_path: Path) -> str:
        """プロジェクトルートからの相対パスを取得"""
        try:
            return str(file_path.relative_to(self.project_root))
        except ValueError:
            return str(file_path)

    def get_changed_files(
        self,
        source_dirs: list[Path | str],
        extensions: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> tuple[list[Path], list[Path], list[str]]:
        """
        変更されたファイルを検出

        Returns:
            (added, modified, deleted) のタプル
        """
        if extensions is None:
            extensions = [".py", ".php", ".js", ".ts", ".html", ".css", ".blade.php", ".json"]

        if exclude_patterns is None:
            exclude_patterns = [
                "**/node_modules/**",
                "**/__pycache__/**",
                "**/venv/**",
                "**/vendor/**",
                "**/.git/**",
                "**/.code-intel/**",
            ]

        added: list[Path] = []
        modified: list[Path] = []
        seen_paths: set[str] = set()

        for dir_path in source_dirs:
            dir_path = Path(dir_path)
            if not dir_path.is_absolute():
                dir_path = self.project_root / dir_path

            if not dir_path.exists():
                logger.warning(f"Source directory not found: {dir_path}")
                continue

            for ext in extensions:
                pattern = f"**/*{ext}" if not ext.startswith("*") else ext
                for file_path in dir_path.glob(pattern):
                    if not file_path.is_file():
                        continue

                    # 除外パターンをチェック
                    skip = False
                    rel_path_str = str(file_path.relative_to(self.project_root))
                    for exclude in exclude_patterns:
                        # Path.match() は ** パターンを正しく処理できないため、
                        # より柔軟なマッチングを実装
                        from fnmatch import fnmatch
                        from pathlib import PurePosixPath

                        # fnmatch で試す
                        if fnmatch(rel_path_str, exclude):
                            skip = True
                            break

                        # **/dir/** パターンの場合、パスに dir が含まれるか簡易チェック
                        if exclude.startswith("**/") and exclude.endswith("/**"):
                            dir_name = exclude[3:-3]  # **/ と /** を除去
                            path_parts = PurePosixPath(rel_path_str).parts
                            if dir_name in path_parts:
                                skip = True
                                break
                    if skip:
                        continue

                    rel_path = self.get_relative_path(file_path)
                    seen_paths.add(rel_path)

                    current_hash = self.compute_hash(file_path)
                    if not current_hash:
                        continue

                    if rel_path not in self.state:
                        added.append(file_path)
                    elif self.state[rel_path].hash != current_hash:
                        modified.append(file_path)

        # 削除されたファイルを検出
        deleted = [
            path for path in self.state.keys()
            if path not in seen_paths
        ]

        return added, modified, deleted

    def mark_indexed(self, file_path: Path, chunk_count: int = 0) -> None:
        """インデックス完了をマーク"""
        rel_path = self.get_relative_path(file_path)
        self.state[rel_path] = FileFingerprint(
            path=rel_path,
            hash=self.compute_hash(file_path),
            mtime=file_path.stat().st_mtime,
            indexed_at=datetime.now().isoformat(),
            chunk_count=chunk_count,
        )
        self._save_state()

    def mark_deleted(self, rel_path: str) -> None:
        """削除されたファイルを状態から除去"""
        if rel_path in self.state:
            del self.state[rel_path]
            self._save_state()

    def mark_sync_completed(self) -> None:
        """同期完了時刻を記録"""
        self._ensure_directory()
        self.last_sync_file.write_text(
            datetime.now().isoformat(),
            encoding='utf-8'
        )

    def get_last_sync_time(self) -> datetime | None:
        """最後の同期時刻を取得"""
        if self.last_sync_file.exists():
            try:
                return datetime.fromisoformat(
                    self.last_sync_file.read_text(encoding='utf-8').strip()
                )
            except ValueError:
                return None
        return None

    def needs_sync(self, ttl_hours: float = 1.0) -> bool:
        """
        同期が必要かどうかを判定

        Args:
            ttl_hours: 前回の同期からの経過時間（時間）

        Returns:
            同期が必要なら True
        """
        last_sync = self.get_last_sync_time()
        if last_sync is None:
            return True

        from datetime import timedelta
        elapsed = datetime.now() - last_sync
        return elapsed > timedelta(hours=ttl_hours)

    def get_stats(self) -> dict[str, Any]:
        """同期状態の統計を取得"""
        if not self.state:
            return {
                "total_files": 0,
                "total_chunks": 0,
                "last_sync": None,
            }

        total_chunks = sum(fp.chunk_count for fp in self.state.values())

        return {
            "total_files": len(self.state),
            "total_chunks": total_chunks,
            "last_sync": self.get_last_sync_time().isoformat() if self.get_last_sync_time() else None,
        }

    def clear(self) -> None:
        """同期状態をクリア"""
        self.state = {}
        if self.state_file.exists():
            self.state_file.unlink()
        if self.last_sync_file.exists():
            self.last_sync_file.unlink()

"""
Persistent Ctags Cache Manager

Caches ctags output per file with SHA256-based invalidation.
Similar to SyncStateManager pattern.
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
class CtagsFileCache:
    """Ctags output for a single file"""
    file_path: str
    hash: str  # SHA256 hash (first 16 chars)
    tags: list[dict]  # Parsed ctags JSON output
    cached_at: str
    language: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CtagsFileCache:
        return cls(**data)


class CtagsCacheManager:
    """Manages persistent ctags cache per project"""

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.cache_dir = self.project_root / ".code-intel" / "ctags_cache"
        self.cache_index_file = self.cache_dir / "cache_index.json"
        self.cache: dict[str, CtagsFileCache] = {}
        self._load_cache()

    def _ensure_directory(self) -> None:
        """Create cache directory if needed"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _load_cache(self) -> None:
        """Load cache index from disk"""
        if self.cache_index_file.exists():
            try:
                data = json.loads(self.cache_index_file.read_text(encoding='utf-8'))
                self.cache = {
                    k: CtagsFileCache.from_dict(v)
                    for k, v in data.items()
                }
                logger.debug(f"Loaded ctags cache: {len(self.cache)} files")
            except Exception as e:
                logger.warning(f"Failed to load ctags cache: {e}")
                self.cache = {}
        else:
            self.cache = {}

    def _save_cache(self) -> None:
        """Save cache index to disk"""
        self._ensure_directory()
        data = {k: v.to_dict() for k, v in self.cache.items()}
        self.cache_index_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )

    def compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash (first 16 chars)"""
        try:
            content = file_path.read_bytes()
            return hashlib.sha256(content).hexdigest()[:16]
        except Exception as e:
            logger.warning(f"Failed to compute hash for {file_path}: {e}")
            return ""

    def get_relative_path(self, file_path: Path) -> str:
        """Get relative path from project root"""
        try:
            return str(file_path.relative_to(self.project_root))
        except ValueError:
            return str(file_path)

    def get_cached_tags(
        self,
        file_path: Path,
        language: str | None = None
    ) -> list[dict] | None:
        """Get cached tags for a file if valid"""
        rel_path = self.get_relative_path(file_path)

        if rel_path not in self.cache:
            return None

        cached = self.cache[rel_path]

        # Validate hash
        current_hash = self.compute_hash(file_path)
        if not current_hash or current_hash != cached.hash:
            # Hash mismatch - invalidate
            logger.debug(f"Ctags cache invalidated: {rel_path} (hash mismatch)")
            return None

        # Validate language if specified
        if language and cached.language != language:
            return None

        logger.debug(f"Ctags cache hit: {rel_path}")
        return cached.tags

    def cache_tags(
        self,
        file_path: Path,
        tags: list[dict],
        language: str | None = None
    ) -> None:
        """Cache tags for a file"""
        rel_path = self.get_relative_path(file_path)

        self.cache[rel_path] = CtagsFileCache(
            file_path=rel_path,
            hash=self.compute_hash(file_path),
            tags=tags,
            cached_at=datetime.now().isoformat(),
            language=language,
        )

        self._save_cache()
        logger.debug(f"Ctags cached: {rel_path} ({len(tags)} tags)")

    def invalidate_file(self, file_path: Path) -> None:
        """Invalidate cache for a file"""
        rel_path = self.get_relative_path(file_path)
        if rel_path in self.cache:
            del self.cache[rel_path]
            self._save_cache()
            logger.debug(f"Ctags cache invalidated: {rel_path}")

    def clear(self) -> None:
        """Clear entire cache"""
        self.cache = {}
        if self.cache_index_file.exists():
            self.cache_index_file.unlink()
        logger.info("Ctags cache cleared")

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics"""
        total_tags = sum(len(entry.tags) for entry in self.cache.values())
        return {
            "cached_files": len(self.cache),
            "total_tags": total_tags,
            "cache_dir": str(self.cache_dir),
        }

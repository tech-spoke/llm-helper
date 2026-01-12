"""
Agreements Manager for v3.8.

v3.8: 成功した NL→Symbol ペアを Markdown 形式で保存し、
devrag-map で検索可能にする。
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from tools.embedding import EmbeddingValidator


@dataclass
class AgreementData:
    """合意事項のデータ構造"""
    nl_term: str
    symbol: str
    similarity: float
    code_evidence: Optional[str]
    session_id: str
    intent: str
    related_files: list[str]
    query_frame_summary: Optional[dict] = None

    def to_frontmatter(self) -> dict:
        """YAML frontmatter 用の辞書を生成"""
        symbol_normalized = EmbeddingValidator.split_camel_case(self.symbol)
        return {
            "doc_type": "agreement",
            "nl_term": self.nl_term,
            "symbol": self.symbol,
            "symbol_normalized": symbol_normalized,
            "similarity": round(self.similarity, 3),
            "session_id": self.session_id,
            "intent": self.intent,
            "learned_at": datetime.now().isoformat(),
        }


def generate_agreement_markdown(data: AgreementData) -> str:
    """
    合意事項の Markdown を生成。

    devrag-map がインデックス化できる形式で出力。
    """
    symbol_normalized = EmbeddingValidator.split_camel_case(data.symbol)

    # YAML frontmatter
    frontmatter_lines = [
        "---",
        "doc_type: agreement",
        f"nl_term: {data.nl_term}",
        f"symbol: {data.symbol}",
        f"symbol_normalized: {symbol_normalized}",
        f"similarity: {data.similarity:.3f}",
        f"session_id: {data.session_id}",
        f"intent: {data.intent}",
        f"learned_at: {datetime.now().isoformat()}",
        "---",
    ]

    # 本文
    body_lines = [
        "",
        f"# {data.nl_term} → {data.symbol}",
        "",
        f"**シンボル（分解）**: {symbol_normalized}",
        "",
        "## 根拠 (Code Evidence)",
        "",
    ]

    if data.code_evidence:
        body_lines.append(data.code_evidence)
    else:
        body_lines.append("（根拠なし）")

    body_lines.extend([
        "",
        "## 関連ファイル",
        "",
    ])

    if data.related_files:
        for f in data.related_files:
            body_lines.append(f"- `{f}`")
    else:
        body_lines.append("（なし）")

    # QueryFrame サマリー
    if data.query_frame_summary:
        body_lines.extend([
            "",
            "## QueryFrame",
            "",
        ])
        for key, value in data.query_frame_summary.items():
            if value:
                body_lines.append(f"- **{key}**: {value}")

    return "\n".join(frontmatter_lines + body_lines) + "\n"


def sanitize_filename(text: str, max_length: int = 50) -> str:
    """ファイル名に使える形式に変換"""
    # 日本語や特殊文字を除去し、スペースをアンダースコアに
    sanitized = re.sub(r'[^\w\s-]', '', text)
    sanitized = re.sub(r'\s+', '_', sanitized)
    sanitized = sanitized.strip('_')

    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]

    return sanitized or "unnamed"


class AgreementsManager:
    """
    合意事項（agreements/）の管理。

    - Markdown ファイルの生成・保存
    - devrag-map との同期
    """

    AGREEMENTS_DIR = ".code-intel/agreements"

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        self.agreements_dir = self.project_root / self.AGREEMENTS_DIR

    def _ensure_dir(self) -> None:
        """agreements ディレクトリを作成"""
        self.agreements_dir.mkdir(parents=True, exist_ok=True)

    def save_agreement(self, data: AgreementData) -> Path:
        """
        合意事項を Markdown として保存。

        Returns:
            保存されたファイルのパス
        """
        self._ensure_dir()

        # ファイル名: {nl_term}_{symbol}.md
        filename = f"{sanitize_filename(data.nl_term)}_{sanitize_filename(data.symbol)}.md"
        filepath = self.agreements_dir / filename

        # Markdown 生成
        content = generate_agreement_markdown(data)

        # 保存（上書き）
        filepath.write_text(content, encoding="utf-8")

        return filepath

    def list_agreements(self) -> list[dict]:
        """保存されている合意事項の一覧を取得"""
        if not self.agreements_dir.exists():
            return []

        agreements = []
        for md_file in self.agreements_dir.glob("*.md"):
            content = md_file.read_text(encoding="utf-8")

            # frontmatter をパース
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = parts[1].strip()
                    meta = {}
                    for line in frontmatter.split("\n"):
                        if ":" in line:
                            key, value = line.split(":", 1)
                            meta[key.strip()] = value.strip()

                    agreements.append({
                        "file": md_file.name,
                        "path": str(md_file),
                        **meta,
                    })

        return agreements

    def find_by_nl_term(self, nl_term: str) -> list[dict]:
        """NL用語で合意事項を検索（完全一致）"""
        return [
            a for a in self.list_agreements()
            if a.get("nl_term") == nl_term
        ]

    def delete_agreement(self, filename: str) -> bool:
        """合意事項を削除"""
        filepath = self.agreements_dir / filename
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    async def trigger_devrag_sync(self) -> dict:
        """
        devrag-map の再インデックスをトリガー。

        Returns:
            sync 結果
        """
        config_path = self.project_root / "devrag-map.json"

        if not config_path.exists():
            return {
                "success": False,
                "error": "devrag-map.json not found",
                "hint": "Run setup.sh to generate config files",
            }

        try:
            process = await asyncio.create_subprocess_exec(
                "devrag",
                "--config", str(config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0,
            )

            return {
                "success": process.returncode == 0,
                "stdout": stdout.decode() if stdout else "",
                "stderr": stderr.decode() if stderr else "",
            }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": "devrag sync timed out (30s)",
            }
        except FileNotFoundError:
            return {
                "success": False,
                "error": "devrag command not found",
                "hint": "Install devrag: https://github.com/tomohiro-owada/devrag",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


# シングルトンインスタンス
_manager_instance: Optional[AgreementsManager] = None


def get_agreements_manager(project_root: str = ".") -> AgreementsManager:
    """AgreementsManager のシングルトンを取得"""
    global _manager_instance
    if _manager_instance is None or str(_manager_instance.project_root) != str(Path(project_root).resolve()):
        _manager_instance = AgreementsManager(project_root)
    return _manager_instance

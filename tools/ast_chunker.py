"""
AST-based Code Chunker for Code Intelligence MCP Server v3.9

tree-sitter を使用して、ソースコードを意味のある単位（関数、クラス等）に分割する。
言語ごとの Strategy パターンで、PHP/Filament/Laravel 等に特化した解析を行う。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Chunk:
    """コードチャンクを表すデータクラス"""
    id: str
    type: str
    name: str
    file: str
    content: str
    line_start: int = 0
    line_end: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """ChromaDB 格納用の辞書に変換"""
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "file": self.file,
            "content": self.content,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "metadata": self.metadata,
        }


# =============================================================================
# Language Detection
# =============================================================================

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".php": "php",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".vue": "vue",
}


def detect_language(file_path: Path) -> str:
    """ファイルパスから言語を検出"""
    name = file_path.name
    path_str = str(file_path)

    # 特殊ファイル
    if name == "composer.json":
        return "composer_json"
    if name in ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs"):
        return "tailwind_config"

    # 複合拡張子を先にチェック
    if name.endswith(".blade.php"):
        return "blade"

    # Filament Resource の判定
    if name.endswith("Resource.php") and "/Resources/" in path_str:
        return "filament_resource"

    # Laravel Migration の判定
    if "/migrations/" in path_str and name.endswith(".php"):
        return "migration"

    # 拡張子で判定
    suffix = file_path.suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(suffix, "unknown")


# =============================================================================
# Base Strategy
# =============================================================================

class ChunkStrategy(ABC):
    """言語ごとのチャンク戦略の基底クラス"""

    @abstractmethod
    def get_chunk_node_types(self) -> list[str]:
        """チャンク対象のノードタイプを返す"""
        pass

    @abstractmethod
    def extract_name(self, node: Any) -> str:
        """ノードから名前を抽出"""
        pass

    def extract_metadata(self, node: Any, file_path: Path, file_content: str) -> dict[str, Any]:
        """ノードからメタデータを抽出（オーバーライド可能）"""
        return {}

    def chunk_file(self, file_path: Path, parser: Any = None) -> list[Chunk]:
        """ファイルをチャンク化（サブクラスでオーバーライド可能）"""
        raise NotImplementedError("Subclass must implement chunk_file or use ASTChunker")


class FallbackChunkStrategy(ChunkStrategy):
    """未対応言語用: 行数ベースにフォールバック"""

    def __init__(self, max_lines: int = 50):
        self.max_lines = max_lines

    def get_chunk_node_types(self) -> list[str]:
        return []  # AST チャンクなし

    def extract_name(self, node: Any) -> str:
        return "unknown"

    def chunk_by_lines(self, content: str, file_path: Path) -> list[Chunk]:
        """行数ベースで分割"""
        lines = content.split("\n")
        chunks = []

        for i in range(0, len(lines), self.max_lines):
            chunk_lines = lines[i:i + self.max_lines]
            chunk_content = "\n".join(chunk_lines)

            if not chunk_content.strip():
                continue

            chunks.append(Chunk(
                id=f"{file_path}:lines_{i+1}_{min(i + self.max_lines, len(lines))}",
                type="lines",
                name=f"lines_{i+1}_{min(i + self.max_lines, len(lines))}",
                file=str(file_path),
                content=chunk_content,
                line_start=i + 1,
                line_end=min(i + self.max_lines, len(lines)),
            ))

        return chunks


# =============================================================================
# Python Strategy
# =============================================================================

class PythonChunkStrategy(ChunkStrategy):
    """Python 用チャンク戦略"""

    def get_chunk_node_types(self) -> list[str]:
        return ["function_definition", "class_definition"]

    def extract_name(self, node: Any) -> str:
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode()
        return "unknown"

    def extract_metadata(self, node: Any, file_path: Path, file_content: str) -> dict[str, Any]:
        metadata = {}

        # docstring を抽出
        docstring = self._extract_docstring(node)
        if docstring:
            metadata["docstring"] = docstring

        # デコレータを抽出
        decorators = self._extract_decorators(node)
        if decorators:
            metadata["decorators"] = decorators

        return metadata

    def _extract_docstring(self, node: Any) -> str | None:
        """docstring を抽出"""
        for child in node.children:
            if child.type == "block":
                for stmt in child.children:
                    if stmt.type == "expression_statement":
                        for expr in stmt.children:
                            if expr.type == "string":
                                return expr.text.decode().strip('"""\'\'\'')
        return None

    def _extract_decorators(self, node: Any) -> list[str]:
        """デコレータを抽出"""
        decorators = []
        # デコレータは function_definition の前にある
        # tree-sitter の構造によって異なる
        return decorators


# =============================================================================
# PHP Strategy
# =============================================================================

class PHPChunkStrategy(ChunkStrategy):
    """PHP 用チャンク戦略（Laravel/Translatable/DI 対応）"""

    def get_chunk_node_types(self) -> list[str]:
        return [
            "class_declaration",
            "function_definition",
            "method_declaration",
            "trait_declaration",
        ]

    def extract_name(self, node: Any) -> str:
        for child in node.children:
            if child.type == "name":
                return child.text.decode()
        return "unknown"

    def extract_metadata(self, node: Any, file_path: Path, file_content: str) -> dict[str, Any]:
        metadata = {}

        # FQCN を抽出
        fqcn = self._extract_fqcn(file_content)
        if fqcn:
            metadata["fqcn"] = fqcn

        # use 文を抽出
        imports = self._extract_use_statements(file_content)
        if imports:
            metadata["imports"] = imports

        # Translatable 検出
        translatable_fields = self._extract_translatable_fields(file_content)
        if translatable_fields:
            metadata["translatable"] = True
            metadata["translatable_fields"] = translatable_fields

        # Eloquent リレーション検出
        relations = self._extract_eloquent_relations(file_content)
        if relations:
            metadata["relations"] = relations

        # Constructor DI 検出
        dependencies = self._extract_constructor_dependencies(file_content)
        if dependencies:
            metadata["injected_dependencies"] = dependencies
            metadata["related_symbols"] = metadata.get("related_symbols", []) + [
                dep["class"] for dep in dependencies
            ]

        return metadata

    def _extract_fqcn(self, content: str) -> str | None:
        """namespace + class 名から FQCN を構築"""
        namespace_match = re.search(r'namespace\s+([\w\\]+);', content)
        class_match = re.search(r'class\s+(\w+)', content)

        if class_match:
            class_name = class_match.group(1)
            if namespace_match:
                return f"{namespace_match.group(1)}\\{class_name}"
            return class_name
        return None

    def _extract_use_statements(self, content: str) -> list[str]:
        """use 文を抽出"""
        return re.findall(r'use\s+([\w\\]+)(?:\s+as\s+\w+)?;', content)

    def _extract_translatable_fields(self, content: str) -> list[str] | None:
        """$translatable プロパティから翻訳可能フィールドを抽出"""
        # use HasTranslations; があるか確認
        if "HasTranslations" not in content and "Translatable" not in content:
            return None

        match = re.search(r'\$translatable\s*=\s*\[([^\]]+)\]', content)
        if not match:
            return None

        fields_str = match.group(1)
        fields = re.findall(r"['\"](\w+)['\"]", fields_str)
        return fields if fields else None

    def _extract_eloquent_relations(self, content: str) -> list[dict[str, str]]:
        """Eloquent リレーションを抽出（出現順序を維持）"""
        relations = []

        # 統合パターン: すべてのリレーションタイプを一度にマッチ
        pattern = r'function\s+(\w+)\s*\(\)[^{]*\{\s*return\s+\$this->(hasMany|belongsTo|belongsToMany|hasOne|morphMany|morphTo|morphOne|hasManyThrough)\(([^)]*)\)'

        for match in re.finditer(pattern, content, re.DOTALL):
            method_name = match.group(1)
            relation_type = match.group(2)
            args = match.group(3)

            if relation_type == 'morphTo':
                related_class = 'dynamic'
            else:
                # 最初の引数（クラス名）を抽出
                related_class = args.split(',')[0].strip().strip("'\"")
                related_class = related_class.replace("::class", "")

            relations.append({
                "method": method_name,
                "type": relation_type,
                "related": related_class,
            })

        return relations

    def _extract_constructor_dependencies(self, content: str) -> list[dict[str, str]]:
        """コンストラクタのタイプヒントから依存クラスを抽出"""
        dependencies = []

        construct_match = re.search(
            r'function\s+__construct\s*\(([^)]*)\)',
            content,
            re.DOTALL
        )

        if not construct_match:
            return dependencies

        params_str = construct_match.group(1)
        param_pattern = r'(?:private|protected|public|readonly)?\s*(\w+)\s+\$(\w+)'

        for match in re.finditer(param_pattern, params_str):
            type_hint = match.group(1)
            var_name = match.group(2)

            # 基本型は除外
            if type_hint.lower() in ['string', 'int', 'float', 'bool', 'array', 'mixed', 'null', 'callable']:
                continue

            dependencies.append({
                "class": type_hint,
                "variable": var_name,
            })

        return dependencies


# =============================================================================
# Filament Resource Strategy
# =============================================================================

class FilamentResourceStrategy(PHPChunkStrategy):
    """Filament v4 Resource 専用 Strategy"""

    FILAMENT_METHODS = [
        "form",
        "table",
        "infolist",
        "getRelations",
        "getPages",
        "getActions",
        "getBulkActions",
        "getHeaderActions",
        "getTableActions",
        "getFormActions",
    ]

    def is_filament_resource(self, file_path: Path, content: str) -> bool:
        """Filament Resource かどうかを判定"""
        return (
            "extends Resource" in content or
            "extends BaseResource" in content or
            "/Resources/" in str(file_path)
        )

    def chunk_file(self, file_path: Path, parser: Any = None) -> list[Chunk]:
        """Filament Resource をチャンク化"""
        content = file_path.read_text(encoding='utf-8')

        if not self.is_filament_resource(file_path, content):
            # 通常の PHP として処理
            return []

        chunks = []

        # Resource クラス全体
        class_chunk = self._extract_resource_class(content, file_path)
        if class_chunk:
            chunks.append(class_chunk)

        # 各 Filament メソッド
        for method_name in self.FILAMENT_METHODS:
            method_chunk = self._extract_filament_method(content, file_path, method_name)
            if method_chunk:
                chunks.append(method_chunk)

        return chunks

    def _extract_resource_class(self, content: str, file_path: Path) -> Chunk | None:
        """Resource クラスのメタデータを抽出"""
        class_match = re.search(r'class\s+(\w+Resource)\s+extends', content)
        if not class_match:
            return None

        class_name = class_match.group(1)

        # Model を抽出
        model_match = re.search(r'\$model\s*=\s*(\w+)::class', content)
        model_name = model_match.group(1) if model_match else None

        # navigationIcon を抽出
        icon_match = re.search(r'\$navigationIcon\s*=\s*[\'"]([^\'"]+)[\'"]', content)
        icon = icon_match.group(1) if icon_match else None

        # navigationGroup を抽出
        group_match = re.search(r'\$navigationGroup\s*=\s*[\'"]([^\'"]+)[\'"]', content)
        group = group_match.group(1) if group_match else None

        # Translatable トレイト検出
        is_translatable = (
            "use Translatable;" in content or
            "Concerns\\Translatable" in content
        )

        # DI 検出
        dependencies = self._extract_constructor_dependencies(content)

        # クラス定義部分を抽出（最初の500文字程度）
        class_content = content[:500]

        return Chunk(
            id=f"filament:{class_name}",
            type="filament_resource",
            name=class_name,
            file=str(file_path),
            content=class_content,
            metadata={
                "fqcn": self._extract_fqcn(content),
                "model": model_name,
                "navigation_icon": icon,
                "navigation_group": group,
                "has_form": "function form(" in content,
                "has_table": "function table(" in content,
                "has_infolist": "function infolist(" in content,
                "is_translatable": is_translatable,
                "injected_dependencies": dependencies,
                "related_symbols": [dep["class"] for dep in dependencies] if dependencies else [],
            }
        )

    def _extract_filament_method(self, content: str, file_path: Path, method_name: str) -> Chunk | None:
        """Filament の特定メソッドを抽出"""
        # メソッド定義を検索（複数行対応）
        pattern = rf'public\s+static\s+function\s+{method_name}\s*\([^)]*\)[^{{]*\{{(.*?)\n    \}}'
        match = re.search(pattern, content, re.DOTALL)

        if not match:
            # non-static メソッドも試す
            pattern = rf'public\s+function\s+{method_name}\s*\([^)]*\)[^{{]*\{{(.*?)\n    \}}'
            match = re.search(pattern, content, re.DOTALL)

        if not match:
            return None

        method_content = match.group(0)

        # フィールド/カラム名を抽出
        fields = []
        if method_name == "form":
            fields = re.findall(r"::make\(['\"](\w+)['\"]\)", method_content)
        elif method_name == "table":
            fields = re.findall(r"Column::make\(['\"]([^'\"]+)['\"]\)", method_content)

        # Actions を抽出
        actions = []
        if "Action" in method_name or "action" in method_name.lower():
            action_matches = re.findall(r"(\w+Action)::make\(['\"]?(\w*)['\"]?\)", method_content)
            actions = [{"type": a[0], "name": a[1] or a[0]} for a in action_matches]

        class_match = re.search(r'class\s+(\w+Resource)', content)
        class_name = class_match.group(1) if class_match else "Unknown"

        # 行番号を計算
        method_start = content[:match.start()].count('\n') + 1

        return Chunk(
            id=f"filament:{class_name}:{method_name}",
            type=f"filament_{method_name}",
            name=f"{class_name}::{method_name}",
            file=str(file_path),
            content=method_content,
            line_start=method_start,
            metadata={
                "resource": class_name,
                "method": method_name,
                "fields": fields,
                "actions": actions,
            }
        )


# =============================================================================
# TypeScript/JavaScript Strategy
# =============================================================================

class TypeScriptChunkStrategy(ChunkStrategy):
    """TypeScript/JavaScript 用チャンク戦略"""

    def get_chunk_node_types(self) -> list[str]:
        return [
            "function_declaration",
            "class_declaration",
            "method_definition",
            "arrow_function",
            "export_statement",
        ]

    def extract_name(self, node: Any) -> str:
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode()
        return "unknown"


# =============================================================================
# HTML/Blade Strategy
# =============================================================================

class HTMLChunkStrategy(ChunkStrategy):
    """HTML 用チャンク戦略"""

    SEMANTIC_TAGS = ["section", "article", "nav", "header", "footer", "main", "form", "aside"]

    def get_chunk_node_types(self) -> list[str]:
        return ["element"]

    def extract_name(self, node: Any) -> str:
        tag_name = self._get_tag_name(node)
        id_attr = self._get_id_attribute(node)
        if id_attr:
            return f"{tag_name}#{id_attr}"
        return tag_name

    def should_chunk(self, node: Any) -> bool:
        """チャンク対象とする要素を判定"""
        if node.type != "element":
            return False

        tag_name = self._get_tag_name(node)

        # Blade コンポーネント
        if tag_name.startswith("x-"):
            return True

        # セマンティック要素
        if tag_name in self.SEMANTIC_TAGS:
            return True

        # id 属性がある要素
        if self._has_id_attribute(node):
            return True

        return False

    def _get_tag_name(self, node: Any) -> str:
        """タグ名を取得"""
        for child in node.children:
            if child.type == "tag_name":
                return child.text.decode()
        return "unknown"

    def _get_id_attribute(self, node: Any) -> str | None:
        """id 属性を取得"""
        # 実装は tree-sitter の構造に依存
        return None

    def _has_id_attribute(self, node: Any) -> bool:
        """id 属性があるか確認"""
        return self._get_id_attribute(node) is not None


class BladeChunkStrategy(HTMLChunkStrategy):
    """Laravel Blade 特化 Strategy"""

    def get_chunk_node_types(self) -> list[str]:
        return super().get_chunk_node_types() + ["directive"]

    def should_chunk(self, node: Any) -> bool:
        if node.type == "directive":
            directive_name = self._get_directive_name(node)
            return directive_name in ["section", "component", "slot", "push", "stack"]

        return super().should_chunk(node)

    def _get_directive_name(self, node: Any) -> str:
        """ディレクティブ名を取得"""
        # @section, @component 等
        return "unknown"

    def extract_metadata(self, node: Any, file_path: Path, file_content: str) -> dict[str, Any]:
        metadata = super().extract_metadata(node, file_path, file_content)

        # Blade から参照される PHP シンボルを抽出
        related = []

        # ルート呼び出し
        routes = re.findall(r"route\(['\"]([^'\"]+)['\"]\)", file_content)
        related.extend([f"route:{r}" for r in routes])

        # Blade コンポーネント
        components = re.findall(r'<x-([\w-]+)', file_content)
        related.extend([self._component_to_fqcn(c) for c in components])

        # Livewire コンポーネント
        livewire = re.findall(r"@livewire\(['\"]([^'\"]+)['\"]\)", file_content)
        related.extend([f"livewire:{lw}" for lw in livewire])

        if related:
            metadata["related_symbols"] = related

        return metadata

    def _component_to_fqcn(self, component_name: str) -> str:
        """user-card → App\\View\\Components\\UserCard"""
        parts = component_name.split('-')
        class_name = ''.join(p.capitalize() for p in parts)
        return f"App\\View\\Components\\{class_name}"


# =============================================================================
# CSS Strategy
# =============================================================================

class CSSChunkStrategy(ChunkStrategy):
    """CSS/SCSS 用チャンク戦略"""

    def get_chunk_node_types(self) -> list[str]:
        return [
            "rule_set",
            "media_statement",
            "keyframes_statement",
        ]

    def extract_name(self, node: Any) -> str:
        if node.type == "rule_set":
            for child in node.children:
                if child.type == "selectors":
                    return child.text.decode()[:50]
        return "unknown"


# =============================================================================
# Config File Strategies
# =============================================================================

class ComposerJsonStrategy(ChunkStrategy):
    """composer.json 解析用"""

    def get_chunk_node_types(self) -> list[str]:
        return []  # JSON は AST ではなく直接パース

    def extract_name(self, node: Any) -> str:
        return "composer.json"

    def chunk_file(self, file_path: Path, parser: Any = None) -> list[Chunk]:
        """composer.json をチャンク化"""
        try:
            content = json.loads(file_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse composer.json: {e}")
            return []

        chunks = []

        # require セクション
        if "require" in content:
            chunks.append(Chunk(
                id="composer:require",
                type="dependencies",
                name="production_dependencies",
                file=str(file_path),
                content=json.dumps(content["require"], indent=2),
                metadata={
                    "packages": list(content["require"].keys()),
                    "type": "production"
                }
            ))

        # require-dev セクション
        if "require-dev" in content:
            chunks.append(Chunk(
                id="composer:require-dev",
                type="dependencies",
                name="dev_dependencies",
                file=str(file_path),
                content=json.dumps(content["require-dev"], indent=2),
                metadata={
                    "packages": list(content["require-dev"].keys()),
                    "type": "development"
                }
            ))

        # autoload セクション
        if "autoload" in content:
            psr4 = content["autoload"].get("psr-4", {})
            if psr4:
                chunks.append(Chunk(
                    id="composer:autoload",
                    type="namespace_mapping",
                    name="psr4_autoload",
                    file=str(file_path),
                    content=json.dumps(psr4, indent=2),
                    metadata={
                        "namespaces": list(psr4.keys()),
                        "paths": list(psr4.values())
                    }
                ))

        return chunks


class TailwindConfigStrategy(ChunkStrategy):
    """tailwind.config.js 解析用"""

    def get_chunk_node_types(self) -> list[str]:
        return []  # 正規表現でパース

    def extract_name(self, node: Any) -> str:
        return "tailwind.config"

    def chunk_file(self, file_path: Path, parser: Any = None) -> list[Chunk]:
        """tailwind.config.js をチャンク化"""
        content = file_path.read_text(encoding='utf-8')
        chunks = []

        # テーマ設定を抽出
        theme_config = self._extract_theme(content)
        if theme_config:
            chunks.append(Chunk(
                id="tailwind:theme",
                type="ui_config",
                name="tailwind_theme",
                file=str(file_path),
                content=theme_config,
                metadata={
                    "colors": self._extract_colors(theme_config),
                }
            ))

        # content パス
        content_paths = self._extract_content_paths(content)
        if content_paths:
            chunks.append(Chunk(
                id="tailwind:content",
                type="ui_config",
                name="tailwind_content_paths",
                file=str(file_path),
                content=content_paths,
                metadata={
                    "scan_patterns": self._parse_content_paths(content_paths)
                }
            ))

        # plugins
        plugins = self._extract_plugins(content)
        if plugins:
            chunks.append(Chunk(
                id="tailwind:plugins",
                type="ui_config",
                name="tailwind_plugins",
                file=str(file_path),
                content=plugins,
            ))

        return chunks

    def _extract_theme(self, content: str) -> str | None:
        """theme 設定を抽出"""
        match = re.search(r'theme:\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}', content, re.DOTALL)
        return match.group(0) if match else None

    def _extract_colors(self, theme: str) -> dict[str, str]:
        """カスタムカラー定義を抽出"""
        colors = {}
        color_match = re.search(r"colors:\s*\{([^}]+)\}", theme)
        if color_match:
            for line in color_match.group(1).split(','):
                if ':' in line:
                    key, val = line.split(':', 1)
                    colors[key.strip().strip("'")] = val.strip().strip("',\"")
        return colors

    def _extract_content_paths(self, content: str) -> str | None:
        """content パスを抽出"""
        match = re.search(r'content:\s*\[([^\]]+)\]', content, re.DOTALL)
        return match.group(0) if match else None

    def _parse_content_paths(self, content_str: str) -> list[str]:
        """content パスをリストに変換"""
        return re.findall(r'["\']([^"\']+)["\']', content_str)

    def _extract_plugins(self, content: str) -> str | None:
        """plugins を抽出"""
        match = re.search(r'plugins:\s*\[([^\]]+)\]', content, re.DOTALL)
        return match.group(0) if match else None


# =============================================================================
# Migration Strategy
# =============================================================================

class MigrationChunkStrategy(PHPChunkStrategy):
    """Laravel マイグレーション用"""

    def chunk_file(self, file_path: Path, parser: Any = None) -> list[Chunk]:
        """マイグレーションファイルをチャンク化"""
        content = file_path.read_text(encoding='utf-8')
        chunks = []

        # テーブル作成を抽出
        tables = self._extract_table_definitions(content)
        for table in tables:
            chunks.append(Chunk(
                id=f"migration:{table['name']}",
                type="table_schema",
                name=table["name"],
                file=str(file_path),
                content=table["definition"],
                metadata={
                    "columns": table["columns"],
                    "indexes": table.get("indexes", []),
                    "foreign_keys": table.get("foreign_keys", []),
                    "migration_file": str(file_path),
                }
            ))

        return chunks

    def _extract_table_definitions(self, content: str) -> list[dict[str, Any]]:
        """Schema::create() からテーブル定義を抽出"""
        tables = []

        pattern = r"Schema::create\(['\"](\w+)['\"],\s*function.*?\{(.*?)\}\);"
        matches = re.findall(pattern, content, re.DOTALL)

        for table_name, definition in matches:
            tables.append({
                "name": table_name,
                "definition": definition,
                "columns": self._extract_columns(definition),
                "indexes": self._extract_indexes(definition),
                "foreign_keys": self._extract_foreign_keys(definition),
            })

        return tables

    def _extract_columns(self, definition: str) -> list[dict[str, str]]:
        """カラム定義を抽出"""
        columns = []
        pattern = r"\$table->(\w+)\(['\"](\w+)['\"]"
        for col_type, col_name in re.findall(pattern, definition):
            columns.append({"name": col_name, "type": col_type})
        return columns

    def _extract_indexes(self, definition: str) -> list[str]:
        """インデックスを抽出"""
        indexes = []
        # ->index(), ->unique() 等
        pattern = r"->(?:index|unique)\(['\"]?([^'\")\]]+)"
        indexes = re.findall(pattern, definition)
        return indexes

    def _extract_foreign_keys(self, definition: str) -> list[dict[str, str]]:
        """外部キーを抽出"""
        foreign_keys = []
        # $table->foreign('user_id')->references('id')->on('users')
        pattern = r"->foreign\(['\"](\w+)['\"]\)->references\(['\"](\w+)['\"]\)->on\(['\"](\w+)['\"]\)"
        for column, ref_column, ref_table in re.findall(pattern, definition):
            foreign_keys.append({
                "column": column,
                "references": ref_column,
                "on": ref_table,
            })
        return foreign_keys


# =============================================================================
# Strategy Registry
# =============================================================================

CHUNK_STRATEGIES: dict[str, type[ChunkStrategy]] = {
    # バックエンド
    "python": PythonChunkStrategy,
    "php": PHPChunkStrategy,

    # フロントエンド
    "typescript": TypeScriptChunkStrategy,
    "javascript": TypeScriptChunkStrategy,
    "html": HTMLChunkStrategy,
    "blade": BladeChunkStrategy,

    # スタイル
    "css": CSSChunkStrategy,
    "scss": CSSChunkStrategy,

    # 設定ファイル
    "composer_json": ComposerJsonStrategy,
    "tailwind_config": TailwindConfigStrategy,

    # Laravel 特化
    "filament_resource": FilamentResourceStrategy,
    "migration": MigrationChunkStrategy,
}


def get_strategy(language: str) -> ChunkStrategy:
    """言語に対応する Strategy を取得"""
    strategy_class = CHUNK_STRATEGIES.get(language, FallbackChunkStrategy)
    return strategy_class()


# =============================================================================
# Main ASTChunker
# =============================================================================

class ASTChunker:
    """tree-sitter を使った AST ベースのチャンク分割"""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.max_tokens = self.config.get("chunk_max_tokens", 512)
        self.parsers: dict[str, Any] = {}
        self.fallback_languages: set[str] = set()

    def _get_parser(self, language: str) -> Any | None:
        """パーサを取得（なければフォールバック登録）"""
        if language in self.fallback_languages:
            return None

        if language not in self.parsers:
            try:
                import tree_sitter_languages
                self.parsers[language] = tree_sitter_languages.get_parser(language)
            except Exception as e:
                logger.warning(f"Parser for {language} not available: {e}")
                logger.warning(f"Falling back to line-based chunking for {language}")
                self.fallback_languages.add(language)
                return None

        return self.parsers.get(language)

    def chunk_file(self, file_path: Path) -> list[Chunk]:
        """ファイルをチャンク化"""
        if not file_path.exists():
            logger.warning(f"File not found: {file_path}")
            return []

        language = detect_language(file_path)
        strategy = get_strategy(language)

        # Strategy が直接 chunk_file を実装している場合
        if hasattr(strategy, 'chunk_file'):
            try:
                chunks = strategy.chunk_file(file_path, None)
                if chunks:
                    return chunks
            except NotImplementedError:
                pass

        # AST パーサを使う場合
        parser = self._get_parser(language)

        if parser is None:
            # フォールバック: 行数ベース
            content = file_path.read_text(encoding='utf-8')
            return FallbackChunkStrategy().chunk_by_lines(content, file_path)

        # AST ベースのチャンク
        return self._chunk_with_ast(file_path, parser, strategy)

    def _chunk_with_ast(self, file_path: Path, parser: Any, strategy: ChunkStrategy) -> list[Chunk]:
        """AST を使ってチャンク化"""
        content = file_path.read_text(encoding='utf-8')
        tree = parser.parse(content.encode())

        chunks = []
        node_types = strategy.get_chunk_node_types()

        for node in self._find_nodes(tree.root_node, node_types):
            name = strategy.extract_name(node)
            metadata = strategy.extract_metadata(node, file_path, content)

            node_content = node.text.decode()

            # 大きすぎる場合は切り詰め
            if len(node_content) > self.max_tokens * 4:
                node_content = self._truncate_with_summary(node_content)

            chunks.append(Chunk(
                id=f"{file_path}:{name}",
                type=node.type,
                name=name,
                file=str(file_path),
                content=node_content,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                metadata=metadata,
            ))

        # モジュールレベルのサマリも追加
        if chunks:
            summary = self._create_module_summary(file_path, content, chunks)
            if summary:
                chunks.insert(0, summary)

        return chunks

    def _find_nodes(self, node: Any, types: list[str]) -> list[Any]:
        """指定タイプのノードを再帰的に探索"""
        results = []

        if node.type in types:
            results.append(node)

        for child in node.children:
            results.extend(self._find_nodes(child, types))

        return results

    def _truncate_with_summary(self, content: str, max_length: int = 2000) -> str:
        """長いコンテンツを要約付きで切り詰め"""
        if len(content) <= max_length:
            return content

        # 最初と最後を残す
        half = max_length // 2
        return f"{content[:half]}\n\n... [truncated] ...\n\n{content[-half:]}"

    def _create_module_summary(self, file_path: Path, content: str, chunks: list[Chunk]) -> Chunk | None:
        """モジュールレベルのサマリを作成"""
        # ファイルの最初の部分（import/use 文等）を含める
        lines = content.split('\n')[:30]
        summary_content = '\n'.join(lines)

        return Chunk(
            id=f"{file_path}:module",
            type="module",
            name=file_path.name,
            file=str(file_path),
            content=summary_content,
            metadata={
                "chunk_count": len(chunks),
                "chunk_types": list(set(c.type for c in chunks)),
            }
        )


# =============================================================================
# Utility Functions
# =============================================================================

def chunk_directory(
    directory: Path,
    extensions: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> list[Chunk]:
    """ディレクトリ内のファイルをチャンク化"""
    chunker = ASTChunker(config)
    all_chunks = []

    if extensions is None:
        extensions = [".py", ".php", ".js", ".ts", ".html", ".css", ".blade.php"]

    if exclude_patterns is None:
        exclude_patterns = ["**/node_modules/**", "**/__pycache__/**", "**/venv/**", "**/vendor/**"]

    for ext in extensions:
        for file_path in directory.rglob(f"*{ext}"):
            # 除外パターンをチェック
            skip = False
            for pattern in exclude_patterns:
                if file_path.match(pattern):
                    skip = True
                    break

            if skip:
                continue

            try:
                chunks = chunker.chunk_file(file_path)
                all_chunks.extend(chunks)
            except Exception as e:
                logger.error(f"Failed to chunk {file_path}: {e}")

    return all_chunks

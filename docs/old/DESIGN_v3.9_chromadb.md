# Code Intelligence MCP Server v3.9 設計ドキュメント

## 実装ステータス

| 項目 | ステータス | ファイル |
|------|----------|---------|
| AST チャンキング | ✅ 完了 | `tools/ast_chunker.py` |
| 同期状態管理 | ✅ 完了 | `tools/sync_state.py` |
| ChromaDB マネージャー | ✅ 完了 | `tools/chromadb_manager.py` |
| code_intel_server.py 統合 | ✅ 完了 | `code_intel_server.py` |
| init-project.sh 更新 | ✅ 完了 | `init-project.sh` |
| setup.sh 更新 | ✅ 完了 | `setup.sh` |
| README.md 更新 | ✅ 完了 | `README.md` |
| 統合テスト | ✅ パス | `tests_with_code/test_chromadb_integration.py` |
| AST Chunker テスト | ✅ 19/19 パス | `tests_with_code/test_ast_chunker.py` |

### 新規ツール

- `sync_index`: ソースコードの増分同期
- `semantic_search`: 地図/森の統合ベクトル検索

---

## v3.10 変更点: 探索復帰フロー

### 問題

READYフェーズで `check_write_target` がブロックした場合、探索に戻る手段がなかった。
セッションを最初からやり直すしかなく、非効率だった。

### 解決策

2つの復帰手段を追加:

#### 1. `add_explored_files` (軽量復帰)

READYフェーズのまま、探索済みファイルリストに追加登録する。

```python
# 使用例
session.add_explored_files(["tests_with_code/", "new_module.py"])
```

**ユースケース:**
- 新しいディレクトリにファイルを作成したい
- 同じセッションで追加のファイルを編集したい

#### 2. `revert_to_exploration` (完全復帰)

任意のフェーズからEXPLORATIONに戻る。既存の探索結果は保持される。

```python
# 使用例
session.revert_to_exploration(keep_results=True)
```

**ユースケース:**
- 追加の探索が必要
- 別のアプローチで再探索したい

### 改善された `check_write_target` レスポンス

ブロック時に復帰オプションを提示:

```json
{
  "allowed": false,
  "error": "...",
  "recovery_options": {
    "add_explored_files": {
      "description": "Add files/directories to explored list without leaving READY phase",
      "example": "session.add_explored_files(['tests_with_code/'])"
    },
    "revert_to_exploration": {
      "description": "Go back to EXPLORATION phase for thorough re-exploration",
      "example": "session.revert_to_exploration()"
    }
  }
}
```

### 新規 MCP ツール

| ツール名 | 説明 |
|---------|------|
| `add_explored_files` | READYフェーズで探索済みファイルを追加 |
| `revert_to_exploration` | EXPLORATIONフェーズに戻る |

---

### 主要機能

- **ChromaDB 内蔵**: devrag 不要、Python から直接ベクトル検索
- **AST チャンキング**: PHP, Python, JS, Blade, CSS 等を構文解析
- **増分同期**: SHA256 フィンガープリントで変更ファイルのみ再インデックス
- **Short-circuit**: 地図で ≥0.7 スコアなら森をスキップ
- **自動同期**: セッション開始時に TTL 超過なら自動実行

---

## 概要

devrag を廃止し、ChromaDB に統一することで Python ソースコードの意味検索を実現する。

---

## 変更サマリ

| 項目 | v3.8 (現行) | v3.9 (提案) |
|------|-------------|-------------|
| MCP サーバー数 | 3 (devrag-map, devrag-forest, code-intel) | 1 (code-intel のみ) |
| ベクトル DB | devrag (外部) | ChromaDB (内蔵) |
| Python 対応 | ✗ | ✓ |
| チャンク方式 | 行数ベース | AST ベース (tree-sitter) |
| 埋め込みモデル | multilingual-e5-small | 選択可能 (multilingual-e5 / CodeBERT) |

---

## アーキテクチャ

### v3.8 (現行)

```
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│ devrag-map  │ │devrag-forest│ │ code-intel  │
│   (MCP)     │ │   (MCP)     │ │   (MCP)     │
└──────┬──────┘ └──────┬──────┘ └──────┬──────┘
       │               │               │
       ▼               ▼               ▼
  vectors-map.db  vectors-forest.db  Session/Tools
```

### v3.9 (提案)

```
┌─────────────────────────────────────────────────┐
│              code-intel (MCP)                    │
│  ┌─────────────────────────────────────────┐   │
│  │           ChromaDB (内蔵)                │   │
│  │  ┌─────────────┐  ┌─────────────────┐  │   │
│  │  │ collection: │  │ collection:      │  │   │
│  │  │   "map"     │  │   "forest"       │  │   │
│  │  │ (agreements)│  │ (Python + MD)    │  │   │
│  │  └─────────────┘  └─────────────────┘  │   │
│  └─────────────────────────────────────────┘   │
│                                                 │
│  ┌─────────────┐  ┌─────────────┐             │
│  │ Session     │  │ AST Chunker │             │
│  │ Manager     │  │ (tree-sitter)│             │
│  └─────────────┘  └─────────────┘             │
└─────────────────────────────────────────────────┘
```

---

## コレクション設計

### collection: "map" (地図)

| 項目 | 値 |
|------|-----|
| 用途 | 過去の成功ペア (agreements) |
| ソース | `.code-intel/agreements/*.md` |
| 更新タイミング | `record_outcome(success)` 時 |
| 検索優先度 | **高** (Short-circuit: score ≥ 0.7 で forest スキップ) |

### collection: "forest" (森)

| 項目 | 値 |
|------|-----|
| 用途 | ソースコード全体の意味検索 |
| ソース | `**/*.py`, `**/*.md`, etc. |
| 更新タイミング | `start_session` 時 (差分同期) |
| 検索優先度 | 通常 (map ミス時に使用) |

---

## AST ベースチャンク分割

### 従来 (行数ベース)

```python
# 500行ごとに切断 → 関数の途中で切れる
def authenticate(self, user, password):
    # ... 200行 ...
# ← ここで切断されると意味不明なチャンクに
    return result
```

### v3.9 (AST ベース)

```python
# tree-sitter で構文単位に分割
chunks = [
    {
        "type": "function",
        "name": "authenticate",
        "file": "auth/service.py",
        "line_start": 45,
        "line_end": 120,
        "content": "def authenticate(self, user, password): ..."
    },
    {
        "type": "class",
        "name": "AuthService",
        "file": "auth/service.py",
        "line_start": 10,
        "line_end": 250,
        "content": "class AuthService: ..."
    }
]
```

### チャンク粒度

| 粒度 | 対象 | 用途 |
|------|------|------|
| **function** | 関数/メソッド | 主要な検索単位 |
| **class** | クラス定義 | 構造理解 |
| **module** | ファイル全体のサマリ | 概要把握 |

---

## 埋め込みモデル

### オプション

| モデル | 特徴 | 用途 |
|--------|------|------|
| `multilingual-e5-small` | 日英対応、軽量 | デフォルト |
| `multilingual-e5-base` | 日英対応、高精度 | 精度重視 |
| `microsoft/codebert-base` | コード特化 | コード検索特化 |
| `microsoft/unixcoder-base` | コード+NL | バランス型 |

### 設定

```python
# .code-intel/config.json
{
    "embedding_model": "multilingual-e5-small",  # or "codebert-base"
    "chunk_strategy": "ast",  # or "lines"
    "chunk_max_tokens": 512
}
```

---

## Short-circuit Logic

```python
async def search(query: str, target_feature: str) -> SearchResult:
    # 1. 地図を検索 (過去の成功パターン)
    map_results = collection_map.query(
        query_texts=[f"{query} {target_feature}"],
        n_results=5
    )

    # 2. 高スコアなら森をスキップ
    if map_results and map_results[0].score >= 0.7:
        return SearchResult(
            source="map",
            confidence="high",
            results=map_results,
            skip_forest=True
        )

    # 3. 森を検索
    forest_results = collection_forest.query(
        query_texts=[query],
        n_results=10
    )

    return SearchResult(
        source="forest",
        confidence="medium",
        results=forest_results,
        map_hints=map_results  # 参考情報として含める
    )
```

---

## ファイル構造

### MCP サーバー (llm-helper/)

```
llm-helper/
├── code_intel_server.py
├── tools/
│   ├── session.py
│   ├── query_frame.py
│   ├── chromadb_manager.py    ← 新規
│   ├── ast_chunker.py         ← 新規
│   ├── agreements.py
│   └── learned_pairs.py
├── setup.sh
└── init-project.sh
```

### プロジェクト (.code-intel/)

```
your-project/.code-intel/
├── config.json                ← 新規 (モデル設定等)
├── chroma/                    ← 新規 (ChromaDB データ)
│   ├── chroma.sqlite3
│   └── ...
├── agreements/
│   └── *.md
└── learned_pairs.json
```

---

## 新規コンポーネント

### 1. ChromaDBManager

```python
# tools/chromadb_manager.py
import chromadb
from chromadb.config import Settings

class ChromaDBManager:
    """プロジェクトごとの ChromaDB 管理"""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.db_path = self.project_root / ".code-intel" / "chroma"

        self.client = chromadb.PersistentClient(
            path=str(self.db_path),
            settings=Settings(anonymized_telemetry=False)
        )

        # コレクション初期化
        self.map_collection = self.client.get_or_create_collection(
            name="map",
            metadata={"description": "Agreements - successful NL→Symbol pairs"}
        )
        self.forest_collection = self.client.get_or_create_collection(
            name="forest",
            metadata={"description": "Source code chunks"}
        )

    def sync_forest(self, source_dirs: list[str]) -> SyncResult:
        """ソースコードを forest にインデックス"""
        chunker = ASTChunker()

        for dir_path in source_dirs:
            for py_file in Path(dir_path).glob("**/*.py"):
                chunks = chunker.chunk_file(py_file)
                self._upsert_chunks(self.forest_collection, chunks)

        return SyncResult(added=..., updated=..., deleted=...)

    def sync_map(self) -> SyncResult:
        """agreements を map にインデックス"""
        agreements_dir = self.project_root / ".code-intel" / "agreements"

        for md_file in agreements_dir.glob("*.md"):
            content = md_file.read_text()
            self._upsert_document(self.map_collection, md_file.name, content)

        return SyncResult(...)

    def search_map(self, query: str, n_results: int = 5) -> list[SearchHit]:
        """地図を検索"""
        results = self.map_collection.query(
            query_texts=[query],
            n_results=n_results
        )
        return self._to_search_hits(results)

    def search_forest(self, query: str, n_results: int = 10) -> list[SearchHit]:
        """森を検索"""
        results = self.forest_collection.query(
            query_texts=[query],
            n_results=n_results
        )
        return self._to_search_hits(results)
```

### 2. ASTChunker

```python
# tools/ast_chunker.py
import tree_sitter_languages

class ASTChunker:
    """tree-sitter を使った AST ベースのチャンク分割"""

    def __init__(self, max_tokens: int = 512):
        self.max_tokens = max_tokens
        self.parser = tree_sitter_languages.get_parser("python")

    def chunk_file(self, file_path: Path) -> list[Chunk]:
        """Python ファイルを関数/クラス単位でチャンク化"""
        content = file_path.read_text()
        tree = self.parser.parse(content.encode())

        chunks = []

        # 関数を抽出
        for node in self._find_nodes(tree.root_node, "function_definition"):
            chunks.append(self._node_to_chunk(node, file_path, "function"))

        # クラスを抽出
        for node in self._find_nodes(tree.root_node, "class_definition"):
            chunks.append(self._node_to_chunk(node, file_path, "class"))

        # モジュールレベルのサマリ
        chunks.append(self._create_module_summary(file_path, content))

        return chunks

    def _node_to_chunk(self, node, file_path: Path, chunk_type: str) -> Chunk:
        name = self._get_node_name(node)
        content = node.text.decode()

        # 大きすぎる場合は分割
        if len(content) > self.max_tokens * 4:
            content = self._truncate_with_summary(content)

        return Chunk(
            id=f"{file_path}:{name}",
            type=chunk_type,
            name=name,
            file=str(file_path),
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            content=content,
            metadata={
                "docstring": self._extract_docstring(node),
                "signature": self._extract_signature(node),
            }
        )
```

---

## ツール変更

### 削除されるツール

MCP ツールとしては削除（内部で ChromaDB を直接使用）:

| 旧ツール (devrag MCP) | 新実装 |
|-----------------------|--------|
| `mcp__devrag-map__search` | `ChromaDBManager.search_map()` |
| `mcp__devrag-forest__search` | `ChromaDBManager.search_forest()` |
| `mcp__devrag-*__sync` | `ChromaDBManager.sync_*()` |

### 新規ツール (code-intel)

```python
Tool(
    name="sync_index",
    description="v3.9: Sync ChromaDB index (forest and/or map)",
    inputSchema={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": ["forest", "map", "all"],
                "description": "Which collection to sync"
            },
            "force": {
                "type": "boolean",
                "description": "Force full re-index (default: incremental)"
            }
        }
    }
)

Tool(
    name="semantic_search",
    description="v3.9: Search code semantically using ChromaDB",
    inputSchema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "collection": {
                "type": "string",
                "enum": ["map", "forest", "auto"],
                "default": "auto"
            },
            "n_results": {"type": "integer", "default": 10}
        },
        "required": ["query"]
    }
)
```

---

## セットアップ変更

### init-project.sh

```bash
# v3.8 (削除)
# devrag-forest.json, devrag-map.json の生成

# v3.9 (新規)
# config.json の生成
cat > "$PROJECT_PATH/.code-intel/config.json" << EOF
{
  "embedding_model": "multilingual-e5-small",
  "source_dirs": ["src", "lib", "app"],
  "chunk_strategy": "ast",
  "chunk_max_tokens": 512
}
EOF
```

### .mcp.json

```json
{
  "mcpServers": {
    "code-intel": {
      "type": "stdio",
      "command": "/path/to/llm-helper/venv/bin/python",
      "args": ["/path/to/llm-helper/code_intel_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/llm-helper"
      }
    }
  }
}
```

**devrag-map, devrag-forest は不要に**

---

## 依存関係の変更

### requirements.txt

```diff
  mcp>=1.0.0
  tree-sitter>=0.21.0
  tree-sitter-languages>=1.10.0
  sentence-transformers>=2.2.0
  scikit-learn>=1.0.0
  pytest>=7.0.0
+ chromadb>=0.4.0
+ onnxruntime>=1.16.0  # ChromaDB の埋め込み用
```

### 削除される依存

- devrag バイナリ (外部ツール)
- ONNX Runtime のシステムインストール (ChromaDB が内蔵)

---

## マイグレーション

### v3.8 → v3.9

```bash
# 1. 依存関係を更新
pip install chromadb>=0.4.0

# 2. 旧データを削除
rm -rf .code-intel/devrag-*.json
rm -rf .code-intel/vectors-*.db

# 3. ChromaDB を初期化
python -c "from tools.chromadb_manager import ChromaDBManager; m = ChromaDBManager('.'); m.sync_forest(['tools', 'docs']); m.sync_map()"

# 4. .mcp.json から devrag-map, devrag-forest を削除
```

---

## 実装優先順位

| 優先度 | タスク | 工数 |
|--------|--------|------|
| 1 | ChromaDBManager 基本実装 | 中 |
| 2 | ASTChunker 実装 | 中 |
| 3 | code_intel_server.py 統合 | 中 |
| 4 | init-project.sh 更新 | 小 |
| 5 | start_session での自動 sync | 小 |
| 6 | ドキュメント更新 | 小 |

---

## リスクと対策

| リスク | 対策 |
|--------|------|
| ChromaDB の埋め込み速度 | バッチ処理、差分同期 |
| 大規模リポジトリ | チャンク数制限、フィルタリング |
| モデルサイズ | 初回起動時に警告、プログレス表示 |
| 既存 devrag ユーザー | マイグレーションスクリプト提供 |

---

## 追加考慮事項（レビュー指摘）

### 1. AST チャンカーの多言語対応

#### 問題

現設計は Python 特化。実際のプロジェクトは JS/TS/Java/Go 等を含む。

#### 対策: Strategy パターン

```python
# tools/ast_chunker.py

from abc import ABC, abstractmethod

class ChunkStrategy(ABC):
    """言語ごとのチャンク戦略"""

    @abstractmethod
    def get_chunk_node_types(self) -> list[str]:
        """チャンク対象のノードタイプ"""
        pass

    @abstractmethod
    def extract_name(self, node) -> str:
        """ノードから名前を抽出"""
        pass


class PythonChunkStrategy(ChunkStrategy):
    def get_chunk_node_types(self) -> list[str]:
        return ["function_definition", "class_definition"]

    def extract_name(self, node) -> str:
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode()
        return "unknown"


class TypeScriptChunkStrategy(ChunkStrategy):
    def get_chunk_node_types(self) -> list[str]:
        return ["function_declaration", "class_declaration", "method_definition"]

    def extract_name(self, node) -> str:
        # TS 固有のロジック
        ...


class FallbackChunkStrategy(ChunkStrategy):
    """未対応言語: 行数ベースにフォールバック"""

    def get_chunk_node_types(self) -> list[str]:
        return []  # AST チャンクなし

    def chunk_by_lines(self, content: str, max_lines: int = 50) -> list[Chunk]:
        """行数ベースで分割"""
        lines = content.split("\n")
        chunks = []
        for i in range(0, len(lines), max_lines):
            chunk_lines = lines[i:i + max_lines]
            chunks.append(Chunk(
                type="lines",
                content="\n".join(chunk_lines),
                line_start=i + 1,
                line_end=min(i + max_lines, len(lines))
            ))
        return chunks


# 言語→戦略のマッピング
CHUNK_STRATEGIES: dict[str, type[ChunkStrategy]] = {
    "python": PythonChunkStrategy,
    "typescript": TypeScriptChunkStrategy,
    "javascript": TypeScriptChunkStrategy,  # 共通
    "java": JavaChunkStrategy,
    "go": GoChunkStrategy,
    # ... 追加可能
}

def get_strategy(language: str) -> ChunkStrategy:
    strategy_class = CHUNK_STRATEGIES.get(language, FallbackChunkStrategy)
    return strategy_class()
```

#### 対応言語の優先順位

| 優先度 | 言語 | 理由 |
|--------|------|------|
| 1 | Python | 本プロジェクト |
| 2 | **PHP** | Laravel/WordPress 等で頻出 |
| 3 | TypeScript/JavaScript | Web 開発で頻出 |
| 4 | **HTML/Blade** | フロントエンド/テンプレート |
| 5 | **CSS/SCSS** | スタイル定義 |
| 6 | Go | CLI/バックエンド |
| 7 | Java | エンタープライズ |
| 8 | その他 | 行数ベースフォールバック |

---

### 1.1 PHP/Laravel 対応

```python
class PHPChunkStrategy(ChunkStrategy):
    def get_chunk_node_types(self) -> list[str]:
        return [
            "class_declaration",
            "function_definition",
            "method_declaration",
            "trait_declaration",
        ]

    def extract_name(self, node) -> str:
        for child in node.children:
            if child.type == "name":
                return child.text.decode()
        return "unknown"

    def extract_metadata(self, node) -> dict:
        """PHP 固有のメタデータ"""
        metadata = {}
        # namespace 抽出
        # use 文抽出
        # docblock 抽出
        return metadata
```

### 1.2 HTML/Blade 対応

HTML/Blade はコンポーネント単位でチャンク：

```python
class HTMLChunkStrategy(ChunkStrategy):
    """HTML/Blade テンプレート用"""

    def get_chunk_node_types(self) -> list[str]:
        return [
            "element",  # 主要な HTML 要素
        ]

    def should_chunk(self, node) -> bool:
        """チャンク対象とする要素を判定"""
        # コンポーネントっぽい要素のみ
        if node.type != "element":
            return False

        tag_name = self._get_tag_name(node)

        # Blade コンポーネント
        if tag_name.startswith("x-"):
            return True

        # セマンティック要素
        if tag_name in ["section", "article", "nav", "header", "footer", "main", "form"]:
            return True

        # id 属性がある要素
        if self._has_id_attribute(node):
            return True

        return False

    def extract_name(self, node) -> str:
        tag_name = self._get_tag_name(node)
        id_attr = self._get_id_attribute(node)
        if id_attr:
            return f"{tag_name}#{id_attr}"
        return tag_name


class BladeChunkStrategy(HTMLChunkStrategy):
    """Laravel Blade 特化"""

    def get_chunk_node_types(self) -> list[str]:
        return super().get_chunk_node_types() + [
            "directive",  # @section, @component 等
        ]

    def should_chunk(self, node) -> bool:
        if node.type == "directive":
            directive_name = self._get_directive_name(node)
            return directive_name in ["section", "component", "slot", "push"]

        return super().should_chunk(node)
```

### 1.3 CSS/Tailwind 対応

```python
class CSSChunkStrategy(ChunkStrategy):
    """CSS/SCSS 用"""

    def get_chunk_node_types(self) -> list[str]:
        return [
            "rule_set",      # セレクタ + ルール
            "media_statement",  # @media クエリ
            "keyframes_statement",  # @keyframes
        ]

    def extract_name(self, node) -> str:
        if node.type == "rule_set":
            # セレクタを名前として使用
            for child in node.children:
                if child.type == "selectors":
                    return child.text.decode()[:50]  # 長すぎる場合は切り詰め
        return "unknown"

    def chunk_tailwind_config(self, content: str) -> list[Chunk]:
        """tailwind.config.js の特別処理"""
        # theme, plugins, extend 等をセクションとしてチャンク
        ...
```

### 1.4 PHP 名前空間（FQCN）管理

#### 問題

`LoginController` だけでは不十分。大規模プロジェクトで同名クラスを混同する。

#### 対策: 完全修飾名（FQCN）をメタデータに含める

```python
class PHPChunkStrategy(ChunkStrategy):
    def extract_metadata(self, node, file_content: str) -> dict:
        """PHP 固有のメタデータ（FQCN 含む）"""
        metadata = {}

        # namespace を抽出
        namespace = self._extract_namespace(file_content)
        class_name = self.extract_name(node)

        if namespace and class_name:
            metadata["fqcn"] = f"{namespace}\\{class_name}"
        else:
            metadata["fqcn"] = class_name

        # use 文を抽出（依存クラス）
        metadata["imports"] = self._extract_use_statements(file_content)

        # docblock を抽出
        metadata["docblock"] = self._extract_docblock(node)

        return metadata

    def _extract_namespace(self, content: str) -> str | None:
        """namespace App\Http\Controllers; を抽出"""
        import re
        match = re.search(r'namespace\s+([\w\\]+);', content)
        return match.group(1) if match else None

    def _extract_use_statements(self, content: str) -> list[str]:
        """use 文を抽出"""
        import re
        return re.findall(r'use\s+([\w\\]+)(?:\s+as\s+\w+)?;', content)
```

#### ChromaDB への格納例

```python
collection.add(
    ids=["app_http_controllers_auth_logincontroller"],
    documents=["class LoginController extends Controller { ... }"],
    metadatas=[{
        "type": "class",
        "name": "LoginController",
        "fqcn": "App\\Http\\Controllers\\Auth\\LoginController",  # ← 完全修飾名
        "file": "app/Http/Controllers/Auth/LoginController.php",
        "imports": ["App\\Models\\User", "Illuminate\\Http\\Request"],
        "docblock": "Handles user authentication"
    }]
)
```

---

### 1.5 言語間の依存関係（Cross-Language Indexing）

#### 問題

Web 開発は Blade → Controller → Model → Database と処理が流れる。
単独ファイルのインデックスでは関係性が失われる。

#### 対策: related_symbols の自動付与

```python
class BladeChunkStrategy(HTMLChunkStrategy):
    def extract_metadata(self, node, file_path: Path, file_content: str) -> dict:
        metadata = super().extract_metadata(node, file_path, file_content)

        # Blade から参照されている PHP シンボルを抽出
        related = []

        # ルートアクション: route('users.store') → UsersController@store
        routes = self._extract_route_calls(file_content)
        related.extend(routes)

        # コンポーネント: <x-user-card /> → App\View\Components\UserCard
        components = self._extract_blade_components(file_content)
        related.extend(components)

        # Livewire: @livewire('user-list') → App\Http\Livewire\UserList
        livewire = self._extract_livewire_components(file_content)
        related.extend(livewire)

        # 変数から推測: $user → User モデル
        variables = self._extract_typed_variables(file_content)
        related.extend(variables)

        metadata["related_symbols"] = related
        return metadata

    def _extract_route_calls(self, content: str) -> list[str]:
        """route('users.store') を抽出"""
        import re
        routes = re.findall(r"route\(['\"]([^'\"]+)['\"]\)", content)
        # routes.php からコントローラーアクションに解決（要: ルート解析）
        return routes

    def _extract_blade_components(self, content: str) -> list[str]:
        """<x-user-card /> を App\View\Components\UserCard に変換"""
        import re
        components = re.findall(r'<x-([\w-]+)', content)
        return [self._component_to_fqcn(c) for c in components]

    def _component_to_fqcn(self, component_name: str) -> str:
        """user-card → App\View\Components\UserCard"""
        parts = component_name.split('-')
        class_name = ''.join(p.capitalize() for p in parts)
        return f"App\\View\\Components\\{class_name}"
```

#### Controller → Model の関係抽出

```python
class PHPChunkStrategy(ChunkStrategy):
    def extract_metadata(self, node, file_path: Path, file_content: str) -> dict:
        metadata = super().extract_metadata(node, file_content)

        # Eloquent モデル参照: User::find(), $this->user->orders
        models = self._extract_model_references(file_content)
        metadata["related_models"] = models

        # サービス/リポジトリ参照: $this->userService->create()
        services = self._extract_service_references(file_content)
        metadata["related_services"] = services

        return metadata
```

---

### 1.6 データベーススキーマのインデックス化

#### 問題

EC システムでは「注文データの保存場所」を探すクエリが頻出。
コードだけでは DB 構造が分からない。

#### 対策: MigrationChunkStrategy

```python
class MigrationChunkStrategy(ChunkStrategy):
    """Laravel マイグレーション用"""

    def get_chunk_node_types(self) -> list[str]:
        return ["method_declaration"]  # up(), down()

    def chunk_file(self, file_path: Path) -> list[Chunk]:
        content = file_path.read_text()
        chunks = []

        # テーブル作成を抽出
        tables = self._extract_table_definitions(content)
        for table in tables:
            chunks.append(Chunk(
                id=f"migration:{table['name']}",
                type="table_schema",
                name=table["name"],
                content=table["definition"],
                metadata={
                    "columns": table["columns"],
                    "indexes": table["indexes"],
                    "foreign_keys": table["foreign_keys"],
                    "migration_file": str(file_path),
                }
            ))

        return chunks

    def _extract_table_definitions(self, content: str) -> list[dict]:
        """Schema::create() からテーブル定義を抽出"""
        import re
        tables = []

        # Schema::create('orders', function (Blueprint $table) { ... })
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

    def _extract_columns(self, definition: str) -> list[dict]:
        """$table->string('name') 等を抽出"""
        import re
        columns = []
        # $table->string('email')->unique()->nullable()
        pattern = r"\$table->(\w+)\(['\"](\w+)['\"]"
        for col_type, col_name in re.findall(pattern, definition):
            columns.append({"name": col_name, "type": col_type})
        return columns
```

#### 検索例

```
Query: "注文データはどこに保存されている？"
↓
ChromaDB search → migration:orders チャンクがヒット
↓
Response: "orders テーブル (database/migrations/2024_01_01_create_orders_table.php)
  - id, user_id, total, status, created_at
  - 外部キー: user_id → users.id"
```

---

### 1.7 指紋ベースの増分同期

#### 問題

TTL ベースの同期では、小さな修正のたびに大規模な再インデックスが走る。

#### 対策: ファイルハッシュによる差分検出

```python
# tools/sync_state.py

import hashlib
import json
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime

@dataclass
class FileFingerprint:
    path: str
    hash: str
    mtime: float
    indexed_at: str

class SyncStateManager:
    """ファイルごとの同期状態を管理"""

    def __init__(self, project_root: Path):
        self.state_file = project_root / ".code-intel" / "sync_state.json"
        self.state: dict[str, FileFingerprint] = self._load_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            return {k: FileFingerprint(**v) for k, v in data.items()}
        return {}

    def _save_state(self):
        data = {k: v.__dict__ for k, v in self.state.items()}
        self.state_file.write_text(json.dumps(data, indent=2))

    def compute_hash(self, file_path: Path) -> str:
        """ファイルの SHA256 ハッシュを計算"""
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]

    def get_changed_files(self, source_dirs: list[Path]) -> tuple[list[Path], list[Path], list[str]]:
        """変更されたファイルを検出"""
        added = []
        modified = []
        deleted = list(self.state.keys())

        for dir_path in source_dirs:
            for file_path in dir_path.rglob("*"):
                if not file_path.is_file():
                    continue

                path_str = str(file_path)
                current_hash = self.compute_hash(file_path)

                if path_str in deleted:
                    deleted.remove(path_str)

                if path_str not in self.state:
                    added.append(file_path)
                elif self.state[path_str].hash != current_hash:
                    modified.append(file_path)

        return added, modified, deleted

    def mark_indexed(self, file_path: Path):
        """インデックス完了をマーク"""
        path_str = str(file_path)
        self.state[path_str] = FileFingerprint(
            path=path_str,
            hash=self.compute_hash(file_path),
            mtime=file_path.stat().st_mtime,
            indexed_at=datetime.now().isoformat()
        )
        self._save_state()

    def mark_deleted(self, path_str: str):
        """削除されたファイルを状態から除去"""
        if path_str in self.state:
            del self.state[path_str]
            self._save_state()
```

#### ChromaDBManager での使用

```python
class ChromaDBManager:
    def __init__(self, project_root: str, config: dict):
        self.sync_state = SyncStateManager(Path(project_root))
        # ...

    def sync_forest_incremental(self, source_dirs: list[str]) -> SyncResult:
        """指紋ベースの増分同期"""
        dirs = [Path(d) for d in source_dirs]
        added, modified, deleted = self.sync_state.get_changed_files(dirs)

        # 削除されたファイルのチャンクを削除
        for path_str in deleted:
            self.forest_collection.delete(where={"file": path_str})
            self.sync_state.mark_deleted(path_str)

        # 変更されたファイルを再インデックス
        for file_path in modified:
            self.forest_collection.delete(where={"file": str(file_path)})
            self._index_file(file_path)
            self.sync_state.mark_indexed(file_path)

        # 新規ファイルをインデックス
        for file_path in added:
            self._index_file(file_path)
            self.sync_state.mark_indexed(file_path)

        return SyncResult(
            added=len(added),
            modified=len(modified),
            deleted=len(deleted)
        )
```

#### sync_state.json の例

```json
{
  "app/Http/Controllers/UserController.php": {
    "path": "app/Http/Controllers/UserController.php",
    "hash": "a1b2c3d4e5f6g7h8",
    "mtime": 1704067200.0,
    "indexed_at": "2024-01-01T12:00:00"
  },
  "resources/views/users/index.blade.php": {
    "path": "resources/views/users/index.blade.php",
    "hash": "h8g7f6e5d4c3b2a1",
    "mtime": 1704067300.0,
    "indexed_at": "2024-01-01T12:01:00"
  }
}
```

---

### 1.8 composer.json のインデックス化（Dependency Awareness）

#### 問題

PHPプロジェクトでは外部ライブラリ（spatie/laravel-permission 等）に依存することが多い。
LLM がプロジェクトの依存関係を知らないと、適切な推論ができない。

#### 対策: ComposerJsonStrategy

```python
class ComposerJsonStrategy(ChunkStrategy):
    """composer.json 解析用"""

    def chunk_file(self, file_path: Path) -> list[Chunk]:
        content = json.loads(file_path.read_text())
        chunks = []

        # require セクション
        if "require" in content:
            chunks.append(Chunk(
                id="composer:require",
                type="dependencies",
                name="production_dependencies",
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
                content=json.dumps(content["require-dev"], indent=2),
                metadata={
                    "packages": list(content["require-dev"].keys()),
                    "type": "development"
                }
            ))

        # autoload セクション（名前空間マッピング）
        if "autoload" in content:
            psr4 = content["autoload"].get("psr-4", {})
            chunks.append(Chunk(
                id="composer:autoload",
                type="namespace_mapping",
                name="psr4_autoload",
                content=json.dumps(psr4, indent=2),
                metadata={
                    "namespaces": list(psr4.keys()),
                    "paths": list(psr4.values())
                }
            ))

        return chunks
```

#### 検索例

```
Query: "権限管理はどう実装されている？"
↓
ChromaDB search → composer:require がヒット
↓
Response: "spatie/laravel-permission を使用。
  HasRoles トレイトを User モデルに適用。
  参考: https://spatie.be/docs/laravel-permission"
```

---

### 1.9 TailwindConfigStrategy（テーマ設定のインデックス化）

#### 問題

UI修正の依頼（「ボタンの色を変えて」「レスポンシブ対応して」）に対し、
プロジェクト固有のテーマ設定を知らないと適切な修正ができない。

#### 対策: TailwindConfigStrategy

```python
class TailwindConfigStrategy(ChunkStrategy):
    """tailwind.config.js / tailwind.config.ts 解析用"""

    def chunk_file(self, file_path: Path) -> list[Chunk]:
        content = file_path.read_text()
        chunks = []

        # テーマ設定を抽出
        theme_config = self._extract_theme(content)
        if theme_config:
            chunks.append(Chunk(
                id="tailwind:theme",
                type="ui_config",
                name="tailwind_theme",
                content=theme_config,
                metadata={
                    "colors": self._extract_colors(theme_config),
                    "screens": self._extract_screens(theme_config),
                    "fonts": self._extract_fonts(theme_config),
                }
            ))

        # プラグイン設定を抽出
        plugins = self._extract_plugins(content)
        if plugins:
            chunks.append(Chunk(
                id="tailwind:plugins",
                type="ui_config",
                name="tailwind_plugins",
                content=plugins,
                metadata={
                    "plugins": self._parse_plugin_names(plugins)
                }
            ))

        # content パス（どのファイルがスキャン対象か）
        content_paths = self._extract_content_paths(content)
        if content_paths:
            chunks.append(Chunk(
                id="tailwind:content",
                type="ui_config",
                name="tailwind_content_paths",
                content=content_paths,
                metadata={
                    "scan_patterns": self._parse_content_paths(content_paths)
                }
            ))

        return chunks

    def _extract_theme(self, content: str) -> str | None:
        """theme: { ... } または theme.extend: { ... } を抽出"""
        import re
        # 単純な正規表現（実際は AST パースが望ましい）
        match = re.search(r'theme:\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}', content, re.DOTALL)
        return match.group(0) if match else None

    def _extract_colors(self, theme: str) -> dict:
        """カスタムカラー定義を抽出"""
        import re
        colors = {}
        # colors: { primary: '#xxx', ... }
        color_match = re.search(r"colors:\s*\{([^}]+)\}", theme)
        if color_match:
            # 簡易パース
            for line in color_match.group(1).split(','):
                if ':' in line:
                    key, val = line.split(':', 1)
                    colors[key.strip().strip("'")] = val.strip().strip("',")
        return colors
```

#### 検索例

```
Query: "プライマリカラーを変更したい"
↓
ChromaDB search → tailwind:theme がヒット
↓
Response: "tailwind.config.js の theme.extend.colors.primary を変更。
  現在の値: '#3B82F6' (blue-500)
  ファイル: tailwind.config.js:15"
```

---

### 1.10 tree-sitter パーサ管理

#### 問題

PHP や Blade は標準の `tree-sitter-languages` パッケージに含まれていない、
またはバージョンが古い場合がある。環境構築時のつまずきポイントになりやすい。

#### 対策: setup.sh での明文化

```bash
# setup.sh に追加

echo "=== Installing tree-sitter parsers ==="

# Python パーサ（標準で含まれる）
python3 -c "import tree_sitter_languages; tree_sitter_languages.get_parser('python')" || {
    echo "Installing tree-sitter-languages..."
    pip install tree-sitter-languages
}

# PHP パーサの確認
python3 -c "import tree_sitter_languages; tree_sitter_languages.get_parser('php')" 2>/dev/null || {
    echo "WARNING: PHP parser not available in tree-sitter-languages"
    echo "Installing tree-sitter-php..."
    pip install tree-sitter-php
}

# TypeScript/JavaScript パーサ
python3 -c "import tree_sitter_languages; tree_sitter_languages.get_parser('typescript')" || {
    echo "Installing TypeScript parser..."
    pip install tree-sitter-typescript tree-sitter-javascript
}

# HTML パーサ
python3 -c "import tree_sitter_languages; tree_sitter_languages.get_parser('html')" || {
    echo "Installing HTML parser..."
    pip install tree-sitter-html
}

# CSS パーサ
python3 -c "import tree_sitter_languages; tree_sitter_languages.get_parser('css')" || {
    echo "Installing CSS parser..."
    pip install tree-sitter-css
}

echo "Tree-sitter parsers ready."
```

#### ASTChunker でのフォールバック

```python
class ASTChunker:
    def __init__(self, config: dict):
        self.parsers: dict[str, Parser] = {}
        self.fallback_languages: set[str] = set()

    def _get_parser(self, language: str) -> Parser | None:
        """パーサを取得（なければフォールバック登録）"""
        if language in self.fallback_languages:
            return None  # 行数ベースにフォールバック

        if language not in self.parsers:
            try:
                import tree_sitter_languages
                self.parsers[language] = tree_sitter_languages.get_parser(language)
            except Exception as e:
                logging.warning(f"Parser for {language} not available: {e}")
                logging.warning(f"Falling back to line-based chunking for {language}")
                self.fallback_languages.add(language)
                return None

        return self.parsers.get(language)

    def chunk_file(self, file_path: Path) -> list[Chunk]:
        language = detect_language(file_path)
        parser = self._get_parser(language)

        if parser is None:
            # フォールバック: 行数ベース
            return FallbackChunkStrategy().chunk_by_lines(
                file_path.read_text(),
                max_lines=50
            )

        # AST ベースのチャンク
        strategy = get_strategy(language)
        return self._chunk_with_ast(file_path, parser, strategy)
```

#### config.json での言語指定

```json
{
  "supported_languages": ["python", "php", "typescript", "javascript", "html", "css"],
  "fallback_languages": ["blade"],
  "parser_timeout_ms": 5000
}
```

---

### 1.11 言語マッピングの更新（最終版）

```python
CHUNK_STRATEGIES: dict[str, type[ChunkStrategy]] = {
    # バックエンド
    "python": PythonChunkStrategy,
    "php": PHPChunkStrategy,
    "java": JavaChunkStrategy,
    "go": GoChunkStrategy,

    # フロントエンド
    "typescript": TypeScriptChunkStrategy,
    "javascript": TypeScriptChunkStrategy,
    "html": HTMLChunkStrategy,
    "blade": BladeChunkStrategy,  # .blade.php
    "vue": VueChunkStrategy,      # .vue (SFC)

    # スタイル
    "css": CSSChunkStrategy,
    "scss": CSSChunkStrategy,

    # 設定ファイル
    "composer.json": ComposerJsonStrategy,
    "tailwind.config": TailwindConfigStrategy,
}

# ファイル拡張子→言語マッピング
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".php": "php",
    ".blade.php": "blade",  # .php より先にチェック
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

    # 複合拡張子を先にチェック
    if name.endswith(".blade.php"):
        return "blade"

    suffix = file_path.suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(suffix, "unknown")
```

---

### 1.12 FilamentResourceStrategy（Filament v4 対応）

#### 問題

Filament v4 の Resource クラスは独自の DSL パターンを持つ：

```php
class ProductResource extends Resource
{
    public static function form(Form $form): Form { ... }
    public static function table(Table $table): Table { ... }
    public static function infolist(Infolist $infolist): Infolist { ... }
}
```

通常の PHP クラスとして解析すると、これらのメソッドの「意味」が失われる。
「商品のフォームを修正したい」というクエリに対し、適切な Resource を特定できない。

#### 対策: FilamentResourceStrategy

```python
class FilamentResourceStrategy(PHPChunkStrategy):
    """Filament v4 Resource 専用 Strategy"""

    def get_chunk_node_types(self) -> list[str]:
        return [
            "class_declaration",
            "method_declaration",
        ]

    def is_filament_resource(self, file_path: Path, content: str) -> bool:
        """Filament Resource かどうかを判定"""
        return (
            "extends Resource" in content or
            "extends BaseResource" in content or
            "/Resources/" in str(file_path)
        )

    def chunk_file(self, file_path: Path) -> list[Chunk]:
        content = file_path.read_text()

        if not self.is_filament_resource(file_path, content):
            # 通常の PHP として処理
            return super().chunk_file(file_path)

        chunks = []

        # Resource クラス全体
        class_chunk = self._extract_resource_class(content, file_path)
        if class_chunk:
            chunks.append(class_chunk)

        # form() メソッド
        form_chunk = self._extract_filament_method(content, file_path, "form")
        if form_chunk:
            chunks.append(form_chunk)

        # table() メソッド
        table_chunk = self._extract_filament_method(content, file_path, "table")
        if table_chunk:
            chunks.append(table_chunk)

        # infolist() メソッド
        infolist_chunk = self._extract_filament_method(content, file_path, "infolist")
        if infolist_chunk:
            chunks.append(infolist_chunk)

        # getRelations() メソッド
        relations_chunk = self._extract_filament_method(content, file_path, "getRelations")
        if relations_chunk:
            chunks.append(relations_chunk)

        return chunks

    def _extract_resource_class(self, content: str, file_path: Path) -> Chunk | None:
        """Resource クラスのメタデータを抽出"""
        import re

        # クラス名を抽出
        class_match = re.search(r'class\s+(\w+Resource)\s+extends', content)
        if not class_match:
            return None

        class_name = class_match.group(1)

        # Model を抽出: protected static string $model = Product::class;
        model_match = re.search(r'\$model\s*=\s*(\w+)::class', content)
        model_name = model_match.group(1) if model_match else None

        # navigationIcon を抽出
        icon_match = re.search(r'\$navigationIcon\s*=\s*[\'"]([^\'"]+)[\'"]', content)
        icon = icon_match.group(1) if icon_match else None

        # navigationGroup を抽出
        group_match = re.search(r'\$navigationGroup\s*=\s*[\'"]([^\'"]+)[\'"]', content)
        group = group_match.group(1) if group_match else None

        return Chunk(
            id=f"filament:{class_name}",
            type="filament_resource",
            name=class_name,
            file=str(file_path),
            content=content[:500],  # クラス定義部分
            metadata={
                "fqcn": self._extract_fqcn(content, class_name),
                "model": model_name,
                "navigation_icon": icon,
                "navigation_group": group,
                "has_form": "function form(" in content,
                "has_table": "function table(" in content,
                "has_infolist": "function infolist(" in content,
            }
        )

    def _extract_filament_method(self, content: str, file_path: Path, method_name: str) -> Chunk | None:
        """Filament の特定メソッドを抽出"""
        import re

        # メソッド定義を検索
        pattern = rf'public\s+static\s+function\s+{method_name}\s*\([^)]*\)[^{{]*\{{(.*?)\n    \}}'
        match = re.search(pattern, content, re.DOTALL)

        if not match:
            return None

        method_content = match.group(0)

        # フィールド/カラム名を抽出
        fields = []
        if method_name == "form":
            # TextInput::make('name'), Select::make('category_id') 等
            fields = re.findall(r"::make\(['\"](\w+)['\"]\)", method_content)
        elif method_name == "table":
            # TextColumn::make('name'), etc.
            fields = re.findall(r"Column::make\(['\"]([^'\"]+)['\"]\)", method_content)

        class_match = re.search(r'class\s+(\w+Resource)', content)
        class_name = class_match.group(1) if class_match else "Unknown"

        return Chunk(
            id=f"filament:{class_name}:{method_name}",
            type=f"filament_{method_name}",
            name=f"{class_name}::{method_name}",
            file=str(file_path),
            content=method_content,
            metadata={
                "resource": class_name,
                "method": method_name,
                "fields": fields,
            }
        )
```

#### 検索例

```
Query: "商品のフォームに在庫数フィールドを追加したい"
↓
ChromaDB search → filament:ProductResource:form がヒット
↓
Response: "ProductResource の form() メソッドを修正。
  現在のフィールド: name, description, price, category_id
  ファイル: app/Admin/Resources/ProductResource.php:45"
```

#### ファイルパス判定の追加

```python
def detect_language(file_path: Path) -> str:
    """ファイルパスから言語を検出"""
    name = file_path.name
    path_str = str(file_path)

    # Filament Resource の判定
    if name.endswith("Resource.php") and "/Resources/" in path_str:
        return "filament_resource"

    # 以下既存ロジック...
```

#### Filament Actions のチャンク化

Filament には Actions（HeaderActions, TableActions, BulkActions）という重要な要素がある。
「削除ボタンの確認メッセージを変えて」といった依頼に対応するため、これらもチャンク対象に含める。

```python
class FilamentResourceStrategy(PHPChunkStrategy):
    def chunk_file(self, file_path: Path) -> list[Chunk]:
        # ... 既存コード ...

        # Actions 系メソッドを追加
        action_methods = [
            "getActions",
            "getBulkActions",
            "getHeaderActions",
            "getTableActions",
            "getFormActions",
        ]

        for method_name in action_methods:
            action_chunk = self._extract_filament_method(content, file_path, method_name)
            if action_chunk:
                chunks.append(action_chunk)

        return chunks

    def _extract_filament_method(self, content: str, file_path: Path, method_name: str) -> Chunk | None:
        # ... 既存コード ...

        # Actions 用のメタデータ抽出
        actions = []
        if "Actions" in method_name or "actions" in method_name.lower():
            # Action::make('delete'), DeleteAction::make() 等
            actions = re.findall(r"(\w+Action)::make\(['\"]?(\w*)['\"]?\)", method_content)

        return Chunk(
            # ...
            metadata={
                "resource": class_name,
                "method": method_name,
                "fields": fields,
                "actions": [{"type": a[0], "name": a[1] or a[0]} for a in actions],  # ← 追加
            }
        )
```

#### 検索例（Actions）

```
Query: "商品削除の確認メッセージを変更したい"
↓
ChromaDB search → filament:ProductResource:getHeaderActions がヒット
↓
Response: "ProductResource の getHeaderActions() に DeleteAction あり。
  DeleteAction::make()->requiresConfirmation()->modalHeading('...') で変更可能。
  ファイル: app/Admin/Resources/ProductResource.php:180"
```

---

### 1.13 TranslatableModelDetection（多言語 Model 対応）

#### 問題

`spatie/laravel-translatable` を使用する Model は、特定のカラムが JSON で多言語対応している：

```php
class Product extends Model
{
    use HasTranslations;

    public $translatable = ['name', 'description'];
}
```

「商品名を日本語で変更したい」というクエリに対し、`name` が翻訳可能カラムであることを知らないと、
通常の `$product->name = '新商品'` ではなく `$product->setTranslation('name', 'ja', '新商品')` を
使うべきことを判断できない。

#### 対策: PHPChunkStrategy への拡張

```python
class PHPChunkStrategy(ChunkStrategy):
    def extract_metadata(self, node, file_path: Path, file_content: str) -> dict:
        metadata = {}

        # 基本メタデータ
        metadata["fqcn"] = self._extract_fqcn(file_content)
        metadata["imports"] = self._extract_use_statements(file_content)

        # Translatable 検出
        translatable_fields = self._extract_translatable_fields(file_content)
        if translatable_fields:
            metadata["translatable"] = True
            metadata["translatable_fields"] = translatable_fields

        # Eloquent リレーション検出
        relations = self._extract_eloquent_relations(file_content)
        if relations:
            metadata["relations"] = relations

        return metadata

    def _extract_translatable_fields(self, content: str) -> list[str] | None:
        """$translatable プロパティから翻訳可能フィールドを抽出"""
        import re

        # use HasTranslations; があるか確認
        if "HasTranslations" not in content and "Translatable" not in content:
            return None

        # public $translatable = ['name', 'description'];
        match = re.search(
            r'\$translatable\s*=\s*\[([^\]]+)\]',
            content
        )

        if not match:
            return None

        # フィールド名を抽出
        fields_str = match.group(1)
        fields = re.findall(r"['\"](\w+)['\"]", fields_str)

        return fields if fields else None

    def _extract_eloquent_relations(self, content: str) -> list[dict]:
        """Eloquent リレーションを抽出"""
        import re
        relations = []

        # hasMany, belongsTo, belongsToMany, hasOne 等
        relation_patterns = [
            (r'function\s+(\w+)\s*\(\)[^{]*\{\s*return\s+\$this->hasMany\(([^)]+)\)', 'hasMany'),
            (r'function\s+(\w+)\s*\(\)[^{]*\{\s*return\s+\$this->belongsTo\(([^)]+)\)', 'belongsTo'),
            (r'function\s+(\w+)\s*\(\)[^{]*\{\s*return\s+\$this->belongsToMany\(([^)]+)\)', 'belongsToMany'),
            (r'function\s+(\w+)\s*\(\)[^{]*\{\s*return\s+\$this->hasOne\(([^)]+)\)', 'hasOne'),
        ]

        for pattern, relation_type in relation_patterns:
            for match in re.finditer(pattern, content, re.DOTALL):
                method_name = match.group(1)
                related_class = match.group(2).split(',')[0].strip().strip("'\"")
                relations.append({
                    "method": method_name,
                    "type": relation_type,
                    "related": related_class.replace("::class", ""),
                })

        return relations
```

#### ChromaDB への格納例

```python
collection.add(
    ids=["model:product"],
    documents=["class Product extends Model { ... }"],
    metadatas=[{
        "type": "model",
        "name": "Product",
        "fqcn": "App\\Models\\Product",
        "file": "app/Models/Product.php",
        "translatable": True,
        "translatable_fields": ["name", "description"],  # ← 翻訳可能フィールド
        "relations": [
            {"method": "category", "type": "belongsTo", "related": "Category"},
            {"method": "variants", "type": "hasMany", "related": "ProductVariant"},
        ]
    }]
)
```

#### 検索例

```
Query: "商品名を多言語対応で更新したい"
↓
ChromaDB search → model:product がヒット (translatable: true)
↓
Response: "Product モデルは多言語対応済み。
  翻訳可能フィールド: name, description
  更新方法: $product->setTranslation('name', 'ja', '新商品')
  ファイル: app/Models/Product.php"
```

#### Filament との連携

Filament で Translatable を使う場合、`TranslatableResource` トレイトが使われる：

```php
use Filament\Resources\Concerns\Translatable;

class ProductResource extends Resource
{
    use Translatable;
}
```

これも検出してメタデータに含める：

```python
def _extract_resource_class(self, content: str, file_path: Path) -> Chunk | None:
    # ... 既存コード ...

    # Translatable トレイト検出
    is_translatable = (
        "use Translatable;" in content or
        "Concerns\\Translatable" in content
    )

    return Chunk(
        # ...
        metadata={
            # ...
            "is_translatable": is_translatable,
        }
    )
```

---

### 1.14 Constructor Dependency Injection の解決

#### 問題

Laravel では、コントローラーやサービスのコンストラクタで依存が注入される：

```php
class OrderController extends Controller
{
    public function __construct(
        private OrderService $orderService,
        private PaymentGateway $paymentGateway,
    ) {}
}
```

「注文処理を修正したい」というクエリに対し、`OrderController` だけでなく
`OrderService` や `PaymentGateway` も関連ファイルとして特定する必要がある。

#### 対策: PHPChunkStrategy への DI 解決追加

```python
class PHPChunkStrategy(ChunkStrategy):
    def extract_metadata(self, node, file_path: Path, file_content: str) -> dict:
        metadata = {}

        # 基本メタデータ
        metadata["fqcn"] = self._extract_fqcn(file_content)
        metadata["imports"] = self._extract_use_statements(file_content)

        # Translatable 検出（既存）
        # ...

        # Constructor DI 検出
        injected_dependencies = self._extract_constructor_dependencies(file_content)
        if injected_dependencies:
            metadata["injected_dependencies"] = injected_dependencies
            # related_symbols に自動追加
            metadata["related_symbols"] = metadata.get("related_symbols", []) + [
                dep["class"] for dep in injected_dependencies
            ]

        return metadata

    def _extract_constructor_dependencies(self, content: str) -> list[dict]:
        """コンストラクタのタイプヒントから依存クラスを抽出"""
        import re
        dependencies = []

        # __construct メソッドを探す
        construct_match = re.search(
            r'function\s+__construct\s*\(([^)]*)\)',
            content,
            re.DOTALL
        )

        if not construct_match:
            return dependencies

        params_str = construct_match.group(1)

        # パラメータを解析
        # private OrderService $orderService, private PaymentGateway $paymentGateway
        # または
        # OrderService $orderService, PaymentGateway $paymentGateway
        param_pattern = r'(?:private|protected|public|readonly)?\s*(\w+)\s+\$(\w+)'

        for match in re.finditer(param_pattern, params_str):
            type_hint = match.group(1)
            var_name = match.group(2)

            # 基本型は除外
            if type_hint.lower() in ['string', 'int', 'float', 'bool', 'array', 'mixed', 'null']:
                continue

            dependencies.append({
                "class": type_hint,
                "variable": var_name,
            })

        return dependencies

    def _resolve_fqcn_from_imports(self, class_name: str, imports: list[str]) -> str | None:
        """use 文から FQCN を解決"""
        for import_stmt in imports:
            if import_stmt.endswith(f"\\{class_name}"):
                return import_stmt
            # use App\Services\OrderService as OS; のようなエイリアスも考慮
            if f" as {class_name}" in import_stmt:
                return import_stmt.split(" as ")[0]
        return None
```

#### ChromaDB への格納例

```python
collection.add(
    ids=["controller:order"],
    documents=["class OrderController extends Controller { ... }"],
    metadatas=[{
        "type": "controller",
        "name": "OrderController",
        "fqcn": "App\\Http\\Controllers\\OrderController",
        "file": "app/Http/Controllers/OrderController.php",
        "injected_dependencies": [
            {"class": "OrderService", "variable": "orderService"},
            {"class": "PaymentGateway", "variable": "paymentGateway"},
        ],
        "related_symbols": ["OrderService", "PaymentGateway"],  # ← 自動追加
    }]
)
```

#### 検索例

```
Query: "注文処理のロジックを修正したい"
↓
ChromaDB search → controller:order がヒット
↓
Response: "OrderController が見つかりました。
  依存サービス: OrderService, PaymentGateway
  ロジックは OrderService に実装されている可能性が高いです。
  関連ファイル:
  - app/Http/Controllers/OrderController.php
  - app/Services/OrderService.php
  - app/Services/PaymentGateway.php"
```

#### Filament Resource での適用

Filament の Resource でも `__construct` で依存注入が使われることがある：

```php
class ProductResource extends Resource
{
    public function __construct(
        private ProductRepository $repository,
    ) {}
}
```

FilamentResourceStrategy でも同様に DI を検出：

```python
class FilamentResourceStrategy(PHPChunkStrategy):
    def _extract_resource_class(self, content: str, file_path: Path) -> Chunk | None:
        # ... 既存コード ...

        # DI 検出を継承
        dependencies = self._extract_constructor_dependencies(content)

        return Chunk(
            # ...
            metadata={
                # ...
                "injected_dependencies": dependencies,
                "related_symbols": [dep["class"] for dep in dependencies],
            }
        )
```

---

### 2. 同期の UX（バックグラウンド処理）

#### 問題

大規模プロジェクトで `start_session` 時の同期が数秒〜十数秒かかると、LLM 対話がブロックされる。

#### 対策: 非同期インデックス + キャッシュ TTL

```python
# tools/chromadb_manager.py

import asyncio
from datetime import datetime, timedelta

class ChromaDBManager:
    def __init__(self, project_root: str, config: dict):
        self.project_root = Path(project_root)
        self.config = config

        # キャッシュ TTL（デフォルト: 1時間）
        self.sync_ttl = timedelta(hours=config.get("sync_ttl_hours", 1))
        self.last_sync_file = self.project_root / ".code-intel" / ".last_sync"

    def needs_sync(self) -> bool:
        """前回の同期から TTL が経過しているか"""
        if not self.last_sync_file.exists():
            return True

        last_sync = datetime.fromisoformat(self.last_sync_file.read_text().strip())
        return datetime.now() - last_sync > self.sync_ttl

    async def sync_forest_async(self, incremental: bool = True) -> asyncio.Task:
        """バックグラウンドで同期を開始"""
        task = asyncio.create_task(self._do_sync_forest(incremental))
        return task

    async def _do_sync_forest(self, incremental: bool):
        """実際の同期処理"""
        try:
            # ... 同期ロジック ...
            self.last_sync_file.write_text(datetime.now().isoformat())
        except Exception as e:
            # エラーログに記録、次回リトライ
            logging.error(f"Sync failed: {e}")


# start_session での使用
async def handle_start_session(...):
    session = SessionManager.create_session(...)
    session.chromadb = ChromaDBManager(repo_path, config)

    # 同期が必要なら非同期で開始（ブロックしない）
    if session.chromadb.needs_sync():
        session.sync_task = await session.chromadb.sync_forest_async()
        sync_status = "syncing_in_background"
    else:
        sync_status = "up_to_date"

    # 即座にレスポンス
    return {
        "session_id": session.id,
        "sync_status": sync_status,
        # ...
    }
```

#### config.json への追加

```json
{
  "embedding_model": "multilingual-e5-small",
  "sync_ttl_hours": 1,
  "sync_on_start": true,
  "sync_timeout_seconds": 30
}
```

---

### 3. モデルのウォームアップとタイムアウト

#### 問題

- モデル（約 400MB）の初回ダウンロード/ロードに時間がかかる
- MCP クライアントのタイムアウトで接続が切れるリスク

#### 対策: Eager Loading + setup.sh でプリロード

```bash
# setup.sh に追加

echo "Pre-downloading embedding model..."
python3 -c "
from sentence_transformers import SentenceTransformer
print('Downloading multilingual-e5-small...')
model = SentenceTransformer('intfloat/multilingual-e5-small')
print('Model downloaded and cached.')
"
```

```python
# code_intel_server.py - サーバー起動時に Eager Loading

import threading

class EmbeddingModelLoader:
    _instance = None
    _model = None
    _loading = False

    @classmethod
    def get_model(cls):
        if cls._model is None and not cls._loading:
            cls._loading = True
            # バックグラウンドでロード開始
            thread = threading.Thread(target=cls._load_model)
            thread.start()
        return cls._model

    @classmethod
    def _load_model(cls):
        from sentence_transformers import SentenceTransformer
        cls._model = SentenceTransformer('intfloat/multilingual-e5-small')
        cls._loading = False

# サーバー起動時に即座にロード開始
EmbeddingModelLoader.get_model()
```

#### MCP タイムアウト対策

```python
# 初回ツール呼び出し時にモデルがロード中なら待機メッセージ
async def handle_semantic_search(...):
    model = EmbeddingModelLoader.get_model()

    if model is None:
        return {
            "status": "initializing",
            "message": "Embedding model is loading. Please retry in a few seconds.",
            "retry_after_seconds": 5
        }

    # 通常処理
    ...
```

---

### 4. ハイブリッド検索（ベクトル × キーワード）

#### 問題

- ベクトル検索: 意味的な類似性に強い
- キーワード検索 (ripgrep): 固有名詞・変数名に強い
- どちらか一方では不十分

#### 対策: 統合スコアリングエンジン

```python
# tools/scoring_engine.py

from dataclasses import dataclass
from typing import Optional

@dataclass
class ScoredSymbol:
    name: str
    vector_score: float        # ChromaDB からの類似度
    keyword_hits: int          # ripgrep でのヒット数
    definition_found: bool     # find_definitions で見つかったか
    reference_count: int       # find_references での参照数
    final_score: float = 0.0   # 統合スコア

    def compute_final_score(self, weights: dict) -> float:
        """重み付き統合スコア"""
        self.final_score = (
            self.vector_score * weights.get("vector", 0.4) +
            min(self.keyword_hits / 10, 1.0) * weights.get("keyword", 0.2) +
            (1.0 if self.definition_found else 0.0) * weights.get("definition", 0.3) +
            min(self.reference_count / 20, 1.0) * weights.get("reference", 0.1)
        )
        return self.final_score


class HybridSearchEngine:
    """ベクトル検索とキーワード検索を統合"""

    def __init__(self, chromadb: ChromaDBManager, weights: Optional[dict] = None):
        self.chromadb = chromadb
        self.weights = weights or {
            "vector": 0.4,
            "keyword": 0.2,
            "definition": 0.3,
            "reference": 0.1
        }

    async def search(self, query: str, target_feature: str) -> list[ScoredSymbol]:
        # 1. ベクトル検索
        vector_results = self.chromadb.search_forest(query, n_results=20)

        # 2. キーワード検索（ripgrep）
        keyword_results = await search_text(query, max_results=50)

        # 3. シンボル定義検索
        # ... find_definitions, find_references ...

        # 4. スコア統合
        symbols = self._merge_results(vector_results, keyword_results, ...)

        # 5. スコアでソート
        for sym in symbols:
            sym.compute_final_score(self.weights)

        return sorted(symbols, key=lambda s: s.final_score, reverse=True)
```

#### config.json での重み調整

```json
{
  "search_weights": {
    "vector": 0.4,
    "keyword": 0.2,
    "definition": 0.3,
    "reference": 0.1
  }
}
```

---

### 5. ハードウェアリソース制限

#### 問題

- 低スペックマシンでメモリ圧迫
- 複数プロジェクト同時オープンでリソース競合

#### 対策: リソース制限 + 自動パージ

```python
# tools/chromadb_manager.py

class ChromaDBManager:
    def __init__(self, project_root: str, config: dict):
        # リソース制限
        self.max_chunks = config.get("max_chunks", 10000)
        self.max_index_size_mb = config.get("max_index_size_mb", 500)
        self.auto_purge_days = config.get("auto_purge_days", 30)

    def sync_forest(self, ...):
        # チャンク数制限チェック
        current_count = self.forest_collection.count()
        if current_count >= self.max_chunks:
            self._purge_old_chunks()

        # インデックスサイズチェック
        index_size = self._get_index_size_mb()
        if index_size >= self.max_index_size_mb:
            logging.warning(f"Index size {index_size}MB exceeds limit {self.max_index_size_mb}MB")
            self._purge_low_score_chunks()

    def _purge_old_chunks(self):
        """古いチャンクを削除"""
        cutoff = datetime.now() - timedelta(days=self.auto_purge_days)
        # メタデータの indexed_at で判定
        ...

    def _purge_low_score_chunks(self):
        """低スコアのチャンクを削除"""
        # 参照されていないチャンクを優先削除
        ...
```

#### config.json への追加

```json
{
  "max_chunks": 10000,
  "max_index_size_mb": 500,
  "auto_purge_days": 30,
  "memory_limit_mb": 1024
}
```

#### 軽量モードオプション

```json
{
  "lightweight_mode": true,
  "embedding_model": "multilingual-e5-small",
  "chunk_strategy": "lines",
  "max_chunks": 5000
}
```

---

## 更新: config.json 完全版

```json
{
  "embedding_model": "multilingual-e5-small",
  "source_dirs": ["src", "lib", "app"],
  "exclude_patterns": ["**/node_modules/**", "**/__pycache__/**", "**/venv/**"],

  "chunk_strategy": "ast",
  "chunk_max_tokens": 512,
  "supported_languages": ["python", "typescript", "javascript"],

  "sync_ttl_hours": 1,
  "sync_on_start": true,
  "sync_timeout_seconds": 30,

  "search_weights": {
    "vector": 0.4,
    "keyword": 0.2,
    "definition": 0.3,
    "reference": 0.1
  },

  "max_chunks": 10000,
  "max_index_size_mb": 500,
  "auto_purge_days": 30,
  "memory_limit_mb": 1024,

  "lightweight_mode": false
}
```

---

## Router への影響

### フェーズゲートの変更

#### v3.8 (現行)

```
EXPLORATION
├── 許可: code-intel ツール
├── 禁止: devrag MCP ツール
└── 理由: 外部 MCP 呼び出しを制御

SEMANTIC
├── 許可: devrag-forest MCP
├── 禁止: code-intel ツール
└── 理由: 推測フェーズを分離
```

#### v3.9 (提案)

```
EXPLORATION
├── 許可: code-intel ツール (search_text, find_definitions, etc.)
├── 許可: semantic_search(collection="map")  ← 地図は常に参照可
├── 禁止: semantic_search(collection="forest")
└── 理由: 地図（確定情報）は参照OK、森（推測）は禁止

SEMANTIC
├── 許可: semantic_search(collection="forest")
├── 禁止: code-intel 探索ツール
└── 理由: 推測フェーズを分離（同じ）
```

### Short-circuit Logic の変更

#### v3.8 (現行)

```python
# 外部 MCP 呼び出し
async def search_with_shortcircuit(query: str):
    # 1. devrag-map を MCP 経由で呼び出し
    map_result = await call_mcp_tool("devrag-map", "search", {"query": query})

    if map_result["score"] >= 0.7:
        return ShortCircuitResult(source="map", skip_forest=True)

    # 2. devrag-forest を MCP 経由で呼び出し
    forest_result = await call_mcp_tool("devrag-forest", "search", {"query": query})
    return ForestResult(source="forest", results=forest_result)
```

#### v3.9 (提案)

```python
# 内部メソッド呼び出し（高速）
async def search_with_shortcircuit(query: str, session: Session):
    chroma = session.chromadb_manager

    # 1. 地図を内部メソッドで検索（MCP オーバーヘッドなし）
    map_results = chroma.search_map(query, n_results=5)

    if map_results and map_results[0].score >= 0.7:
        return ShortCircuitResult(
            source="map",
            skip_forest=True,
            cached_symbols=map_results[0].metadata.get("symbols", [])
        )

    # 2. 森を内部メソッドで検索
    forest_results = chroma.search_forest(query, n_results=10)
    return ForestResult(source="forest", results=forest_results)
```

**メリット**: MCP プロトコルのオーバーヘッドがなくなり、検索が高速化

---

### ツール制限マトリクスの変更

#### v3.8 (現行)

| フェーズ | devrag-map | devrag-forest | code-intel | Edit/Write |
|----------|------------|---------------|------------|------------|
| EXPLORATION | 禁止 | 禁止 | 許可 | 禁止 |
| SEMANTIC | 許可 | 許可 | 禁止 | 禁止 |
| VERIFICATION | 禁止 | 禁止 | 許可 | 禁止 |
| READY | 許可 | 許可 | 許可 | 許可 |

#### v3.9 (提案)

| フェーズ | semantic_search (map) | semantic_search (forest) | code-intel | Edit/Write |
|----------|----------------------|--------------------------|------------|------------|
| EXPLORATION | **許可** | 禁止 | 許可 | 禁止 |
| SEMANTIC | 許可 | **許可** | 禁止 | 禁止 |
| VERIFICATION | 許可 | 禁止 | 許可 | 禁止 |
| READY | 許可 | 許可 | 許可 | 許可 |

**変更点**:
- `map` 検索は EXPLORATION でも許可（過去の成功パターンは常に参照可能）
- 外部 MCP → 内部メソッドに変更されるため、制御方法が変わる

---

### start_session の変更

#### v3.8 (現行)

```python
async def handle_start_session(intent: str, query: str, repo_path: str):
    session = SessionManager.create_session(intent, query, repo_path)

    return {
        "session_id": session.id,
        "phase": "EXPLORATION",
        "extraction_prompt": get_extraction_prompt(query),
        "map_search": "Use devrag-map for learned agreements",  # 外部 MCP への指示
        "forest_search": "Use devrag-forest for code search",
    }
```

#### v3.9 (提案)

```python
async def handle_start_session(intent: str, query: str, repo_path: str):
    session = SessionManager.create_session(intent, query, repo_path)

    # ChromaDB マネージャーを初期化
    session.chromadb = ChromaDBManager(repo_path)

    # 差分同期（変更があれば）
    if session.chromadb.needs_sync():
        sync_result = session.chromadb.sync_forest(incremental=True)
        # バックグラウンドで実行も可能

    # 地図を先に検索（Short-circuit チェック）
    map_hits = session.chromadb.search_map(query, n_results=3)

    if map_hits and map_hits[0].score >= 0.7:
        # 過去の成功パターンがヒット → EXPLORATION を一部スキップ可能
        return {
            "session_id": session.id,
            "phase": "EXPLORATION",
            "shortcircuit_hint": {
                "found": True,
                "symbols": map_hits[0].metadata.get("symbols", []),
                "confidence": map_hits[0].score,
                "recommendation": "Past success pattern found. Verify these symbols first."
            },
            "extraction_prompt": get_extraction_prompt(query),
        }

    return {
        "session_id": session.id,
        "phase": "EXPLORATION",
        "shortcircuit_hint": {"found": False},
        "extraction_prompt": get_extraction_prompt(query),
        "sync_status": sync_result if sync_result else "up_to_date"
    }
```

---

### submit_understanding の変更

#### v3.8 (現行)

```python
async def handle_submit_understanding(session_id: str, symbols_identified: list[str], ...):
    session = get_session(session_id)

    # mapped_symbols に追加
    for symbol in symbols_identified:
        session.query_frame.add_mapped_symbol(symbol, confidence=0.5)

    # validate_symbol_relevance を促す
    return {
        "next_step": "Call validate_symbol_relevance to verify symbols",
        "embedding_hint": "Use sentence-transformers for similarity"
    }
```

#### v3.9 (提案)

```python
async def handle_submit_understanding(session_id: str, symbols_identified: list[str], ...):
    session = get_session(session_id)

    # mapped_symbols に追加
    for symbol in symbols_identified:
        session.query_frame.add_mapped_symbol(symbol, confidence=0.5)

    # ChromaDB で即座に類似度を計算
    target_feature = session.query_frame.target_feature
    if target_feature:
        similarities = session.chromadb.compute_similarities(
            query=target_feature,
            symbols=symbols_identified
        )
        # 類似度を mapped_symbols に反映
        for symbol, score in similarities.items():
            session.query_frame.update_symbol_confidence(symbol, score)

    return {
        "symbols_with_confidence": [
            {"symbol": s.name, "confidence": s.confidence}
            for s in session.query_frame.mapped_symbols
        ],
        "next_step": "Call confirm_symbol_relevance with code_evidence",
        "high_confidence_symbols": [
            s.name for s in session.query_frame.mapped_symbols if s.confidence >= 0.6
        ]
    }
```

**変更点**: validate_symbol_relevance を待たずに、submit_understanding 内で類似度計算が可能に

---

### record_outcome の変更

#### v3.8 (現行)

```python
async def handle_record_outcome(session_id: str, outcome: str, ...):
    if outcome == "success":
        # agreements を生成
        manager = get_agreements_manager(repo_path)
        manager.create_agreement(...)

        # devrag-map を外部で sync する必要あり
        manager.trigger_devrag_sync()  # subprocess で devrag sync 呼び出し
```

#### v3.9 (提案)

```python
async def handle_record_outcome(session_id: str, outcome: str, ...):
    if outcome == "success":
        session = get_session(session_id)

        # agreements を生成
        manager = get_agreements_manager(repo_path)
        agreement_path = manager.create_agreement(...)

        # ChromaDB map に即座に追加（外部プロセス不要）
        session.chromadb.add_to_map(
            document_id=agreement_path.stem,
            content=agreement_path.read_text(),
            metadata={
                "nl_term": session.query_frame.target_feature,
                "symbols": [s.name for s in session.query_frame.mapped_symbols],
                "session_id": session_id
            }
        )

        return {
            "status": "success",
            "agreement_created": str(agreement_path),
            "map_updated": True  # 即座に反映
        }
```

**変更点**: 外部 devrag sync が不要に → 即座に地図に反映

---

### フロー図の変更

#### v3.9 全体フロー

```
ユーザークエリ
    │
    ▼
┌──────────────────┐
│   start_session  │
│ ┌──────────────┐ │
│ │ChromaDB init │ │
│ │ + 差分sync   │ │
│ └──────────────┘ │
│ ┌──────────────┐ │
│ │ map 検索     │ │  ← Short-circuit チェック
│ │(内部メソッド)│ │
│ └──────────────┘ │
└────────┬─────────┘
         │
    ┌────┴────┐
    │         │
  ヒット    ミス
  (≥0.7)
    │         │
    ▼         ▼
┌────────┐  ┌──────────────────┐
│HINT付き│  │   EXPLORATION    │
│EXPLORE │  │ (code-intel tools)│
└────────┘  └────────┬─────────┘
    │               │
    └───────┬───────┘
            ▼
┌──────────────────────┐
│ submit_understanding │
│ ┌──────────────────┐ │
│ │ ChromaDB で     │ │  ← 即座に類似度計算
│ │ 類似度計算      │ │
│ └──────────────────┘ │
└────────┬─────────────┘
         │
    ┌────┴────┐
    │         │
  十分      不十分
  (≥0.6)
    │         │
    ▼         ▼
┌────────┐  ┌──────────────────┐
│ READY  │  │    SEMANTIC      │
│        │  │ ┌──────────────┐ │
│        │  │ │forest 検索   │ │
│        │  │ │(内部メソッド)│ │
│        │  │ └──────────────┘ │
└────────┘  └────────┬─────────┘
    │               │
    │               ▼
    │       ┌──────────────────┐
    │       │  VERIFICATION    │
    │       └────────┬─────────┘
    │               │
    └───────┬───────┘
            ▼
┌──────────────────────┐
│   Edit/Write 実行    │
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│   record_outcome     │
│ ┌──────────────────┐ │
│ │ agreement 生成   │ │
│ │ + map に即追加   │ │  ← 外部 sync 不要
│ └──────────────────┘ │
└──────────────────────┘
```

---

### Router 実装の変更点まとめ

| コンポーネント | v3.8 | v3.9 | 影響度 |
|---------------|------|------|--------|
| フェーズ制御 | MCP ツール名で制御 | 内部メソッド + collection で制御 | 大 |
| Short-circuit | MCP 呼び出し後に判定 | start_session 内で即判定 | 中 |
| 類似度計算 | validate_symbol_relevance で別途 | submit_understanding 内で統合可 | 中 |
| 地図更新 | 外部 devrag sync | 内部 add_to_map | 小 |
| ツール制限 | MCP ツール名ブラックリスト | collection パラメータで制御 | 中 |

---

### Router コード修正箇所

```python
# tools/session.py の変更例

class Session:
    def __init__(self, ...):
        # 既存
        self.query_frame: QueryFrame
        self.phase: SessionPhase

        # v3.9 追加
        self.chromadb: ChromaDBManager  # 各セッションで保持

    def is_tool_allowed(self, tool_name: str, params: dict) -> bool:
        """v3.9: ツール使用可否の判定"""

        if tool_name == "semantic_search":
            collection = params.get("collection", "auto")

            if self.phase == SessionPhase.EXPLORATION:
                # map は許可、forest は禁止
                return collection in ["map", "auto"]  # auto は map 優先

            if self.phase == SessionPhase.SEMANTIC:
                # forest のみ許可
                return collection in ["forest", "auto"]

        # その他のツールは既存ロジック
        return self._legacy_tool_check(tool_name)
```

---

## 結論

ChromaDB への統一により:

1. **Python コードの意味検索**が可能に
2. **MCP サーバーが 1 つ**に簡素化
3. **AST ベースの高精度チャンク**
4. **プロジェクト分離**は維持
5. **Short-circuit Logic** も維持

v3.8 の設計思想を継承しつつ、実用性を大幅に向上させる。

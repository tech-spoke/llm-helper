# Code Intelligence MCP Server

Cursor IDEのようなコードインテリジェンス機能をオープンソースツールで実現するMCPサーバー。

## 機能

| ツール | 用途 |
|--------|------|
| `repo_pack` | リポジトリ全体をLLM用にパック (Repomix) |
| `search_text` | 高速テキスト検索 (ripgrep) |
| `search_files` | ファイル名検索 (ripgrep) |
| `analyze_structure` | コード構造解析 (tree-sitter) |
| `find_definitions` | シンボル定義検索 (ctags) |
| `find_references` | シンボル参照検索 (ctags) |
| `get_symbols` | シンボル一覧取得 (ctags) |
| `get_function_at_line` | 特定行の関数取得 (tree-sitter) |
| `query` | 自然言語でのインテリジェントクエリ |

## 依存関係

| ツール | 必須 | 用途 |
|--------|------|------|
| ripgrep (rg) | Yes | search_text, search_files, find_references |
| universal-ctags | Yes | find_definitions, find_references, get_symbols |
| Python 3.10+ | Yes | サーバー本体 |
| tree-sitter | Yes | analyze_structure (pip で自動インストール) |
| repomix | No | repo_pack, bootstrapキャッシュ |

### devrag（オプション：意味検索フォールバック）

> **devragは完全にオプションです。** 未導入でもコードインテリジェンス機能（search_text, find_definitions等）は正常に動作します。

devragはRouterに統合されたフォールバック機構です:

```
Query → Router → コードツール実行 → 結果不十分? → devrag発動
                                    ↓
                              結果十分 → 終了
```

- コードベースのツールで結果が不十分な場合に**自動発動**
- 曖昧なクエリや意味的な質問で効果を発揮
- **最後の保険**として設計

#### devragのインストール

GitHub Releases: https://github.com/tomohiro-owada/devrag/releases

```bash
# Linux x64
wget https://github.com/tomohiro-owada/devrag/releases/latest/download/devrag-linux-x64.tar.gz
tar xzf devrag-linux-x64.tar.gz
sudo mv devrag /usr/local/bin/

# macOS Apple Silicon
wget https://github.com/tomohiro-owada/devrag/releases/latest/download/devrag-macos-apple-silicon.tar.gz
tar xzf devrag-macos-apple-silicon.tar.gz
sudo mv devrag /usr/local/bin/

# 確認
devrag -version
```

#### devragの設定

対象プロジェクトのルートに `rag-custom-config.json` を作成:

```json
{
    "document_patterns": [
        "./src",
        "./docs"
    ],
    "db_path": "./vectors.db",
    "chunk_size": 500,
    "search_top_k": 5,
    "compute": {
        "device": "auto",
        "fallback_to_cpu": true
    },
    "model": {
        "name": "multilingual-e5-small",
        "dimensions": 384
    }
}
```

| 設定 | 説明 |
|------|------|
| document_patterns | インデックス対象ディレクトリ |
| db_path | ベクトルDBの保存先 |
| chunk_size | ドキュメント分割サイズ |
| search_top_k | 検索結果数 |
| model.name | 埋め込みモデル |

#### devragのインデックス作成

```bash
cd /path/to/your/project
devrag -config rag-custom-config.json index
```

#### devragのMCP設定（任意）

code-intelのフォールバックはdevrag CLIを直接呼び出すため、**MCP設定は不要**です。

Claude Codeからdevragを直接使いたい場合のみ、`.mcp.json`に追加:

```json
{
  "mcpServers": {
    "devrag": {
      "type": "stdio",
      "command": "/usr/local/bin/devrag",
      "args": ["--config", "rag-custom-config.json"]
    }
  }
}
```

#### devragがない場合

- 基本機能（search_text, find_definitions等）は正常動作
- フォールバックがスキップされ、結果が不十分なまま返る可能性あり

## セットアップ

### 1. 依存ツールのインストール

#### 必須

```bash
# Ubuntu/Debian
sudo apt install ripgrep universal-ctags

# macOS
brew install ripgrep universal-ctags
```

#### 任意

```bash
# Repomix - repo_pack機能に必要 (Node.js必須)
npm install -g repomix
```

### 2. サーバーのセットアップ

```bash
cd /home/kazuki/public_html/llm-helper

# セットアップスクリプト実行
./setup.sh
```

または手動で:

```bash
# 仮想環境作成
python3 -m venv venv

# 依存関係インストール
./venv/bin/pip install mcp tree-sitter tree-sitter-languages
```

### 3. 他プロジェクトでの設定

#### 方法A: プロジェクトローカル設定（推奨）

対象プロジェクトのルートに `.mcp.json` を作成:

```json
{
  "mcpServers": {
    "code-intel": {
      "type": "stdio",
      "command": "/home/kazuki/public_html/llm-helper/venv/bin/python",
      "args": ["/home/kazuki/public_html/llm-helper/code_intel_server.py"],
      "env": {
        "PYTHONPATH": "/home/kazuki/public_html/llm-helper"
      }
    }
  }
}
```

#### 方法B: グローバル設定（全プロジェクト共通）

`~/.claude/mcp.json` に追加:

```json
{
  "mcpServers": {
    "code-intel": {
      "type": "stdio",
      "command": "/home/kazuki/public_html/llm-helper/venv/bin/python",
      "args": ["/home/kazuki/public_html/llm-helper/code_intel_server.py"],
      "env": {
        "PYTHONPATH": "/home/kazuki/public_html/llm-helper"
      }
    }
  }
}
```

### 4. 設定の確認

Claude Codeを再起動し、MCPツールが認識されているか確認:

```bash
# Claude Codeを起動
claude

# /mcp コマンドでMCPサーバー一覧を確認
/mcp
```

`code-intel` サーバーが表示されればOK。

## 利用方法

### 基本的なツール呼び出し

Claude Codeから直接ツールを呼び出せます:

```
# テキスト検索
mcp__code-intel__search_text でパターン "Router" を検索して

# 定義検索
mcp__code-intel__find_definitions で "SessionBootstrap" の定義を探して

# 構造解析
mcp__code-intel__analyze_structure で tools/router.py を解析して

# シンボル一覧
mcp__code-intel__get_symbols で tools/ 配下のシンボルを取得して
```

### インテリジェントクエリ（推奨）

`query` ツールを使うと、質問に応じて適切なツールを自動選択:

```
mcp__code-intel__query で「Router classはどこで定義されている？」を調べて

mcp__code-intel__query で「この関数は何をしている？」を調べて
```

#### queryの動作

1. 質問を4カテゴリに分類
   - A_SYNTAX: 定義場所、構文
   - B_REFERENCE: 参照、呼び出し元
   - C_SEMANTIC: 目的、設計意図
   - D_IMPACT: 影響範囲、変更分析

2. カテゴリに応じたツールを自動選択

3. 結果が不十分な場合はdevrag（意味検索）にフォールバック

### 出力例

```json
{
  "question": "Where is Router defined?",
  "categories": ["A_SYNTAX"],
  "intent_confidence": "low",
  "force_devrag": true,
  "results": [
    {
      "file_path": "tools/router.py",
      "symbol_name": "Router",
      "start_line": 792,
      "source_tool": "find_definitions"
    }
  ],
  "decision_log": {
    "classification": {
      "categories": ["A_SYNTAX"],
      "confidence": "low",
      "pattern_match_count": 1,
      "ambiguous": true
    },
    "fallback": {
      "triggered": false,
      "reason": "devrag already executed",
      "threshold": 1
    }
  }
}
```

## アーキテクチャ

```
User Query
    |
    v
+-------------------+
|      Router       |
|  - QueryClassifier (質問分類)
|  - ToolSelector   (ツール選択)
|  - FallbackDecider (フォールバック判定)
+-------------------+
    |
    v
+-------------------+
|   Tool Execution  |
|  ripgrep, ctags,  |
|  tree-sitter, etc |
+-------------------+
    |
    v
+-------------------+
| ResultIntegrator  |
|  結果の統合・重複排除 |
+-------------------+
```

## v3.1 機能

### Decision Log

全ての判断理由を構造化ログとして記録:

```json
{
  "classification": { "categories": [...], "confidence": "..." },
  "tool_selection": { "tools_planned": [...], "force_devrag": ... },
  "fallback": { "triggered": ..., "reason": "...", "threshold": ... }
}
```

### Cache Invalidation

repo_packのキャッシュを簡易チェックで無効化:
- ファイル数が変わったら無効化
- ファイルが更新されたら（mtime変化）無効化

※ 簡易的なチェックであり、差分解析や依存関係追跡は行いません。

## トラブルシューティング

### MCPサーバーが認識されない

```bash
# サーバーを直接起動してエラー確認
/home/kazuki/public_html/llm-helper/venv/bin/python \
  /home/kazuki/public_html/llm-helper/code_intel_server.py
```

### 依存ツールが見つからない

```bash
# 各ツールの存在確認
which rg        # ripgrep
which ctags     # universal-ctags
which repomix   # repomix
```

### devragフォールバックが動作しない

devragはRouterに統合されたフォールバック機構です。動作しない場合:

```bash
# devrag CLIが存在するか確認
which devrag

# 直接実行してエラー確認
devrag search "test query" --path . --format json
```

devragが未設定でも基本機能は動作しますが、結果が不十分な場合の補完ができません。

## ライセンス

MIT

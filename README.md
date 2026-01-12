# Code Intelligence MCP Server v3.6

Cursor IDEのようなコードインテリジェンス機能をオープンソースツールで実現するMCPサーバー。

## なぜ必要か

同じ Opus 4.5 モデルでも、呼び出し元によって挙動が異なる：

| 呼び出し元 | 挙動 |
|-----------|------|
| **Cursor** | コードベース全体を理解した上で修正する |
| **Claude Code** | 修正箇所だけを見て修正する傾向がある |

このMCPサーバーは、Claude Codeに「コードベースを理解させる情報」を提供します。

## v3.6 の特徴

### フェーズゲート実行

LLMが探索をスキップできないよう、物理的に制限：

```
EXPLORATION → SEMANTIC → VERIFICATION → READY
     ↓            ↓           ↓           ↓
  code-intel   devrag      検証       実装許可
   ツール      (仮説)     (確定)
```

### QueryFrame

自然文を4+1スロットで構造化：

| スロット | 説明 | 例 |
|----------|------|-----|
| `target_feature` | 対象機能 | 「ログイン機能」 |
| `trigger_condition` | 再現条件 | 「パスワードが空のとき」 |
| `observed_issue` | 問題 | 「エラーが出ない」 |
| `desired_action` | 期待 | 「チェックを追加」 |
| `mapped_symbols` | 探索で見つけたシンボル | `["LoginService"]` |

### 設計原則

| 原則 | 実装 |
|------|------|
| LLMに判断をさせない | confidence はサーバーが算出 |
| 幻覚を物理的に排除 | Quote検証（引用が原文にあるか確認） |
| 動的な要件調整 | risk_level (HIGH/MEDIUM/LOW) で探索要件を変更 |
| 情報の確実性を追跡 | FACT（確定）vs HYPOTHESIS（要検証） |

## ツール一覧

### コードインテリジェンス

| ツール | 用途 |
|--------|------|
| `query` | 自然言語でのインテリジェントクエリ |
| `search_text` | 高速テキスト検索 (ripgrep) |
| `search_files` | ファイル名検索 (ripgrep) |
| `analyze_structure` | コード構造解析 (tree-sitter) |
| `find_definitions` | シンボル定義検索 (ctags) |
| `find_references` | シンボル参照検索 (ctags) |
| `get_symbols` | シンボル一覧取得 (ctags) |
| `get_function_at_line` | 特定行の関数取得 (tree-sitter) |
| `repo_pack` | リポジトリ全体をLLM用にパック (Repomix) |

### セッション管理（v3.6）

| ツール | 用途 |
|--------|------|
| `start_session` | セッション開始、extraction_prompt を返す |
| `set_query_frame` | QueryFrame 設定（Quote検証付き） |
| `get_session_status` | 現在のフェーズ・状態を確認 |
| `submit_understanding` | EXPLORATION 完了 |
| `submit_semantic` | SEMANTIC 完了 |
| `submit_verification` | VERIFICATION 完了 |
| `check_write_target` | Write 可否確認（探索済みファイルのみ許可） |
| `record_outcome` | 結果を記録（改善サイクル用） |

## スキル

### /code スキル

コード実装を支援するエージェント。フェーズゲートに従って探索→実装を行います。

```
/code ログイン機能でパスワードが空のときエラーが出ないバグを直して
```

### /outcome スキル

実装結果を記録するエージェント。失敗パターンの分析に使用。

```
/outcome この実装は失敗だった
```

## 依存関係

| ツール | 必須 | 用途 |
|--------|------|------|
| ripgrep (rg) | Yes | search_text, search_files, find_references |
| universal-ctags | Yes | find_definitions, find_references, get_symbols |
| Python 3.10+ | Yes | サーバー本体 |
| tree-sitter | Yes | analyze_structure (pip で自動インストール) |
| repomix | No | repo_pack, bootstrapキャッシュ |
| devrag | No | 意味検索フォールバック |

## セットアップ

### 1. 依存ツールのインストール

```bash
# Ubuntu/Debian
sudo apt install ripgrep universal-ctags

# macOS
brew install ripgrep universal-ctags

# 任意: Repomix
npm install -g repomix
```

### 2. サーバーのセットアップ

```bash
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper

# セットアップスクリプト実行
./setup.sh
```

または手動で:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3. MCP設定

対象プロジェクトのルートに `.mcp.json` を作成:

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

### 4. スキルの設定（任意）

`.claude/commands/` に `code.md` と `outcome.md` をコピー:

```bash
mkdir -p .claude/commands
cp /path/to/llm-helper/.claude/commands/*.md .claude/commands/
```

## 利用方法

### /code スキルを使う（推奨）

```
/code AuthServiceのlogin関数でパスワードが空のときエラーが出ないバグを直して
```

スキルが自動的に:
1. Intent判定（MODIFY）
2. セッション開始
3. QueryFrame抽出・検証
4. EXPLORATION（find_definitions, find_references等）
5. 必要に応じてSEMANTIC（devrag）
6. VERIFICATION（仮説検証）
7. READY（実装）

### 直接ツールを呼び出す

```
# テキスト検索
mcp__code-intel__search_text でパターン "Router" を検索して

# 定義検索
mcp__code-intel__find_definitions で "SessionState" の定義を探して

# 構造解析
mcp__code-intel__analyze_structure で tools/router.py を解析して
```

## フェーズの詳細

### EXPLORATION

code-intelツールでコードベースを探索：
- `find_definitions`: シンボルの定義場所
- `find_references`: シンボルの使用箇所
- `search_text`: テキストパターン検索
- `analyze_structure`: AST解析

**devragは使用禁止**（物理的にブロック）

### SEMANTIC

探索結果が不十分な場合のみ発動：
- `devrag_search`: 意味検索
- 結果は **HYPOTHESIS**（仮説）として記録

### VERIFICATION

HYPOTHESISをcode-intelツールで検証：
- 確認されれば **FACT** に昇格
- 否定されれば **rejected** として記録

**devragは使用禁止**（物理的にブロック）

### READY

実装が許可される：
- HYPOTHESISが残っていないことを確認
- Write対象は探索済みファイルのみ許可

## devrag（オプション）

devragは意味検索のフォールバック機構です。未導入でも基本機能は動作します。

### インストール

```bash
# Linux x64
wget https://github.com/tomohiro-owada/devrag/releases/latest/download/devrag-linux-x64.tar.gz
tar xzf devrag-linux-x64.tar.gz
sudo mv devrag /usr/local/bin/
```

### 設定

対象プロジェクトに `rag-custom-config.json` を作成:

```json
{
  "document_patterns": ["./src", "./docs"],
  "db_path": "./vectors.db",
  "chunk_size": 500,
  "search_top_k": 5,
  "model": {
    "name": "multilingual-e5-small",
    "dimensions": 384
  }
}
```

### インデックス作成

```bash
devrag -config rag-custom-config.json index
```

## ドキュメント

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - システム設計
- [ROUTER.md](docs/ROUTER.md) - Router詳細
- [DESIGN_v3.6.md](docs/DESIGN_v3.6.md) - v3.6設計

## ライセンス

MIT

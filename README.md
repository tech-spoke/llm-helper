# Code Intelligence MCP Server v3.9

Cursor IDE のようなコードインテリジェンス機能をオープンソースツールで実現する MCP サーバー。

## v3.9 新機能

- **ChromaDB ベースの意味検索**: devrag を置換し、内蔵の ChromaDB でベクトル検索
- **AST ベースのチャンキング**: PHP, Python, JS, Blade 等を構文解析してチャンク化
- **フィンガープリント増分同期**: SHA256 ハッシュベースで変更ファイルのみ再インデックス
- **自動同期**: セッション開始時に必要に応じて自動同期
- **Short-circuit**: 地図で高スコア（≥0.7）なら森の探索をスキップ

---

## 目的

同じ Opus 4.5 モデルでも、呼び出し元によって挙動が異なる：

| 呼び出し元 | 挙動 |
|-----------|------|
| **Cursor** | コードベース全体を理解した上で修正する |
| **Claude Code** | 修正箇所だけを見て修正する傾向がある |

この MCP サーバーは、Claude Code に「コードベースを理解させる」ための仕組みを提供する。

---

## 設計思想

```
LLM に判断をさせない。守らせるのではなく、守らないと進めない設計。
そして、失敗から学ぶ仕組みを持つ。
```

| 原則 | 実装 |
|------|------|
| フェーズ強制 | ツール使用制限（EXPLORATION で semantic_search 禁止等） |
| サーバー評価 | confidence はサーバーが算出、LLM の自己申告を排除 |
| 構造化入力 | Quote 検証による幻覚防止 |
| Embedding 検証 | NL→Symbol の関連性をベクトル類似度で客観評価 |
| Write 制限 | 探索済みファイルのみ許可 |
| 改善サイクル | Outcome Log + agreements による学習 |
| プロジェクト分離 | 各プロジェクトごとに独立した学習データ |

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Clients (Claude Code)                 │
└─────────────────────────────────────────────────────────────┘
                               │
                               ▼
                    ┌─────────────────┐
                    │   code-intel    │  ← 統合 MCP サーバー
                    │ (オーケストレータ) │
                    └─────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
        │ ChromaDB    │ │ ripgrep     │ │ ctags       │
        │ (map/forest)│ │ (検索)      │ │ (シンボル)   │
        └─────────────┘ └─────────────┘ └─────────────┘
               │
               ▼
    ┌───────────────────┐
    │ Project/.code-intel│  ← プロジェクト固有
    │ ├─ config.json     │
    │ ├─ chroma/         │  ← ChromaDB データ
    │ ├─ agreements/     │
    │ ├─ sync_state.json │
    │ └─ learned_pairs.json│
    └───────────────────┘
```

### 森と地図

| 名称 | コレクション | 役割 | データの性質 |
|------|-------------|------|-------------|
| **森 (Forest)** | forest | ソースコード全体の意味検索 | 生データ・HYPOTHESIS |
| **地図 (Map)** | map | 過去の成功ペア・合意事項 | 確定データ・FACT |

**Short-circuit Logic**: 地図でスコア ≥ 0.7 → 森の探索をスキップ

---

## フェーズゲート

```
EXPLORATION → VALIDATION → SEMANTIC → VERIFICATION → READY
     ↓            ↓           ↓           ↓           ↓
  code-intel  Embedding  semantic    検証       実装許可
   ツール      検証       search     (確定)
                         (仮説)
```

| フェーズ | 許可 | 禁止 |
|----------|------|------|
| EXPLORATION | code-intel ツール | semantic_search |
| SEMANTIC | semantic_search | code-intel |
| VERIFICATION | code-intel ツール | semantic_search |
| READY | すべて | - |

---

## ツール一覧

### コードインテリジェンス

| ツール | 用途 |
|--------|------|
| `search_text` | テキスト検索 (ripgrep) |
| `search_files` | ファイル名検索 |
| `find_definitions` | シンボル定義検索 (ctags) |
| `find_references` | シンボル参照検索 (ctags) |
| `get_symbols` | シンボル一覧取得 |
| `analyze_structure` | コード構造解析 (tree-sitter) |
| `get_function_at_line` | 特定行の関数取得 |
| `repo_pack` | リポジトリパック (Repomix) |
| `query` | 自然言語でのインテリジェントクエリ |

### v3.9 新ツール

| ツール | 用途 |
|--------|------|
| `sync_index` | ソースコードを ChromaDB にインデックス（増分同期） |
| `semantic_search` | 地図/森の統合ベクトル検索（Short-circuit 対応） |

### セッション管理

| ツール | 用途 |
|--------|------|
| `start_session` | セッション開始、extraction_prompt を返す、自動同期 |
| `set_query_frame` | QueryFrame 設定（Quote 検証） |
| `get_session_status` | 現在のフェーズ・状態を確認 |
| `submit_understanding` | EXPLORATION 完了、mapped_symbols 自動追加 |
| `validate_symbol_relevance` | Embedding 提案を取得 |
| `confirm_symbol_relevance` | 検証結果確定、confidence 更新 |
| `submit_semantic` | SEMANTIC 完了 |
| `submit_verification` | VERIFICATION 完了 |
| `check_write_target` | Write 可否確認 |
| `record_outcome` | 結果記録、agreements 自動生成 |

---

## 依存関係

### システムツール

| ツール | 必須 | 用途 |
|--------|------|------|
| ripgrep (rg) | Yes | search_text, search_files, find_references |
| universal-ctags | Yes | find_definitions, find_references, get_symbols |
| Python 3.10+ | Yes | サーバー本体 |
| repomix | No | repo_pack |

### Python パッケージ

```
mcp>=1.0.0
chromadb>=1.0.0
tree-sitter>=0.21.0
tree-sitter-languages>=1.10.0
sentence-transformers>=2.2.0
scikit-learn>=1.0.0
pytest>=7.0.0
```

---

## セットアップ

### Step 1: MCP サーバーのセットアップ（1回のみ）

```bash
# リポジトリをクローン
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper

# サーバーをセットアップ（venv、依存関係）
./setup.sh
```

### Step 2: プロジェクトの初期化（プロジェクトごと）

```bash
# 対象プロジェクトを初期化
./init-project.sh /path/to/your-project

# オプション: ソースディレクトリを指定
./init-project.sh /path/to/your-project --src-dirs=src,packages,modules
```

これにより以下が作成されます：

```
your-project/
└── .code-intel/
    ├── config.json       ← v3.9 設定
    ├── chroma/           ← ChromaDB データ（自動生成）
    └── agreements/       ← 合意事項ディレクトリ
```

### Step 3: .mcp.json の設定

`init-project.sh` が出力する設定を `.mcp.json` に追加：

```json
{
  "mcpServers": {
    "code-intel": {
      "type": "stdio",
      "command": "/path/to/llm-helper/venv/bin/python",
      "args": ["/path/to/llm-helper/code_intel_server.py"],
      "env": {"PYTHONPATH": "/path/to/llm-helper"}
    }
  }
}
```

**Note**: v3.9 では devrag-map/devrag-forest の設定は不要です。ChromaDB が内蔵されています。

### Step 4: スキルの設定（任意）

```bash
mkdir -p /path/to/your-project/.claude/commands
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 5: Claude Code を再起動

MCP サーバーを読み込むために再起動。

初回セッション開始時に自動的にインデックスが構築されます。

---

## 利用方法

### /code スキルを使う（推奨）

```
/code AuthServiceのlogin関数でパスワードが空のときエラーが出ないバグを直して
```

スキルが自動的に：
1. Intent 判定（MODIFY）
2. セッション開始（自動同期）
3. QueryFrame 抽出・検証
4. 地図を検索（過去の成功パターン）
5. EXPLORATION（find_definitions, find_references 等）
6. シンボル検証（Embedding）
7. 必要に応じて SEMANTIC（semantic_search）
8. VERIFICATION（仮説検証）
9. READY（実装）
10. 成功時に地図を更新

### 直接ツールを呼び出す

```
# テキスト検索
mcp__code-intel__search_text でパターン "Router" を検索して

# 定義検索
mcp__code-intel__find_definitions で "SessionState" の定義を探して

# 意味検索（v3.9）
mcp__code-intel__semantic_search でクエリ "ログイン機能" を検索して

# 同期（v3.9）
mcp__code-intel__sync_index で強制再インデックスして
```

---

## スキル

### /code

コード実装を支援するエージェント。フェーズゲートに従って探索→実装を行う。

```
/code ログイン機能でパスワードが空のときエラーが出ないバグを直して
```

### /outcome

実装結果を記録するエージェント。失敗パターンの分析に使用。

```
/outcome この実装は失敗だった
```

---

## 合意事項（Agreements）

成功した NL→Symbol ペアは `.code-intel/agreements/` に Markdown として保存され、ChromaDB の map コレクションでベクトル検索可能になる：

```markdown
---
doc_type: agreement
nl_term: ログイン機能
symbols: ["AuthService", "login"]
similarity: 0.85
session_id: session_20250112_143000
created_at: 2025-01-12T14:30:00
---

# ログイン機能 → AuthService, login

## 根拠 (Code Evidence)

AuthService.login() がユーザー認証を処理

## 関連シンボル

- `AuthService`
- `login`
```

次回以降の探索で優先的に参照される。

---

## プロジェクト構造

### MCP サーバー（llm-helper/）

```
llm-helper/
├── code_intel_server.py    ← MCP サーバー本体
├── tools/                  ← ツール実装
│   ├── session.py
│   ├── query_frame.py
│   ├── ast_chunker.py      ← v3.9: AST チャンキング
│   ├── sync_state.py       ← v3.9: 同期状態管理
│   ├── chromadb_manager.py ← v3.9: ChromaDB 管理
│   ├── agreements.py
│   └── learned_pairs.py
├── setup.sh                ← サーバーセットアップ
├── init-project.sh         ← プロジェクト初期化
└── .claude/commands/       ← スキル定義
    ├── code.md
    └── outcome.md
```

### 対象プロジェクト

```
your-project/
├── .mcp.json               ← MCP 設定（手動設定）
├── .code-intel/            ← Code Intel データ（自動生成）
│   ├── config.json         ← v3.9 設定
│   ├── chroma/             ← ChromaDB データ
│   │   ├── map/            ← 合意事項のベクトル
│   │   └── forest/         ← ソースコードのベクトル
│   ├── agreements/         ← 成功ペア（自動生成）
│   ├── sync_state.json     ← 同期状態（自動生成）
│   └── learned_pairs.json  ← キャッシュ（自動生成）
├── .claude/commands/       ← スキル（任意コピー）
└── src/                    ← あなたのソースコード
```

---

## v3.8 からの移行

v3.9 は v3.8 と後方互換性があります：

1. `setup.sh` を再実行（chromadb をインストール）
2. プロジェクトで `init-project.sh` を再実行（config.json を生成）
3. `.mcp.json` から devrag-map/devrag-forest を削除可能（任意）
4. Claude Code を再起動

既存の agreements は自動的に ChromaDB に取り込まれます。

---

## ドキュメント

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - システム設計
- [ROUTER.md](docs/ROUTER.md) - Router 詳細
- [DESIGN_v3.9_chromadb.md](docs/DESIGN_v3.9_chromadb.md) - v3.9 設計詳細

---

## ライセンス

MIT

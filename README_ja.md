# Code Intelligence MCP Server v1.1

Cursor IDE のようなコードインテリジェンス機能をオープンソースツールで実現する MCP サーバー。

## 概要

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
| 改善サイクル | DecisionLog + OutcomeLog + agreements による学習 |
| 自動失敗検出 | /code 開始時に前回失敗を自動判定・記録 |
| プロジェクト分離 | 各プロジェクトごとに独立した学習データ |
| 必須コンテキスト（v1.1） | セッション開始時に設計ドキュメントとプロジェクトルールを自動提供 |
| 影響範囲分析（v1.1） | READY フェーズ前に影響確認を強制 |

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
    │ ├─ logs/           │  ← DecisionLog, OutcomeLog
    │ └─ sync_state.json │
    └───────────────────┘
```

### 森と地図（Forest/Map）

| 名称 | コレクション | 役割 | データの性質 |
|------|-------------|------|-------------|
| **森 (Forest)** | forest | ソースコード全体の意味検索 | 生データ・HYPOTHESIS |
| **地図 (Map)** | map | 過去の成功ペア・合意事項 | 確定データ・FACT |

**Short-circuit Logic**: 地図でスコア ≥ 0.7 → 森の探索をスキップ

---

## フェーズゲート

```
EXPLORATION → SEMANTIC → VERIFICATION → IMPACT ANALYSIS → READY
     ↓           ↓           ↓               ↓               ↓
  code-intel  semantic    検証          analyze_impact   実装許可
   ツール      search     (確定)          (v1.1)
             (仮説)
```

| フェーズ | 許可 | 禁止 |
|----------|------|------|
| EXPLORATION | code-intel ツール | semantic_search |
| SEMANTIC | semantic_search | code-intel |
| VERIFICATION | code-intel ツール | semantic_search |
| IMPACT ANALYSIS | analyze_impact, code-intel | semantic_search |
| READY | すべて | - |

---

## ツール一覧

### コードインテリジェンス

| ツール | 用途 |
|--------|------|
| `query` | 自然言語でのインテリジェントクエリ |
| `find_definitions` | シンボル定義検索 (ctags) |
| `find_references` | シンボル参照検索 (ripgrep) |
| `search_text` | テキスト検索 (ripgrep) |
| `analyze_structure` | コード構造解析 (tree-sitter) |
| `get_symbols` | シンボル一覧取得 |
| `sync_index` | ソースコードを ChromaDB にインデックス |
| `semantic_search` | 地図/森の統合ベクトル検索 |
| `analyze_impact` | 変更の影響範囲分析（v1.1） |

### セッション管理

| ツール | 用途 |
|--------|------|
| `start_session` | セッション開始 |
| `set_query_frame` | QueryFrame 設定（Quote 検証） |
| `get_session_status` | 現在のフェーズ・状態を確認 |
| `submit_understanding` | EXPLORATION 完了 |
| `validate_symbol_relevance` | Embedding 検証 |
| `submit_semantic` | SEMANTIC 完了 |
| `submit_verification` | VERIFICATION 完了 |
| `check_write_target` | Write 可否確認 |
| `add_explored_files` | 探索済みファイル追加 |
| `revert_to_exploration` | EXPLORATION に戻る |

### 改善サイクル

| ツール | 用途 |
|--------|------|
| `record_outcome` | 結果記録（自動/手動） |
| `get_outcome_stats` | 統計取得 |

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
# 対象プロジェクトを初期化（プロジェクト全体をインデックス）
./init-project.sh /path/to/your-project

# オプション: 特定ディレクトリのみをインデックス
./init-project.sh /path/to/your-project --include=src,packages

# オプション: 追加の除外パターンを指定
./init-project.sh /path/to/your-project --exclude=tests,docs,*.log
```

これにより以下が作成されます：

```
your-project/
└── .code-intel/
    ├── config.json       ← 設定
    ├── chroma/           ← ChromaDB データ（自動生成）
    ├── agreements/       ← 合意事項ディレクトリ
    └── logs/             ← DecisionLog, OutcomeLog
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

### Step 4: スキルの設定（任意）

```bash
mkdir -p /path/to/your-project/.claude/commands
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 5: Claude Code を再起動

MCP サーバーを読み込むために再起動。初回セッション開始時に自動的にインデックスが構築されます。

### Step 6: 必須コンテキストの設定（v1.1、任意）

`.code-intel/context.yml` を作成して、セッション開始時に設計ドキュメントとプロジェクトルールを LLM に提供：

```yaml
# .code-intel/context.yml

# 設計ドキュメント - セッション開始時に要約が自動提供される
essential_docs:
  source: "docs/architecture"  # 設計ドキュメントのディレクトリ
  summaries:
    - file: "overview.md"
      path: "docs/architecture/overview.md"
      summary: |
        3層アーキテクチャ（Controller/Service/Repository）。
        ビジネスロジックは Service 層に集約。
      content_hash: "abc123..."  # 自動生成、変更検知に使用
      extra_notes: |
        # 手動追記（任意 - 自動生成された要約を補完）
        - 例外: 単純な CRUD は Service 層をバイパス可

# プロジェクトルール - CLAUDE.md 等からの DO/DON'T ルール
project_rules:
  source: "CLAUDE.md"  # ルールのソースファイル
  summary: |
    DO:
    - Service 層でビジネスロジックを実装
    - 全機能にテストを書く
    - 既存の命名規則に従う

    DON'T:
    - Controller に複雑なロジックを書かない
    - コードレビューをスキップしない
    - main ブランチに直接コミットしない
  content_hash: "def456..."
  extra_notes: ""

last_synced: "2025-01-14T10:00:00"  # 自動更新
```

**ポイント:**
- `summary` は手動で書くか、LLM に生成させる
- `extra_notes` でソースドキュメントにない暗黙知を追加可能
- `content_hash` で `sync_index` 実行時に変更を検知
- セッション開始時、`essential_context` としてこれらの要約が返される

**自動検出:** `context.yml` が存在しない場合、サーバーは一般的なパターンを検出：
- 設計ドキュメント: `docs/architecture/`, `docs/design/`, `docs/`
- プロジェクトルール: `CLAUDE.md`, `.claude/CLAUDE.md`, `CONTRIBUTING.md`

---

## 利用方法

### /code スキルを使う（推奨）

```
/code AuthServiceのlogin関数でパスワードが空のときエラーが出ないバグを直して
```

スキルが自動的に：
1. 失敗チェック（前回失敗を自動検出・記録）
2. Intent 判定
3. セッション開始（自動同期、必須コンテキスト）
4. QueryFrame 抽出・検証
5. EXPLORATION（find_definitions, find_references 等）
6. シンボル検証（Embedding）
7. 必要に応じて SEMANTIC
8. VERIFICATION（仮説検証）
9. IMPACT ANALYSIS（v1.1 - 影響範囲の分析）
10. READY（実装）

### 直接ツールを呼び出す

```
# テキスト検索
mcp__code-intel__search_text でパターン "Router" を検索して

# 定義検索
mcp__code-intel__find_definitions で "SessionState" の定義を探して

# 意味検索
mcp__code-intel__semantic_search でクエリ "ログイン機能" を検索して
```

---

## 改善サイクル

### 2つのログ

| ログ | ファイル | トリガー |
|------|----------|---------|
| DecisionLog | `.code-intel/logs/decisions.jsonl` | query 実行時（自動） |
| OutcomeLog | `.code-intel/logs/outcomes.jsonl` | 失敗検出時（自動）または手動 |

### 自動失敗検出

`/code` 開始時に、今回のリクエストが「前回の失敗」を示しているか自動判定：
- 「やり直して」「動かない」「違う」等のパターンを検出
- 自動で OutcomeLog に failure を記録
- `/outcome` 手動呼び出し不要

---

## 依存関係

### システムツール

| ツール | 必須 | 用途 |
|--------|------|------|
| ripgrep (rg) | Yes | search_text, find_references |
| universal-ctags | Yes | find_definitions, get_symbols |
| Python 3.10+ | Yes | サーバー本体 |

### Python パッケージ

```
mcp>=1.0.0
chromadb>=1.0.0
tree-sitter>=0.21.0
tree-sitter-languages>=1.10.0
sentence-transformers>=2.2.0
scikit-learn>=1.0.0
PyYAML>=6.0.0
pytest>=7.0.0
```

---

## プロジェクト構造

### MCP サーバー（llm-helper/）

```
llm-helper/
├── code_intel_server.py    ← MCP サーバー本体
├── tools/                  ← ツール実装
│   ├── session.py          ← セッション管理
│   ├── query_frame.py      ← QueryFrame
│   ├── router.py           ← クエリルーティング
│   ├── chromadb_manager.py ← ChromaDB 管理
│   ├── ast_chunker.py      ← AST チャンキング
│   ├── sync_state.py       ← 同期状態管理
│   ├── outcome_log.py      ← 改善サイクルログ
│   ├── context_provider.py ← 必須コンテキスト（v1.1）
│   ├── impact_analyzer.py  ← 影響範囲分析（v1.1）
│   └── ...
├── setup.sh                ← サーバーセットアップ
├── init-project.sh         ← プロジェクト初期化
└── .claude/commands/       ← スキル定義
    └── code.md
```

### 対象プロジェクト

```
your-project/
├── .mcp.json               ← MCP 設定（手動設定）
├── .code-intel/            ← Code Intel データ（自動生成）
│   ├── config.json
│   ├── context.yml         ← 必須コンテキスト設定（v1.1）
│   ├── chroma/             ← ChromaDB データ
│   ├── agreements/         ← 成功ペア
│   ├── logs/               ← DecisionLog, OutcomeLog
│   └── sync_state.json
├── .claude/commands/       ← スキル（任意コピー）
└── src/                    ← あなたのソースコード
```

---

## ドキュメント

| ドキュメント | 内容 |
|-------------|------|
| [DESIGN_v1.0.md](docs/ja/DESIGN_v1.0.md) | 全体設計 |
| [INTERNALS_v1.0.md](docs/ja/INTERNALS_v1.0.md) | 内部動作詳細 |
| [DESIGN_v1.0.md (英語)](docs/en/DESIGN_v1.0.md) | Overall design (English) |
| [INTERNALS_v1.0.md (英語)](docs/en/INTERNALS_v1.0.md) | Internal details (English) |

---

## ライセンス

MIT

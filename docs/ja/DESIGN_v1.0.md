# Code Intelligence MCP Server v1.0 設計ドキュメント

> **v1.1 での追加機能については [v1.1 追加機能](#v11-追加機能) セクションを参照してください。**

## 概要

Code Intelligence MCP Server は、LLM（Large Language Model）がコードベースを正確に理解し、安全に実装を行うためのガードレールを提供するシステムです。

### 設計思想

1. **フェーズゲート方式**: 探索 → 検証 → 実装の順序を強制し、「調べずに実装」を防止
2. **Forest/Map アーキテクチャ**: コード全体（森）と成功パターン（地図）の2層構造
3. **改善サイクル**: 失敗を自動記録し、システム改善に活用
4. **LLM委譲 + サーバー検証**: LLMの判断をサーバーが検証するハイブリッド方式

---

## アーキテクチャ

### Forest/Map 2層構造

```
┌─────────────────────────────────────────────────────┐
│                    LLM Agent                         │
│                   (/code skill)                      │
└─────────────────────┬───────────────────────────────┘
                      │ MCP Protocol
                      ▼
┌─────────────────────────────────────────────────────┐
│             Code Intelligence Server                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │   Router    │  │   Session   │  │ QueryFrame  │ │
│  │             │  │   Manager   │  │ Decomposer  │ │
│  └─────────────┘  └─────────────┘  └─────────────┘ │
│  ┌─────────────────────────────────────────────────┐│
│  │              ChromaDB Manager                   ││
│  │  ┌─────────────────┐  ┌─────────────────────┐  ││
│  │  │  Forest (森)     │  │  Map (地図)          │  ││
│  │  │  全コードチャンク │  │  成功した合意事項    │  ││
│  │  └─────────────────┘  └─────────────────────┘  ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│                  Tool Layer                          │
│  ctags │ ripgrep │ tree-sitter │ AST Chunker        │
└─────────────────────────────────────────────────────┘
```

### Forest（森）

- **目的**: プロジェクト全体のコードをベクトル化して検索可能にする
- **内容**: AST チャンキングされたコード断片
- **同期**: SHA256 フィンガープリントによる増分同期
- **用途**: セマンティック検索、コード理解

### Map（地図）

- **目的**: 過去に成功したパターンを記憶し、再利用する
- **内容**: 成功した NL→Symbol ペア、合意事項
- **更新**: `/outcome success` 時に自動追加
- **用途**: 探索のショートカット、信頼性の高い提案

---

## フェーズゲート

LLM の実装プロセスを4つのフェーズに分割し、各フェーズの完了をサーバーが検証します。

```
EXPLORATION → SEMANTIC → VERIFICATION → READY
    │            │            │           │
    │            │            │           └─ 実装許可
    │            │            └─ 仮説検証
    │            └─ セマンティック検索
    └─ コード探索
```

### フェーズ詳細

| フェーズ | 目的 | 許可されるツール |
|---------|------|-----------------|
| EXPLORATION | コードベース理解 | query, find_definitions, find_references, search_text, analyze_structure |
| SEMANTIC | 不足情報の補完 | semantic_search (ChromaDB) |
| VERIFICATION | 仮説の検証 | 探索ツール全般 |
| READY | 実装許可 | check_write_target, add_explored_files |

### フェーズ遷移条件

```
EXPLORATION → SEMANTIC
  条件: サーバー評価が "low" または整合性エラー

EXPLORATION → READY
  条件: サーバー評価が "high" かつ整合性OK

SEMANTIC → VERIFICATION
  条件: submit_semantic 完了

VERIFICATION → READY
  条件: submit_verification 完了
```

---

## QueryFrame

自然言語のリクエストを構造化し、「何が不足しているか」を明確にします。

### スロット構造

| スロット | 説明 | 例 |
|---------|------|-----|
| target_feature | 対象機能 | 「ログイン機能」 |
| trigger_condition | 発生条件 | 「パスワードが空のとき」 |
| observed_issue | 観察された問題 | 「エラーが出ない」 |
| desired_action | 期待する動作 | 「バリデーション追加」 |

### Quote 検証

LLM が抽出したスロットの `quote` が元のクエリに存在するかサーバーが検証します。これによりハルシネーションを防止します。

```json
{
  "target_feature": {
    "value": "ログイン機能",
    "quote": "ログイン機能で"  // 元のクエリに存在する必要あり
  }
}
```

### リスクレベル

| Level | 条件 | 探索要件 |
|-------|------|----------|
| HIGH | MODIFY + issue不明 | 厳格：全スロット埋め必須 |
| MEDIUM | IMPLEMENT または部分不明 | 標準要件 |
| LOW | INVESTIGATE または全情報あり | 最小限でOK |

---

## 改善サイクル

失敗を自動的に記録し、システム改善に活用する仕組みです。

### 2つのログ

| ログ | ファイル | トリガー |
|------|----------|---------|
| DecisionLog | `.code-intel/logs/decisions.jsonl` | query ツール実行時（自動） |
| OutcomeLog | `.code-intel/logs/outcomes.jsonl` | 失敗検出時（自動）または /outcome（手動） |

### 自動失敗検出

`/code` スキル開始時に、今回のリクエストが「前回の失敗」を示しているか自動判定します。

**検出パターン:**
- やり直し要求: 「やり直して」「もう一度」
- 否定・不満: 「違う」「そうじゃない」
- 動作不良: 「動かない」「エラーになる」
- バグ報告: 「バグがある」「おかしい」

### 分析機能

```python
get_session_analysis(session_id)      # 決定+結果を結合
get_improvement_insights(limit=100)   # 失敗パターン分析
```

---

## /code スキルのフロー

```
Step 0: 失敗チェック
    ├─ 前回セッション確認
    └─ 失敗パターン検出 → 自動記録

Step 1: Intent判定
    └─ IMPLEMENT / MODIFY / INVESTIGATE / QUESTION

Step 2: セッション開始
    └─ start_session

Step 3: QueryFrame設定
    └─ set_query_frame

Step 4: EXPLORATION
    ├─ find_definitions（必須）
    ├─ find_references（必須）
    └─ submit_understanding

Step 5: シンボル検証
    └─ validate_symbol_relevance

Step 6: SEMANTIC（必要時）
    └─ semantic_search → submit_semantic

Step 7: VERIFICATION（必要時）
    └─ 仮説検証 → submit_verification

Step 8: READY
    ├─ check_write_target
    └─ 実装（Edit/Write）
```

---

## MCP ツール一覧

### セッション管理

| ツール | 説明 |
|--------|------|
| start_session | セッション開始 |
| get_session_status | 現在の状態取得 |
| set_query_frame | QueryFrame設定 |

### 探索ツール

| ツール | 説明 |
|--------|------|
| query | 汎用クエリ（Router経由） |
| find_definitions | シンボル定義検索（ctags） |
| find_references | 参照検索（ripgrep） |
| search_text | テキスト検索 |
| analyze_structure | 構造解析（tree-sitter） |
| get_symbols | シンボル一覧取得 |

### フェーズ完了

| ツール | 説明 |
|--------|------|
| submit_understanding | EXPLORATION完了 |
| submit_semantic | SEMANTIC完了 |
| submit_verification | VERIFICATION完了 |

### 検証・制御

| ツール | 説明 |
|--------|------|
| validate_symbol_relevance | シンボル関連性検証 |
| check_write_target | 書き込み対象検証 |
| add_explored_files | 探索済みファイル追加 |
| revert_to_exploration | EXPLORATIONに戻る |

### 改善サイクル

| ツール | 説明 |
|--------|------|
| record_outcome | 結果記録 |
| get_outcome_stats | 統計取得 |

### ChromaDB

| ツール | 説明 |
|--------|------|
| sync_index | インデックス同期 |
| semantic_search | セマンティック検索 |

---

## プロジェクト構造

```
llm-helper/
├── code_intel_server.py      # MCPサーバーメイン
├── tools/
│   ├── session.py            # セッション管理
│   ├── query_frame.py        # QueryFrame処理
│   ├── router.py             # クエリルーティング
│   ├── chromadb_manager.py   # ChromaDB管理
│   ├── ast_chunker.py        # ASTチャンキング
│   ├── sync_state.py         # 同期状態管理
│   ├── ctags_tool.py         # ctags wrapper
│   ├── ripgrep_tool.py       # ripgrep wrapper
│   ├── treesitter_tool.py    # tree-sitter wrapper
│   ├── embedding.py          # Embedding計算
│   ├── learned_pairs.py      # 学習ペアキャッシュ
│   ├── agreements.py         # 合意事項管理
│   └── outcome_log.py        # 改善サイクルログ
├── .claude/
│   └── commands/
│       └── code.md           # /code スキル定義
├── .code-intel/              # プロジェクト固有データ
│   ├── chroma/               # ChromaDBデータ
│   ├── agreements/           # 合意事項（.md）
│   ├── logs/                 # DecisionLog, OutcomeLog
│   ├── config.json           # 設定
│   └── sync_state.json       # 同期状態
└── docs/
    ├── DESIGN_v1.0.md        # 全体設計（本ドキュメント）
    └── INTERNALS_v1.0.md     # 内部動作詳細
```

---

## セットアップ

### Step 1: MCP サーバーセットアップ（初回のみ）

```bash
# リポジトリをクローン
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper

# サーバーをセットアップ（venv、依存関係）
./setup.sh
```

### Step 2: プロジェクト初期化（プロジェクトごと）

```bash
# 対象プロジェクトを初期化（全体をインデックス）
./init-project.sh /path/to/your-project

# オプション: 特定ディレクトリのみインデックス
./init-project.sh /path/to/your-project --include=src,packages

# オプション: 除外パターンを追加指定
./init-project.sh /path/to/your-project --exclude=tests,docs,*.log
```

### Step 3: .mcp.json の設定

`init-project.sh` が出力する設定を `.mcp.json` に追加:

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

### Step 4: スキルのセットアップ（オプション）

```bash
mkdir -p /path/to/your-project/.claude/commands
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 5: Claude Code を再起動

MCP サーバーを読み込むために再起動。インデックスは最初のセッション開始時に自動構築されます。

### Step 6: 必須コンテキストの設定（v1.1、オプション）

`.code-intel/context.yml` を作成して、セッション開始時に設計ドキュメントとプロジェクトルールを LLM に提供。

**最小テンプレート（コピーして編集）:**

```yaml
# .code-intel/context.yml

essential_docs:
  source: "docs/architecture"      # ← 設計ドキュメントのディレクトリ
  summaries:
    - file: "overview.md"
      path: "docs/architecture/overview.md"
      summary: |
        ここにドキュメントの要約を書く。
        セッション開始時に LLM に提供される。

project_rules:
  source: "CLAUDE.md"              # ← ルールファイル
  summary: |
    DO:
    - プロジェクトルールをここに書く

    DON'T:
    - 避けるべきことをここに書く
```

**全フィールドの例（オプション含む）:**

```yaml
essential_docs:
  source: "docs/architecture"
  summaries:
    - file: "overview.md"
      path: "docs/architecture/overview.md"
      summary: |
        3層アーキテクチャ（Controller/Service/Repository）。
        ビジネスロジックは Service 層に集約。
      extra_notes: |                     # ← 任意: 暗黙知を追加
        - 例外: 単純な CRUD は Service 層をバイパス可
      # content_hash: "..."              # ← 自動生成、書かない

project_rules:
  source: "CLAUDE.md"
  summary: |
    DO:
    - Service 層でビジネスロジックを実装
    - 全機能にテストを書く

    DON'T:
    - Controller に複雑なロジックを書かない
    - コードレビューをスキップしない
  extra_notes: ""                        # ← 任意
  # content_hash: "..."                  # ← 自動生成、書かない

# last_synced: "..."                     # ← 自動生成、書かない
```

| フィールド | 必須 | 説明 |
|-----------|------|------|
| `source` | Yes | ソースファイル/ディレクトリのパス |
| `summary` | Yes | LLM に提供する要約 |
| `extra_notes` | No | 追加の暗黙知 |
| `content_hash` | No | 自動生成（変更検知用） |
| `last_synced` | No | 自動生成（タイムスタンプ） |

**自動検出:** `context.yml` が存在しない場合、サーバーは一般的なパターンを検出:
- 設計ドキュメント: `docs/architecture/`, `docs/design/`, `docs/`
- プロジェクトルール: `CLAUDE.md`, `.claude/CLAUDE.md`, `CONTRIBUTING.md`

### 必要な外部ツール

- Universal Ctags
- ripgrep
- tree-sitter

---

## 設定ファイル

`.code-intel/config.json`:

```json
{
  "version": "1.0",
  "embedding_model": "multilingual-e5-small",
  "source_dirs": ["src", "lib"],
  "exclude_patterns": ["**/node_modules/**", "**/__pycache__/**"],
  "chunk_strategy": "ast",
  "chunk_max_tokens": 512,
  "sync_ttl_hours": 1,
  "sync_on_start": true
}
```

---

## v1.1 追加機能

v1.1 では以下の機能が追加されました。

### 必須コンテキストの自動提供

セッション開始時に、設計ドキュメントとプロジェクトルールを自動的にLLMに提供します。

**目的:**
- LLMがCLAUDE.md等のルールを読むのをサボる問題を解決
- 設計ドキュメントの無視を防止

**設定ファイル:**

```yaml
# .code-intel/context.yml

# 設計ドキュメント - セッション開始時に要約が自動提供される
essential_docs:
  source: "docs/設計資料/アーキテクチャ"  # 設計ドキュメントのディレクトリ
  summaries:
    - file: "全体アーキテクチャ.md"
      path: "docs/設計資料/アーキテクチャ/全体アーキテクチャ.md"
      summary: |
        3層レイヤード構成（Controller/Service/Repository）。
        ビジネスロジックは Service 層に集約。
      content_hash: "abc123..."  # 自動生成、変更検知に使用
      extra_notes: |
        # 手動追記（自動要約で漏れた暗黙知を補完）
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

**自動検出:** `context.yml` が存在しない場合、サーバーは一般的なパターンを検出:
- 設計ドキュメント: `docs/architecture/`, `docs/design/`, `docs/`
- プロジェクトルール: `CLAUDE.md`, `.claude/CLAUDE.md`, `CONTRIBUTING.md`

**start_session レスポンス:**

```json
{
  "success": true,
  "session_id": "abc123",
  "essential_context": {
    "design_docs": {
      "source": "docs/architecture",
      "summaries": [...]
    },
    "project_rules": {
      "source": "CLAUDE.md",
      "summary": "DO:\n- ...\nDON'T:\n- ..."
    }
  }
}
```

### 影響範囲分析（IMPACT ANALYSIS）

READY フェーズ移行前に、変更の影響範囲を分析し、確認を強制します。

**追加フェーズ:**

```
EXPLORATION → SEMANTIC → VERIFICATION → IMPACT ANALYSIS → READY
                                              ↑
                                         v1.1 で追加
```

**新ツール `analyze_impact`:**

```
mcp__code-intel__analyze_impact
  target_files: ["app/Models/Product.php"]
  change_description: "price フィールドの型を変更"
```

**レスポンス:**

```json
{
  "impact_analysis": {
    "mode": "standard",
    "depth": "direct_only",
    "static_references": {
      "callers": [
        {"file": "app/Services/CartService.php", "line": 45}
      ]
    },
    "naming_convention_matches": {
      "tests": ["tests/Feature/ProductTest.php"],
      "factories": ["database/factories/ProductFactory.php"]
    }
  },
  "confirmation_required": {
    "must_verify": ["app/Services/CartService.php"],
    "should_verify": ["tests/Feature/ProductTest.php"]
  }
}
```

**LLM の応答義務:**

```json
{
  "verified_files": [
    {"file": "...", "status": "will_modify | no_change_needed | not_affected", "reason": "..."}
  ]
}
```

### マークアップ緩和

純粋なマークアップファイル（.html, .css, .md）のみを対象とする場合、影響分析が緩和されます。

| 拡張子 | 緩和 |
|--------|------|
| `.html`, `.htm`, `.css`, `.scss`, `.md` | ✅ 緩和適用 |
| `.blade.php`, `.vue`, `.jsx`, `.tsx` | ❌ 緩和なし（ロジック結合） |

### 間接参照の扱い

- ツールは**直接参照のみ**を検出（1段階）
- 間接参照（2段階以上）は LLM の判断に委ねる
- 必要に応じて `find_references` で追加調査可能

**設計理由:**
- 再帰的な全探索はノイズが多い
- 直接参照を確認した時点で、LLM は追加調査の必要性を判断できる

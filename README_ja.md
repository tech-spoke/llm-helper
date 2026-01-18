# Code Intelligence MCP Server v1.2

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
| ゴミ分離（v1.2） | OverlayFS + Git ブランチで変更を隔離、clean で一括破棄 |

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

### 完全なフロー

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Flag Check → Failure Check → Intent → Session Start → QueryFrame           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  EXPLORATION → Symbol Validation → SEMANTIC* → VERIFICATION* → IMPACT       │
│       ↓              ↓                ↓             ↓            ↓          │
│  code-intel     Embedding検証     semantic     コード検証   analyze_impact  │
│   ツール         (NL→Symbol)       search       (仮説→確定)    (影響分析)    │
│                                   (仮説)                                    │
│                                                                             │
│  * SEMANTIC/VERIFICATION は confidence=low の場合のみ                        │
│  ← --quick / -g=n でこのブロック全体をスキップ                                │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  READY (実装) → POST_IMPLEMENTATION_VERIFICATION → PRE_COMMIT → Merge       │
│       ↓                    ↓                           ↓           ↓        │
│  Edit/Write           検証プロンプト実行            変更レビュー   mainへ    │
│  （探索済みファイルのみ）  (Playwright/pytest等)      ゴミ除去      マージ    │
│                                                                             │
│  ← --no-verify で VERIFICATION をスキップ                                    │
│  ← 検証失敗時は READY に戻ってループ                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### フェーズ別ツール許可

| フェーズ | 許可 | 禁止 |
|----------|------|------|
| EXPLORATION | code-intel ツール (query, find_definitions, find_references, search_text) | semantic_search |
| SEMANTIC | semantic_search | code-intel |
| VERIFICATION | code-intel ツール | semantic_search |
| IMPACT_ANALYSIS | analyze_impact, code-intel | semantic_search |
| READY | Edit, Write（探索済みファイルのみ） | - |
| POST_IMPL_VERIFY | 検証ツール (Playwright, pytest等) | - |
| PRE_COMMIT | review_changes, finalize_changes | - |

---

## ツール一覧

### コードインテリジェンス

| ツール | 用途 |
|--------|------|
| `query` | 自然言語でのインテリジェントクエリ |
| `find_definitions` | シンボル定義検索 (ctags) |
| `find_references` | シンボル参照検索 (ripgrep) |
| `search_text` | テキスト検索 (ripgrep) |
| `search_files` | ファイルパターン検索 (glob) |
| `analyze_structure` | コード構造解析 (tree-sitter) |
| `get_symbols` | シンボル一覧取得 |
| `get_function_at_line` | 指定行の関数を取得 |
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
| `confirm_symbol_relevance` | シンボル検証結果を確認 |
| `submit_semantic` | SEMANTIC 完了 |
| `submit_verification` | VERIFICATION 完了 |
| `submit_impact_analysis` | IMPACT_ANALYSIS 完了（v1.1） |
| `check_write_target` | Write 可否確認 |
| `add_explored_files` | 探索済みファイル追加 |
| `revert_to_exploration` | EXPLORATION に戻る |
| `update_context` | コンテキスト要約を更新（v1.1） |

### ゴミ検出（v1.2）

| ツール | 用途 |
|--------|------|
| `submit_for_review` | PRE_COMMIT フェーズに遷移 |
| `review_changes` | 全ファイル変更を表示 |
| `finalize_changes` | ファイルを保持/破棄してコミット |
| `merge_to_main` | タスクブランチを main にマージ |
| `cleanup_stale_overlays` | 中断セッションをクリーンアップ |

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
    ├── context.yml       ← Essential context（v1.1、自動生成）
    ├── chroma/           ← ChromaDB データ（自動生成）
    ├── agreements/       ← 合意事項ディレクトリ
    ├── logs/             ← DecisionLog, OutcomeLog
    ├── verifiers/        ← 検証プロンプト
    └── doc_research/     ← ドキュメント調査プロンプト（v1.3）
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

### Step 6: 必須コンテキスト（v1.1、自動）

設計ドキュメントとプロジェクトルールは**自動検出・自動要約**されます。

**動作の流れ:**
1. 初回 `sync_index` 時、サーバーが設計ドキュメントとプロジェクトルールを自動検出
2. 検出したソースで `.code-intel/context.yml` を作成
3. ドキュメント内容 + プロンプトを LLM に返す
4. LLM が要約を生成し、`update_context` ツールで保存
5. 以降の同期では変更を検出し、必要に応じて要約を再生成

**自動検出パス:**
- 設計ドキュメント: `docs/architecture/`, `docs/design/`, `docs/`
- プロジェクトルール: `CLAUDE.md`, `.claude/CLAUDE.md`, `CONTRIBUTING.md`

**手動カスタマイズ（任意）:**

`.code-intel/context.yml` を編集して `extra_notes`（ソースドキュメントにない暗黙知）を追加できます：

```yaml
essential_docs:
  source: "docs/architecture"
  summaries:
    - file: "overview.md"
      path: "docs/architecture/overview.md"
      summary: "..."                     # ← LLM が自動生成
      extra_notes: |                     # ← 任意: 暗黙知を追加
        - 例外: 単純な CRUD は Service 層をバイパス可

project_rules:
  source: "CLAUDE.md"
  summary: "..."                         # ← LLM が自動生成
  extra_notes: |                         # ← 任意: 暗黙知を追加
    - /old 配下のレガシーコードはこのルールを無視してよい
```

| フィールド | 説明 |
|-----------|------|
| `source` | 自動検出されたソースパス |
| `summary` | LLM が自動生成 |
| `extra_notes` | 手動追加（再生成時も保持される） |
| `content_hash` | 変更検知用（自動生成） |

---

## アップグレード（v1.0 → v1.1 → v1.2 → v1.3）

既存プロジェクトをアップグレードする手順：

### Step 1: llm-helper サーバーを更新

```bash
cd /path/to/llm-helper
git pull
./setup.sh  # 依存関係を更新
```

### Step 2: スキルを更新（プロジェクトにコピーしている場合）

```bash
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 3: 新しいディレクトリを追加（v1.3）

新しいディレクトリを作成し、テンプレートをコピー：

```bash
# 新しいディレクトリを作成
mkdir -p /path/to/your-project/.code-intel/logs
mkdir -p /path/to/your-project/.code-intel/verifiers
mkdir -p /path/to/your-project/.code-intel/doc_research

# verifier テンプレートをコピー
cp /path/to/llm-helper/.code-intel/verifiers/*.md /path/to/your-project/.code-intel/verifiers/

# doc_research プロンプトをコピー
cp /path/to/llm-helper/.code-intel/doc_research/*.md /path/to/your-project/.code-intel/doc_research/
```

### Step 4: context.yml を更新（v1.3）

`.code-intel/context.yml` に `doc_research` セクションを追加：

```yaml
# ドキュメント調査設定（v1.3）
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"
```

### Step 5: Claude Code を再起動

MCP サーバーを再読み込みするために再起動。

### 変更点

| 項目 | v1.0 | v1.1 | v1.2 | v1.3 |
|------|------|------|------|------|
| フェーズ数 | 4 | 5（IMPACT_ANALYSIS 追加） | 6（PRE_COMMIT 追加） | 6（DOCUMENT_RESEARCH ステップ追加） |
| context.yml | なし | 自動生成 | 自動生成 | doc_research 追加 |
| 設計ドキュメント要約 | なし | セッション開始時に自動提供 | 同左 | サブエージェント調査 |
| プロジェクトルール | CLAUDE.md を手動参照 | セッション開始時に自動提供 | 同左 | 2層コンテキスト |
| ゴミ分離 | なし | なし | OverlayFS + Git ブランチ | 同左 |
| マークアップ解析 | なし | 緩和のみ | 同左 | クロスリファレンス検出 |
| verifiers/ | なし | なし | なし | 検証プロンプト |
| doc_research/ | なし | なし | なし | 調査プロンプト |

### 変更不要なもの

- `.code-intel/config.json` - 互換性あり、変更不要
- `.code-intel/chroma/` - 既存のインデックスはそのまま動作
- `.mcp.json` - 変更不要

`context.yml` は次回のセッション開始時に自動作成されます。

---

## 利用方法

### /code スキルを使う（推奨）

```
/code AuthServiceのlogin関数でパスワードが空のときエラーが出ないバグを直して
```

### コマンドオプション

| Long | Short | 説明 |
|------|-------|------|
| `--no-verify` | - | 検証をスキップ |
| `--only-verify` | `-v` | 検証のみ実行（実装スキップ） |
| `--gate=LEVEL` | `-g=LEVEL` | ゲートレベル: h(igh), m(iddle), l(ow), a(uto), n(one) |
| `--quick` | `-q` | 探索フェーズをスキップ（= `-g=n`） |
| `--clean` | `-c` | stale オーバーレイのクリーンアップ |

**デフォルト動作:** gate=high + 実装 + 検証（フルモード）

#### 使用例

```bash
# フルモード（デフォルト）: gate=high + 実装 + 検証
/code add login feature

# 検証をスキップ
/code --no-verify fix this bug

# 検証のみ（既存実装のチェック）
/code -v sample/hello.html

# クイックモード（探索スキップ、実装 + 検証のみ）
/code -q change the button color to blue

# ゲートレベルを明示的に設定
/code -g=m add password validation

# stale オーバーレイのクリーンアップ
/code -c
```

#### --clean オプション（v1.2）

前回の作業で作成されたファイルを破棄してやり直す場合：

```
/code -c
```

`-c` / `--clean` を指定すると：
- 現在の OverlayFS セッションの変更を破棄
- Git ブランチ（`llm_task_*`）を削除
- クリーンな状態から新しいセッションを開始

**注意**: `fuse-overlayfs` がインストールされていない場合、OverlayFS 機能は無効になります。

#### 通常の実行フロー

スキルが自動的に：
1. フラグチェック
2. 失敗チェック（前回失敗を自動検出・記録）
3. Intent 判定
4. セッション開始（自動同期、必須コンテキスト）
5. QueryFrame 抽出・検証
6. EXPLORATION（find_definitions, find_references 等） ← `--quick` / `-g=n` でスキップ
7. シンボル検証（Embedding） ← `--quick` / `-g=n` でスキップ
8. 必要に応じて SEMANTIC ← `--quick` / `-g=n` でスキップ
9. VERIFICATION（仮説検証） ← `--quick` / `-g=n` でスキップ
10. IMPACT ANALYSIS（v1.1 - 影響範囲の分析） ← `--quick` / `-g=n` でスキップ
11. READY（実装）
12. POST_IMPLEMENTATION_VERIFICATION ← `--no-verify` でスキップ

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
| fuse-overlayfs | No | ゴミ分離機能（v1.2、Linux のみ） |

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
│   ├── overlay_manager.py  ← OverlayFS ゴミ分離（v1.2）
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
│   ├── verifiers/          ← 検証プロンプト
│   ├── doc_research/       ← ドキュメント調査プロンプト（v1.3）
│   └── sync_state.json
├── .claude/commands/       ← スキル（任意コピー）
└── src/                    ← あなたのソースコード
```

---

## ドキュメント

| ドキュメント | 内容 |
|-------------|------|
| [DESIGN_ja.md](docs/DESIGN_ja.md) | 全体設計（日本語） |
| [DESIGN.md](docs/DESIGN.md) | Overall design (English) |
| [DOCUMENTATION_RULES.md](docs/DOCUMENTATION_RULES.md) | ドキュメント管理ルール |

---

## ライセンス

MIT

# Code Intelligence MCP Server

> **Current Version: v1.6**

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
| ゴミ分離（v1.2） | Git ブランチで変更を隔離、clean で一括破棄 |
| ドキュメント調査（v1.3） | サブエージェントで設計ドキュメントを調査、タスク固有ルールを抽出 |
| マークアップクロスリファレンス（v1.3） | CSS/HTML/JS の軽量クロスリファレンス分析 |
| 介入システム（v1.4） | 検証ループにハマった時のリトライベース介入 |
| 品質レビュー（v1.5） | 実装後の品質チェック、リトライループ |
| ブランチライフサイクル（v1.6） | stale ブランチ警告、失敗時自動削除、begin_phase_gate 分離 |

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

## 処理フロー

処理は3つのレイヤーで構成されます:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. 準備フェーズ（Skill 制御）                                              │
│     Flag Check → Failure Check → Intent → Session Start                    │
│     → DOCUMENT_RESEARCH → QueryFrame                                       │
│     ← --no-doc-research でスキップ可                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  1.5. フェーズゲート開始（v1.6）                                            │
│     begin_phase_gate → [Stale ブランチ?] → [ユーザー介入] → 継続           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  2. フェーズゲート（Server 強制）                                           │
│     EXPLORATION → SEMANTIC* → VERIFICATION* → IMPACT_ANALYSIS → READY      │
│     → POST_IMPL_VERIFY → PRE_COMMIT → QUALITY_REVIEW                       │
│     ← --quick で探索スキップ、--no-verify/--no-quality で各フェーズスキップ │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  3. 完了                                                                    │
│     Finalize & Merge                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1. 準備フェーズ（Skill 制御）

Skill プロンプト（code.md）が制御。サーバーは関与しない。

| ステップ | 内容 | スキップ |
|---------|------|---------|
| Flag Check | コマンドオプション（`--quick` 等）をパース | - |
| Failure Check | 前回セッションが失敗したか自動検出、OutcomeLog に記録 | - |
| Intent | IMPLEMENT / MODIFY / INVESTIGATE / QUESTION を判定 | - |
| Session Start | セッション開始、project_rules 取得（ブランチ作成は v1.6 で分離） | - |
| **DOCUMENT_RESEARCH** | サブエージェントで設計ドキュメントを調査、mandatory_rules 抽出 | `--no-doc-research` |
| QueryFrame | ユーザー要求を構造化スロットに分解、Quote 検証 | - |

### 1.5. フェーズゲート開始（v1.6）

準備フェーズの後、`begin_phase_gate` がタスクブランチを作成しフェーズゲートを開始。

**Stale ブランチ検出:**
- タスクブランチ上にいない状態で `llm_task_*` ブランチが存在する場合、ユーザー介入が必要
- 3つの選択肢: 削除、マージ、そのまま継続

### 2. フェーズゲート（Server 強制）

MCP サーバーがフェーズ遷移を強制。LLM が勝手にスキップできない。

#### フェーズマトリックス

| オプション | 探索 | 実装 | 検証 | 介入 | ゴミ取 | 品質 | ブランチ |
|-----------|:----:|:----:|:----:|:----:|:------:|:----:|:-------:|
| (デフォルト) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `--no-verify` | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `--no-quality` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--fast` / `-f` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--quick` / `-q` | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

### フェーズ別ツール許可

| フェーズ | 許可 | 禁止 |
|----------|------|------|
| EXPLORATION | code-intel ツール (query, find_definitions, find_references, search_text) | semantic_search |
| SEMANTIC | semantic_search | code-intel |
| VERIFICATION | code-intel ツール | semantic_search |
| IMPACT_ANALYSIS | analyze_impact, code-intel | semantic_search |
| READY | Edit, Write（探索済みファイルのみ） | - |
| POST_IMPL_VERIFY | 検証ツール (Playwright, pytest 等) | - |
| PRE_COMMIT | review_changes, finalize_changes | - |
| QUALITY_REVIEW | submit_quality_review（Edit/Write 禁止） | - |

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

### ゴミ検出 & 品質レビュー（v1.2, v1.5）

| ツール | 用途 |
|--------|------|
| `submit_for_review` | PRE_COMMIT フェーズに遷移 |
| `review_changes` | 全ファイル変更を表示 |
| `finalize_changes` | ファイルを保持/破棄してコミット |
| `submit_quality_review` | 品質レビュー結果を報告（v1.5） |
| `merge_to_base` | タスクブランチを元のブランチにマージ |
| `cleanup_stale_sessions` | 中断セッションをクリーンアップ |

### ブランチライフサイクル（v1.6）

| ツール | 用途 |
|--------|------|
| `begin_phase_gate` | フェーズゲート開始、ブランチ作成（stale チェック付き） |
| `cleanup_stale_sessions` | stale ブランチを削除 |

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
    ├── config.json        ← 設定
    ├── context.yml        ← プロジェクトルール・ドキュメント調査設定（自動生成）
    ├── chroma/            ← ChromaDB データ（自動生成）
    ├── agreements/        ← 成功パターン保存
    ├── logs/              ← DecisionLog, OutcomeLog
    ├── verifiers/         ← 検証プロンプト（backend.md, html_css.md 等）
    ├── doc_research/      ← ドキュメント調査プロンプト
    ├── interventions/     ← 介入プロンプト（v1.4）
    └── review_prompts/    ← 品質レビュープロンプト（v1.5）
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

MCP サーバーを読み込むために再起動。

### Step 6: context.yml のカスタマイズ（任意）

`.code-intel/context.yml` ファイルで各種動作を制御できます。必要に応じてカスタマイズしてください：

```yaml
# プロジェクトルール（自動検出: CLAUDE.md, .claude/CLAUDE.md, CONTRIBUTING.md）
project_rules:
  source: "CLAUDE.md"

# analyze_impact 用のドキュメント検索設定
document_search:
  include_patterns:
    - "**/*.md"
    - "**/README*"
    - "**/docs/**/*"
  exclude_patterns:
    - "node_modules/**"
    - "vendor/**"
    - ".git/**"

# v1.3: ドキュメント調査の設定
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

# v1.4: 介入システムの設定
interventions:
  enabled: true
  prompts_dir: "interventions/"
  threshold: 3  # 介入発動までの失敗回数

# 検証者の設定
verifiers:
  suggest_improvements: true
```

| セクション | 説明 |
|-----------|------|
| `project_rules` | プロジェクトルールのソースファイル（自動検出） |
| `document_search` | 影響範囲分析時のドキュメント検索パターン |
| `doc_research` | ドキュメント調査の設定（v1.3） |
| `interventions` | 介入システムの設定（v1.4） |
| `verifiers` | 検証者の動作設定 |

---

## アップグレード（既存ユーザー向け）

**注意:** 新規セットアップの場合、このセクションは不要です。`init-project.sh` がすべてのディレクトリを作成します。

v1.2 以前からアップグレードする場合のみ、以下の手順を実行してください。

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

### Step 3: 不足ディレクトリを追加

v1.3 以降で追加されたディレクトリがない場合、作成してテンプレートをコピー：

```bash
cd /path/to/your-project

# 不足ディレクトリを作成（既存の場合はスキップされる）
mkdir -p .code-intel/logs
mkdir -p .code-intel/verifiers
mkdir -p .code-intel/doc_research
mkdir -p .code-intel/interventions
mkdir -p .code-intel/review_prompts

# テンプレートをコピー（既存ファイルは上書きされない）
cp -n /path/to/llm-helper/.code-intel/verifiers/*.md .code-intel/verifiers/
cp -n /path/to/llm-helper/.code-intel/doc_research/*.md .code-intel/doc_research/
cp -n /path/to/llm-helper/.code-intel/interventions/*.md .code-intel/interventions/
cp -n /path/to/llm-helper/.code-intel/review_prompts/*.md .code-intel/review_prompts/
```

### Step 4: Claude Code を再起動

MCP サーバーを再読み込みするために再起動。

### 変更不要なもの

- `.code-intel/config.json` - 互換性あり、変更不要
- `.code-intel/context.yml` - 自動更新される
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
| `--no-verify` | - | 検証をスキップ（介入もスキップ） |
| `--no-quality` | - | 品質レビューをスキップ（v1.5） |
| `--only-verify` | `-v` | 検証のみ実行（実装スキップ） |
| `--fast` | `-f` | 高速モード: 探索スキップ、ブランチあり |
| `--quick` | `-q` | 最小モード: 探索スキップ、ブランチなし |
| `--doc-research=PROMPTS` | - | 調査プロンプトを指定（v1.3） |
| `--no-doc-research` | - | ドキュメント調査をスキップ（v1.3） |
| `--no-intervention` | `-ni` | 介入システムをスキップ（v1.4） |
| `--clean` | `-c` | stale セッションのクリーンアップ |
| `--rebuild` | `-r` | 全インデックスを強制再構築 |

**デフォルト動作:** フルモード（探索 + 実装 + 検証 + ゴミ取り + 品質）

#### 使用例

```bash
# フルモード（デフォルト）: 探索 + 実装 + 検証 + ゴミ取り + 品質
/code add login feature

# 検証をスキップ（介入もスキップ）
/code --no-verify fix this bug

# 品質レビューのみスキップ
/code --no-quality fix simple typo

# 検証のみ（既存実装のチェック）
/code -v sample/hello.html

# 高速モード（探索スキップ、ブランチあり、既知の修正向け）
/code -f fix known issue in login validation

# クイックモード（最小限、ブランチなし）
/code -q change the button color to blue

# ドキュメント調査で特定プロンプト使用（v1.3）
/code --doc-research=security add authentication

# ドキュメント調査をスキップ（v1.3）
/code --no-doc-research fix typo

# stale セッションのクリーンアップ
/code -c

# 全インデックスを強制再構築
/code -r
```

#### --clean オプション（v1.2）

前回の作業で作成されたファイルを破棄してやり直す場合：

```
/code -c
```

`-c` / `--clean` を指定すると：
- Git ブランチ（`llm_task_*`）を削除
- クリーンな状態から新しいセッションを開始

#### 通常の実行フロー

スキルが自動的に：
1. フラグチェック
2. 失敗チェック（前回失敗を自動検出・記録）
3. Intent 判定
4. セッション開始（自動同期、必須コンテキスト）
5. DOCUMENT_RESEARCH（v1.3） ← `--no-doc-research` でスキップ
6. QueryFrame 抽出・検証
7. EXPLORATION（find_definitions, find_references 等） ← `--quick` でスキップ
8. シンボル検証（Embedding） ← `--quick` でスキップ
9. 必要に応じて SEMANTIC ← `--quick` でスキップ
10. VERIFICATION（仮説検証） ← `--quick` でスキップ
11. IMPACT ANALYSIS（v1.1 - 影響範囲の分析） ← `--quick` でスキップ
12. READY（実装）
13. POST_IMPLEMENTATION_VERIFICATION ← `--no-verify` でスキップ
14. INTERVENTION（v1.4） ← 検証3回連続失敗で発動
15. GARBAGE DETECTION ← `--quick` でスキップ
16. QUALITY REVIEW（v1.5） ← `--no-quality` または `--quick` でスキップ
17. Finalize & Merge

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

## CHANGELOG

バージョン履歴と詳細な変更内容：

| Version | Description | Link |
|---------|-------------|------|
| v1.6 | Branch Lifecycle（stale 警告、begin_phase_gate） | [v1.6](docs/updates/v1.6_ja.md) |
| v1.5 | Quality Review（品質レビュー） | [v1.5](docs/updates/v1.5_ja.md) |
| v1.4 | Intervention System（介入システム） | [v1.4](docs/updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](docs/updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](docs/updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](docs/updates/v1.1_ja.md) |

---

## ライセンス

MIT

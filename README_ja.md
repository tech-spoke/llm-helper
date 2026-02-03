# Code Intelligence MCP Server

> **Current Version: v1.16**

Claude や Codex などの LLM に「仕様通りの完全な実装」を強制する MCP サーバー。

## 概要

### 解決する課題

LLM によるコード実装には3つの課題がある：

| 課題 | 症状 |
|------|------|
| **探索不足** | 修正箇所だけを見て、関連コードを無視する |
| **ルール無視** | 設計ドキュメントやコーディング規約を読まない |
| **実装の手抜き** | タスクを抜かす、モックで済ます、空実装を返す |

### 本ツールの解決策

```
LLM に判断をさせない。守らせるのではなく、守らないと進めない設計。
```

| 課題 | 解決策 | 実装 |
|------|--------|------|
| 探索不足 | **探索強制** | EXPLORATION フェーズで code-intel ツール使用を強制 |
| ルール無視 | **ドキュメント強制** | DOCUMENT_RESEARCH フェーズで設計ドキュメント読み込みを強制 |
| 実装の手抜き | **検証強制** | checklist + evidence 必須、空実装検出（pass/TODO/NotImplementedError） |

### 立ち位置

```
┌─────────────────────────────────────────────────────────────┐
│  上位: スペック仕様開発ワークフロー                           │
│  「何を作るか」を定義（仕様書作成・タスク分解）               │
└─────────────────────────────────────────────────────────────┘
                         ↓ 仕様・タスクを渡す
┌─────────────────────────────────────────────────────────────┐
│  本ツール: Code Intelligence MCP Server                      │
│  「仕様通りに作らせる」を強制                                 │
│  ・探索 → ルール確認 → 実装 → 検証 の19ステップを強制実行    │
│  ・サーバー側でフェーズ遷移を制御（LLM がスキップ不可）       │
└─────────────────────────────────────────────────────────────┘
```

本ツールはスペック駆動開発ワークフローの **下位レイヤー** として機能し、「仕様は正しいのに LLM が正しく実装しない」問題を解決する。

---

## 設計思想

| 原則 | 内容 |
|------|------|
| **フェーズ強制** | 探索なしに実装フェーズに進めない。サーバーがフェーズ遷移を制御 |
| **サーバー評価** | 信頼度はサーバーが算出。LLM の自己申告を排除 |
| **Write 制限** | 探索済みファイルのみ修正可能。未探索ファイルへの書き込みはブロック |
| **安全弁** | サーバー側カウンターで無限ループを防止。検証失敗3回で介入発動 |
| **改善サイクル** | DecisionLog + OutcomeLog で失敗パターンを学習 |

詳細は [DESIGN_ja.md](docs/DESIGN_ja.md) を参照。

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

### セマンティック検索（Forest/Map）

**用途**: 「認証処理」→ `AuthService.login()` のように、自然言語からコードを探す

| 層 | 検索対象 | 効果 |
|----|---------|------|
| **Map** | 用語→シンボル対応表 | 学習済みの用語は即座にシンボル特定 |
| **Forest** | ソースコード全体 | 初見の用語でも関連コードを発見 |

**動作**: Map で高スコア（≥0.7）→ Forest スキップで高速化

---

## 処理フロー（19 Steps）

各フェーズをゲート管理し、LLM がフェーズをスキップしないようにオーケストレーターが制御する。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  セッション開始                                                             │
│  Step 1: start_session（Intent 判定 → 初期化）                             │
│  Step 2: BRANCH_INTERVENTION（stale 検出時のみ）                           │
│  Step 3: DOCUMENT_RESEARCH ← --no-doc でスキップ                          │
│  Step 4: QUERY_FRAME                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  探索フェーズ ← --fast/--quick でスキップ                                  │
│  Step 5:  EXPLORATION                                                      │
│  Step 6:  Q1（SEMANTIC 必要性評価）                                        │
│  Step 7:  SEMANTIC（Q1=true 時のみ）                                       │
│  Step 8:  Q2（VERIFICATION 必要性評価）                                    │
│  Step 9:  VERIFICATION（Q2=true 時のみ）                                   │
│  Step 10: Q3（IMPACT_ANALYSIS 必要性評価）                                 │
│  Step 11: IMPACT_ANALYSIS（Q3=true 時のみ / 調査: SESSION_COMPLETE）       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│  実装フェーズ                                                               │
│  Step 12: READY（タスク計画 + ブランチ作成）                               │
│  Step 13: READY（実装 ×N）                                                 │
│  Step 14: READY（全タスク完了確認）                                        │
│  Step 15: POST_IMPL_VERIFY ← --no-verify でスキップ / fail→Step 12       │
│  Step 16: VERIFY_INTERVENTION（failure_count ≥ 3 時のみ）                  │
│  Step 17: PRE_COMMIT                                                       │
│  Step 18: QUALITY_REVIEW ← --no-quality でスキップ                        │
│  Step 19: MERGE → SESSION_COMPLETE                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

詳細は [DESIGN_ja.md](docs/DESIGN_ja.md) を参照。

---

## Requirements

- Python 3.10+
- 対応 OS: Linux (Ubuntu/Debian, Fedora/RHEL, Arch), macOS, Windows (WSL)

### setup.sh が導入するツール

**環境構築:**
- venv — Python 仮想環境を作成
- pip — 最新版にアップグレード

**システムツール:**
- ripgrep (rg) — テキスト検索
- universal-ctags — シンボル定義検索

**Python パッケージ:**
| パッケージ | バージョン | 用途 |
|-----------|-----------|------|
| mcp | ≥1.0.0 | MCP サーバーフレームワーク |
| chromadb | ≥1.0.0 | ベクトル検索 (Forest/Map) |
| tree-sitter | ≥0.21.0, <0.22.0 | AST 解析 |
| tree-sitter-languages | ≥1.10.0 | 言語パーサー |
| sentence-transformers | ≥2.2.0 | Embedding モデル |
| scikit-learn | ≥1.0.0 | 類似度計算 |
| PyYAML | ≥6.0.0 | context.yml 解析 |
| asyncio | — | 非同期処理 |
| pytest | ≥7.0.0 | テスト |
| pytest-asyncio | ≥0.21.0 | 非同期テスト |

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

これにより以下が作成されます（◎ = 修正必須、★ = 追加/修正可、〇 = 修正可）：

```
your-project/
├── .claude/
│   ├── CLAUDE.md          〇 LLM 用プロジェクトルール（ルール追加可）
│   └── commands/          ← スキルファイル（自動生成）
└── .code-intel/
    ├── config.json           ← 設定（自動生成）
    ├── context.yml           ◎ プロジェクト設定（Step 6 参照）
    ├── phase_contract.yml    ← フェーズ契約定義（v1.13）
    ├── task_planning.md      〇 タスク分割ガイド（v1.11）
    ├── user_escalation.md    〇 ユーザーエスカレーション手順
    ├── chroma/               ← ChromaDB データ（自動管理）
    ├── sessions/             ← チェックポイント（自動管理）
    ├── agreements/           ← 成功パターン（自動管理）
    ├── logs/                 ← DecisionLog, OutcomeLog（自動管理）
    ├── verifiers/            ★ 検証プロンプト（追加/修正可）
    ├── doc_research/         ★ ドキュメント調査プロンプト（追加/修正可）
    ├── interventions/        ★ 介入プロンプト（追加/修正可）
    └── review_prompts/       ★ 品質レビュープロンプト（追加/修正可）
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

### Step 4: Claude Code を再起動

MCP サーバーを読み込むために再起動。

### Step 5: スキルの確認

スキルが利用可能か確認：
```bash
# Claude Code 内で
/code --help
```

### Codex ユーザー向け（スキル配置）

Codex は `$CODEX_HOME/skills` にスキルを配置します。テンプレートをコピーしてください：
```bash
cp -r /path/to/llm-helper/templates/skills/codex/* "$CODEX_HOME/skills/"
```

### Step 6: context.yml のカスタマイズ

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

## 利用方法

### /code スキルを使う

```
/code AuthServiceのlogin関数でパスワードが空のときエラーが出ないバグを直して
```

**デフォルト動作:** フルモード（探索 + 実装 + 検証 + ゴミ取り + 品質）

### コマンドオプション

| Long | Short | 説明 |
|------|-------|------|
| `--gate=LEVEL` | `-g=LEVEL` | ゲートレベル: f(ull), a(uto) [default: auto]（v1.10） |
| `--no-verify` | - | 検証をスキップ（介入もスキップ） |
| `--no-quality` | - | 品質レビューをスキップ（v1.5） |
| `--only-verify` | `-v` | 検証のみ実行（実装スキップ） |
| `--only-explore` | `-e` | 探索のみ実行（実装スキップ） |
| `--fast` | `-f` | 高速モード: 探索スキップ、ブランチあり |
| `--quick` | `-q` | 最小モード: 探索スキップ、ブランチなし |
| `--no-doc-research` | - | ドキュメント調査をスキップ（v1.3） |
| `--no-intervention` | `-ni` | 介入システムをスキップ（v1.4） |
| `--resume` | `-r` | チェックポイントからセッション再開 |
| `--clean` | `-c` | stale ブランチ削除 + チェックポイント全削除 |
| `--rebuild` | - | 全インデックスを強制再構築 |

**gate_level オプション（v1.10）:**
- `--gate=full` または `-g=f`: Q1/Q2/Q3 チェックを無視して全フェーズ実行
- `--gate=auto` または `-g=a`: LLM が各フェーズ前に必要性を判断（デフォルト）

#### フェーズマトリクス

| オプション | DOC調査 | 探索 | 実装 | 検証 | 介入 | ゴミ取 | 品質 | ブランチ |
|-----------|:----:|:----:|:----:|:----:|:------:|:----:|:-------:|:-------:|
| (デフォルト) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `--only-explore` / `-e` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `--only-verify` / `-v` | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `--no-verify` | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `--no-quality` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--no-intervention` / `-ni` | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| `--no-doc` | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `--fast` / `-f` | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--quick` / `-q` | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

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

# 探索のみ（実装はしない、問題調査向け）
/code -e コードベースの問題点を調査

# 高速モード（探索スキップ、ブランチあり、既知の修正向け）
/code -f fix known issue in login validation

# クイックモード（最小限、ブランチなし）
/code -q change the button color to blue

# ドキュメント調査をスキップ（v1.3）
/code --no-doc-research fix typo

# チェックポイントから再開
/code -r

# stale セッションのクリーンアップ
/code -c

# 全インデックスを強制再構築
/code --rebuild
```

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
│   ├── branch_manager.py   ← Git ブランチ分離（v1.2）
│   └── ...
├── setup.sh                ← サーバーセットアップ
├── init-project.sh         ← プロジェクト初期化
└── templates/
    ├── code-intel/         ← .code-intel テンプレート
    └── skills/
        ├── claude/         ← Claude スキルテンプレート
        └── codex/          ← Codex スキルテンプレート
```

> 対象プロジェクトの構造は「[セットアップ > Step 2](#step-2-対象プロジェクトの初期化)」を参照

---

## ツール一覧

| カテゴリ | ツール |
|---------|--------|
| セッション | `start_session`, `submit_phase`, `get_session_status` |
| 探索 | `search_text`, `find_definitions`, `find_references`, `analyze_structure`, `get_symbols`, `search_files`, `semantic_search`, `query` |
| 実装制御 | `check_write_target`, `add_explored_files`, `review_changes` |
| ブランチ | `cleanup_stale_branches` |
| インデックス | `sync_index`, `update_context`, `record_outcome`, `get_outcome_stats` |

詳細は [DESIGN_ja.md](docs/DESIGN_ja.md) を参照。

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
| v1.11 | submit_phase 統一 & タスクオーケストレーション（17個のツール→1個、コンパクト耐性、サーバー強制タスク管理） | [v1.11](docs/updates/v1.11_ja.md) |
| v1.10 | Individual Phase Checks（各フェーズ前の個別チェック、VERIFICATION/IMPACT分離、gate_level再編 - 20-60秒削減） | [v1.10](docs/updates/v1.10_ja.md) |
| v1.9 | sync_index バッチ処理、VERIFICATION+IMPACT_ANALYSIS統合（15-20秒削減） | [v1.9](docs/updates/v1.9_ja.md) |
| v1.8 | Exploration-Only Mode（Intent自動判定 + --only-explore、ブランチ作成なし） | [v1.8](docs/updates/v1.8_ja.md) |
| v1.7 | Parallel Execution（並列実行 - 27-35秒削減） | [v1.7](docs/updates/v1.7_ja.md) |
| v1.6 | Branch Lifecycle（stale 警告、begin_phase_gate） | [v1.6](docs/updates/v1.6_ja.md) |
| v1.5 | Quality Review（品質レビュー） | [v1.5](docs/updates/v1.5_ja.md) |
| v1.4 | Intervention System（介入システム） | [v1.4](docs/updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](docs/updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](docs/updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](docs/updates/v1.1_ja.md) |

---

## ライセンス

MIT

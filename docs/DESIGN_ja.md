# Code Intelligence MCP Server - 設計ドキュメント v1.11

> このドキュメントは v1.11 時点の完全なシステム仕様を記述しています。
> バージョン履歴については [CHANGELOG](#changelog) を参照してください。

---

## 目次

1. [概要](#概要)
2. [設計思想](#設計思想)
3. [アーキテクチャ](#アーキテクチャ)
4. [Complete Flow (19 Steps)](#complete-flow-19-steps)
5. [フェーズマトリクス](#フェーズマトリクス)
6. [submit_phase API](#submit_phase-api)
7. [タスクオーケストレーション](#タスクオーケストレーション)
8. [安全弁（ループ防止）](#安全弁ループ防止)
9. [コンパクト耐性設計](#コンパクト耐性設計)
10. [2層コンテキスト](#2層コンテキスト)
11. [ツールリファレンス](#ツールリファレンス)
12. [セットアップガイド](#セットアップガイド)
13. [設定](#設定)
14. [内部リファレンス](#内部リファレンス)
15. [CHANGELOG](#changelog)

---

## 概要

Code Intelligence MCP Server は、LLM がコードベースを正確に理解し、安全に実装を行うためのガードレールを提供します。

| 呼び出し元 | デフォルトの動作 |
|-----------|-----------------|
| **Cursor** | コードベース全体を理解してから修正 |
| **Claude Code** | 特定の場所のみを修正しがち |

このサーバーは、実装前に構造化された探索を強制することで、Claude Code を Cursor のように動作させます。

### v1.11 の主要変更

| 変更 | 内容 |
|------|------|
| **submit_phase 統一** | 17 個の submit_* ツール → `submit_phase` 一本 |
| **自己完結型レスポンス** | 毎回 `instruction` + `expected_payload` を返す |
| **タスクオーケストレーション** | サーバー強制のタスク管理（LLM がスキップ不可） |
| **コンパクト耐性** | 4層の耐性設計（ツール・レスポンス・リカバリ・防御） |

---

## 設計思想

```
LLM に決めさせない。従わないと進めない設計にする。
そして失敗から学ぶ仕組みを持つ。
```

### コア原則

| 原則 | 実装 |
|------|------|
| **フェーズ強制** | フェーズごとのツール制限（ショートカット不可） |
| **サーバー評価** | サーバーが信頼度を計算（LLM 自己申告ではない） |
| **Quote 検証** | LLM が抽出した引用を元のクエリと照合 |
| **Embedding 検証** | ベクトル類似度による客観的な NL→Symbol 関連性評価 |
| **書き込み制限** | 探索済みファイルのみ修正可能 |
| **並列実行ガード** | セッション中は他の /code 呼び出しをブロック（1プロジェクト1セッション） |
| **改善サイクル** | DecisionLog + OutcomeLog による失敗からの学習 |
| **2層コンテキスト** | 静的プロジェクトルール + 動的タスク固有ルール |
| **submit_phase 統一** | 全フェーズの出口を1ツールに統一（コンパクト耐性） |
| **サーバー強制タスク管理** | タスク完了をサーバーが検証（LLM のスキップ防止） |
| **安全弁** | 無限ループをサーバー側カウンターで防止 |

---

## アーキテクチャ

### システム概要

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
│  │  │  全コードチャンク │  │  成功パターン        │  ││
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

### Forest/Map 2層構造

| 名前 | 目的 | 内容 | 更新タイミング |
|------|------|------|---------------|
| **Forest** | コードベース全体を検索 | AST チャンキングされたコード断片 | 増分同期（SHA256） |
| **Map** | 成功パターンを再利用 | NL→Symbol ペア、合意事項 | `/outcome success` 時 |

**ショートカットロジック**: Map スコア ≥ 0.7 → Forest 探索をスキップ

---

## Complete Flow (19 Steps)

LLM 側前処理（サーバー呼び出しなし）:
- Flag Check: コマンドオプション解析
- Intent Classification: IMPLEMENT / MODIFY / INVESTIGATE / QUESTION

全ステップの出口は `submit_phase`（Step 1 のみ `start_session`）。

### セッション開始

| Step | Phase | LLM処理内容 | 備考 |
|------|-------|------------|------|
| 1 | — | Intent 判定（実装/調査）→ `start_session` で初期化 | |
| 2 | BRANCH_INTERVENTION | ユーザーに選択肢を提示 | stale ブランチ検出時のみ |
| 3 | DOCUMENT_RESEARCH | Sub-agent で設計文書調査 | |
| 4 | QUERY_FRAME | NL → 構造化スロット抽出 | |

### 探索フェーズ

| Step | Phase | LLM処理内容 | 備考 |
|------|-------|------------|------|
| 5 | EXPLORATION | code-intel ツールで探索 | |
| 6 | Q1 | SEMANTIC 必要性を評価 | サーバー判定で 7 をスキップ可 |
| 7 | SEMANTIC | semantic_search で追加情報収集 | Q1=false 時スキップ |
| 8 | Q2 | VERIFICATION 必要性を評価 | サーバー判定で 9 をスキップ可 |
| 9 | VERIFICATION | 仮説検証 | Q2=false 時スキップ |
| 10 | Q3 | IMPACT_ANALYSIS 必要性を評価 | サーバー判定で 11 をスキップ可 |
| 11 | IMPACT_ANALYSIS | 影響分析 | Q3=false 時スキップ / 調査: SESSION_COMPLETE |

### 実装フェーズ

| Step | Phase | LLM処理内容 | 備考 |
|------|-------|------------|------|
| 12 | READY | タスク分解・計画 | ブランチ作成 |
| 13 | READY | Edit/Write で実装 | タスク数分繰り返し (×N) |
| 14 | READY | 全タスク完了を確認 | 未完了時ブロック |
| 15 | POST_IMPL_VERIFY | verifier プロンプト実行 | fail 時 Step 12 差戻し |
| 16 | VERIFY_INTERVENTION | 介入プロンプト読み取り・実行 | タスク failure_count ≥ 3 時のみ |
| 17 | PRE_COMMIT | review_changes でレビュー | |
| 18 | QUALITY_REVIEW | 品質チェック | quality_revert_count ≥ 3 → forced_completion |
| 19 | MERGE | — | SESSION_COMPLETE |

---

## フェーズマトリクス

サーバーが各 submit_phase レスポンスで次フェーズを決定。LLM はフラグを知る必要がない。

| Step | Phase | 実装 | 調査 | --no-verify | --no-quality | --fast | --quick | --no-doc | -ni | 備考 |
|------|-------|:----:|:----:|:-----------:|:------------:|:------:|:-------:|:--------:|:---:|------|
| 1 | start_session | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| 2 | BRANCH_INTERVENTION | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | stale 検出時のみ |
| 3 | DOCUMENT_RESEARCH | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | |
| 4 | QUERY_FRAME | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | |
| 5 | EXPLORATION | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 6 | Q1 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 7 | SEMANTIC | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Q1=false 時スキップ |
| 8 | Q2 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 9 | VERIFICATION | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Q2=false 時スキップ |
| 10 | Q3 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | |
| 11 | IMPACT_ANALYSIS | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | Q3=false 時スキップ |
| 12 | READY (計画) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ブランチ作成 |
| 13 | READY (実装) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ×N |
| 14 | READY (完了) | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 未完了時ブロック |
| 15 | POST_IMPL_VERIFY | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | fail→Step 12 差戻し |
| 16 | VERIFY_INTERVENTION | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ | タスク failure_count ≥ 3 時のみ |
| 17 | PRE_COMMIT | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | |
| 18 | QUALITY_REVIEW | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | |
| 19 | MERGE | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | |

**凡例**:
- **実装**: IMPLEMENT / MODIFY intent（デフォルト）
- **調査**: INVESTIGATE / QUESTION intent（`--only-explore` と同等、探索完了で SESSION_COMPLETE）
- **-ni**: `--no-intervention`（Step 16 VERIFY_INTERVENTION をスキップ）
- フラグ列は実装 intent に対する修飾。調査 intent では Step 12 以降が存在しない。

### コマンドオプション

| Long | Short | 説明 |
|------|-------|------|
| `--gate=LEVEL` | `-g=LEVEL` | ゲートレベル: f(ull), a(uto) [default: auto] |
| `--no-verify` | — | POST_IMPL_VERIFY をスキップ（Step 15） |
| `--no-quality` | — | QUALITY_REVIEW をスキップ（Step 18） |
| `--only-verify` | `-v` | 検証のみ実行 |
| `--only-explore` | `-e` | 探索のみ実行（SESSION_COMPLETE after Step 11） |
| `--fast` | `-f` | 探索スキップ（Steps 5-11）、ブランチあり |
| `--quick` | `-q` | 探索スキップ（Steps 5-11）、ブランチなし、PRE_COMMIT/QUALITY_REVIEW/MERGE もスキップ |
| `--no-doc-research` | — | DOCUMENT_RESEARCH をスキップ（Step 3） |
| `--no-intervention` | `-ni` | VERIFY_INTERVENTION をスキップ（Step 16） |
| `--clean` | `-c` | stale ブランチ削除 + SessionState リセット |
| `--rebuild` | `-r` | 全インデックスを強制再構築 |

---

## submit_phase API

### 概要

```
start_session → submit_phase (×N) → SESSION_COMPLETE
                ↑                    ↑
                get_session_status   (recovery at any point)
```

- LLM が呼ぶフェーズ遷移ツールは `submit_phase` のみ
- サーバーが `SessionState.phase` から現在フェーズを特定し、ペイロードをバリデーション
- サーバーが次フェーズを決定してレスポンスを返す

### レスポンス形式（自己完結型）

全フェーズ共通のレスポンス構造:

```json
{
  "phase": "EXPLORATION",
  "step": 5,
  "instruction": "code-intel ツールでコードベースを探索してください",
  "expected_payload": {
    "explored_files": "list[str]",
    "findings": "list[str]"
  },
  "call": "submit_phase"
}
```

LLM はレスポンスの `instruction` に従い処理を実行し、`expected_payload` の形式で `submit_phase` を呼ぶ。

### フェーズ別ペイロード

| Step | Phase | LLM → サーバー (`data`) | サーバー判定 |
|------|-------|------------------------|------------|
| 2 | BRANCH_INTERVENTION | `{choice: "delete" \| "merge" \| "continue"}` | 処理実行 → 次フェーズ |
| 3 | DOCUMENT_RESEARCH | `{documents_reviewed: [...]}` | → 次フェーズ |
| 4 | QUERY_FRAME | `{action_type, target_symbols, scope, constraints}` | → 次フェーズ |
| 5 | EXPLORATION | `{explored_files: [...], findings: [...]}` | → 次フェーズ |
| 6 | Q1 | `{needs_more_information: bool, reason}` | true→SEMANTIC / false→Q2 |
| 7 | SEMANTIC | `{search_results: [...]}` | → 次フェーズ |
| 8 | Q2 | `{has_unverified_hypotheses: bool, reason}` | true→VERIFICATION / false→Q3 |
| 9 | VERIFICATION | `{hypotheses_verified: [...]}` | → 次フェーズ |
| 10 | Q3 | `{needs_impact_analysis: bool, reason}` | true→IMPACT / false(実装)→READY / false(調査)→SESSION_COMPLETE |
| 11 | IMPACT_ANALYSIS | `{impact_summary: {...}}` | 実装→READY / 調査→SESSION_COMPLETE |
| 12 | READY (計画) | `{tasks: [{id, description, status, ...}]}` | タスク登録（冪等） |
| 13 | READY (実装) | `{task_id, summary}` | 順序チェック、×N |
| 14 | READY (完了) | `{}` | 全タスク完了チェック → 次フェーズ |
| 15 | POST_IMPL_VERIFY | `{passed: bool, failed_tasks?, details}` | pass→17 / fail→12 |
| 16 | VERIFY_INTERVENTION | `{prompt_used, action_taken}` | failure_count リセット → 12 |
| 17 | PRE_COMMIT | `{reviewed_files: [...], commit_message}` | コミット → 次フェーズ |
| 18 | QUALITY_REVIEW | `{quality_score, issues: [...]}` | 問題なし→MERGE / 問題あり→12 |
| 19 | MERGE | `{}` | マージ → SESSION_COMPLETE |

**注**: `gate_level="full"` の場合、サーバーは Q1/Q2/Q3 の評価を無視し全フェーズ強制実行。

---

## タスクオーケストレーション

READY フェーズは内部的に 3 サブステップ（計画 → 実装 → 完了）を持つ。
全て `submit_phase` で送信し、サーバーが内部状態で判別する。

### Step 12: タスク計画

タスクモデル: `{id, description, status, failure_count?, revert_reason?}`

```python
# 初回
submit_phase(data={
  "tasks": [
    {"id": "task_1", "description": "CSSコンポーネントユーティリティ化", "status": "pending"},
    {"id": "task_2", "description": "@themeトークン化", "status": "pending"}
  ]
})

# 差戻し時（完了済み + 修正タスクの完全なリスト）
submit_phase(data={
  "tasks": [
    {"id": "task_1", "description": "...", "status": "completed"},
    {"id": "task_2", "description": "...", "status": "completed"},
    {"id": "fix_1", "description": "テスト失敗箇所の修正", "status": "pending",
     "failure_count": 1, "revert_reason": "テストX失敗"}
  ]
})
```

**冪等設計**: Step 12 は常に完全なタスクリストを受け取る。同じペイロードを再送しても同じ結果。

### Step 13: タスク完了報告 (×N)

```python
submit_phase(data={"task_id": "task_1", "summary": "変換完了"})
```

登録順に完了報告。サーバーが進捗を追跡し次タスクを返す。

### Step 14: 実装完了

```python
submit_phase(data={})
```

サーバーが全タスクの完了を検証。未完了がある場合はブロック。

### 差戻し時の動作

POST_IMPL_VERIFY / VERIFY_INTERVENTION / QUALITY_REVIEW から Step 12 に差し戻された場合:

1. サーバー: 差戻し理由 + 既存タスク一覧を instruction に含める
2. LLM: 既存タスク（completed）+ 修正タスク（pending）の完全なリストを submit_phase で送信
3. LLM: pending タスクのみ実装（Step 13 ×N）
4. → Step 15 POST_IMPL_VERIFY へ再進行

---

## 安全弁（ループ防止）

LLM はコンパクト後にループを認識できないため、全てのループ制限はサーバー側で強制する。

### サーバー管理カウンター

```python
class SessionState:
    intervention_count: int = 0      # VERIFY_INTERVENTION の実行回数
    quality_revert_count: int = 0    # QUALITY_REVIEW からの差戻し回数
    # failure_count はタスク別（Task.failure_count）
```

### ループパスと制限

| ループパス | トリガー | 制限 | 超過時の動作 |
|-----------|---------|------|-------------|
| POST_IMPL_VERIFY → Step 12 | 検証失敗 | タスク failure_count ≥ 3 | → VERIFY_INTERVENTION |
| VERIFY_INTERVENTION → Step 12 | 介入実行後 | intervention_count ≥ 2 | → user_escalation（AskUserQuestion） |
| QUALITY_REVIEW → Step 12 | 品質問題あり | quality_revert_count ≥ 3 | → forced_completion（warning 付き MERGE） |

---

## コンパクト耐性設計

### 前提

```
LLM 会話    ← コンパクト対象（会話履歴が要約される）
   ↕ MCP プロトコル
MCP サーバー  ← コンパクト対象外（SessionState をインメモリ保持）
```

### 4層の耐性

| 層 | 対策 | 効果 |
|----|------|------|
| ツール設計 | `submit_phase` 一本 | 「何を呼ぶか」を忘れない |
| レスポンス設計 | `expected_payload` 付き | 「何を返すか」を忘れない |
| リカバリ | `get_session_status` | 完全復元手段 |
| 防御 | ペイロード不整合検知 | サイレント障害防止 |

### get_session_status によるリカバリ

コンパクト後に LLM が状態を見失った場合:

```json
{
  "session_id": "...",
  "phase": "VERIFICATION",
  "step": 9,
  "completed_steps": [1, 2, 3, 4, 5, 6, 7, 8],
  "instruction": "仮説検証を実施し submit_phase を呼んでください",
  "expected_payload": {
    "hypotheses_verified": "list[{hypothesis, result, evidence}]"
  }
}
```

### ペイロード不整合検知

LLM がフェーズと無関係なペイロードを送った場合、サーバーが現在フェーズの再指示を返す。

---

## 2層コンテキスト

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: project_rules（セッション開始時）                       │
│  └── 常に必要なベースラインルール（軽量、キャッシュ済み）          │
│      ソース: CLAUDE.md / 内容: DO/DON'T リスト                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2: mandatory_rules（DOCUMENT_RESEARCH フェーズ）          │
│  └── タスク固有の詳細ルール（動的、タスクごと）                   │
│      ソース: docs/**/*.md（サブエージェント調査）                 │
└─────────────────────────────────────────────────────────────────┘
```

| 観点 | project_rules | mandatory_rules |
|------|---------------|-----------------|
| ソース | CLAUDE.md | docs/**/*.md |
| タイミング | セッション開始時 | DOCUMENT_RESEARCH (Step 3) |
| 内容 | 汎用 DO/DON'T | タスク固有の制約 |
| スキップ可能 | 不可 | 可能（`--no-doc`） |

---

## ツールリファレンス

### セッション管理

| ツール | 説明 |
|--------|------|
| `start_session` | intent, query, flags でセッション開始 |
| `submit_phase` | **全フェーズの唯一の出口** — サーバーが次フェーズを決定 |
| `get_session_status` | 現在のフェーズ + instruction + expected_payload を取得（リカバリ用） |

### コード探索

| ツール | 説明 |
|--------|------|
| `search_text` | テキストパターン検索（並列対応） |
| `find_definitions` | シンボル定義検索（ctags） |
| `find_references` | 参照検索（ripgrep） |
| `analyze_structure` | コード構造分析（tree-sitter） |
| `get_symbols` | ファイルのシンボル一覧取得 |
| `search_files` | ファイル名パターン検索 |
| `semantic_search` | Forest/Map のベクトル検索（SEMANTIC フェーズ） |
| `query` | 汎用自然言語クエリ |

### 実装制御

| ツール | 説明 |
|--------|------|
| `check_write_target` | ファイル修正可能か検証 |
| `add_explored_files` | 探索済みリストにファイル追加 |
| `revert_to_exploration` | EXPLORATION フェーズに戻る |
| `review_changes` | PRE_COMMIT で全ファイル変更を表示 |

### ブランチ管理

| ツール | 説明 |
|--------|------|
| `cleanup_stale_branches` | 全 `llm_task_*` ブランチを削除 |

### インデックス & 学習

| ツール | 説明 |
|--------|------|
| `sync_index` | ChromaDB インデックスを同期 |
| `update_context` | context.yml の要約を更新 |
| `record_outcome` | 成功/失敗を記録 |
| `get_outcome_stats` | 学習統計を取得 |

---

## セットアップガイド

### 前提条件

| ツール | 必須 | 目的 |
|--------|------|------|
| Python 3.10+ | Yes | サーバー実行 |
| Universal Ctags | Yes | シンボル定義 |
| ripgrep | Yes | コード検索 |
| tree-sitter | Yes | 構造分析 |
| git | Yes | ブランチ分離 |

### Step 1: サーバーセットアップ（初回のみ）

```bash
git clone https://github.com/tech-spoke/llm-helper.git
cd llm-helper
./setup.sh
```

### Step 2: プロジェクト初期化（プロジェクトごと）

```bash
./init-project.sh /path/to/your-project
```

作成されるもの:
```
your-project/
├── .claude/
│   ├── CLAUDE.md              (プロジェクトルールテンプレート)
│   ├── PARALLEL_GUIDE.md      (並列実行ガイド)
│   └── commands/              (スキル: code.md, exp.md 等)
└── .code-intel/
    ├── config.json            (インデックス設定)
    ├── context.yml            (コンテキスト & doc_research 設定)
    ├── task_planning.md       (タスク分割ガイド, v1.11)
    ├── agreements/            (NL→Symbol ペア)
    ├── chroma/                (ChromaDB)
    ├── logs/                  (DecisionLog, OutcomeLog)
    ├── verifiers/             (検証プロンプト)
    ├── doc_research/          (ドキュメント調査プロンプト)
    ├── review_prompts/        (ゴミ検出, 品質レビュー)
    └── interventions/         (介入プロンプト)
```

### Step 3: .mcp.json 設定

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

`init-project.sh` が `.claude/commands/` へスキルファイルを自動コピーするため、手動コピーは不要。
既存プロジェクトでスキルのみ更新する場合:

```bash
cp /path/to/llm-helper/.claude/commands/code.md /path/to/your-project/.claude/commands/
```

---

## 設定

### config.json

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

### context.yml

```yaml
project_rules:
  source: "CLAUDE.md"
  summary: |
    DO:
    - Service 層でビジネスロジック
    DON'T:
    - Controller に複雑なロジック

doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

document_search:
  include_patterns:
    - "**/*.md"
  exclude_patterns:
    - "node_modules/**"
```

---

## 内部リファレンス

### コアモジュール

| モジュール | ファイル | 責務 |
|-----------|---------|------|
| SessionState | `tools/session.py` | セッション状態、フェーズ遷移、タスク管理 |
| QueryFrame | `tools/query_frame.py` | NL → 構造化クエリ |
| ChromaDB Manager | `tools/chromadb_manager.py` | Forest/Map 管理 |
| ImpactAnalyzer | `tools/impact_analyzer.py` | 変更影響分析 |
| ContextProvider | `tools/context_provider.py` | プロジェクトルール & ドキュメント調査 |
| BranchManager | `tools/branch_manager.py` | Gitブランチ分離 |

### 主要データ構造

```python
class Phase(Enum):
    BRANCH_INTERVENTION = auto()   # Step 2
    DOCUMENT_RESEARCH = auto()     # Step 3
    QUERY_FRAME = auto()           # Step 4
    EXPLORATION = auto()           # Step 5
    Q1 = auto()                    # Step 6
    SEMANTIC = auto()              # Step 7
    Q2 = auto()                    # Step 8
    VERIFICATION = auto()          # Step 9
    Q3 = auto()                    # Step 10
    IMPACT_ANALYSIS = auto()       # Step 11
    READY = auto()                 # Steps 12-14
    POST_IMPL_VERIFY = auto()      # Step 15
    VERIFY_INTERVENTION = auto()   # Step 16
    PRE_COMMIT = auto()            # Step 17
    QUALITY_REVIEW = auto()        # Step 18
    MERGE = auto()                 # Step 19

class SessionState:
    session_id: str
    intent: str                    # IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
    phase: Phase                   # 現在のフェーズ
    query_frame: QueryFrame
    gate_level: str                # full/auto
    # v1.11: フラグ
    fast_mode: bool
    quick_mode: bool
    no_doc: bool
    no_verify: bool
    no_quality: bool
    no_intervention: bool
    # v1.11: タスク管理
    tasks: list[TaskModel]
    # v1.11: 安全弁カウンター
    intervention_count: int
    quality_revert_count: int

@dataclass
class TaskModel:
    id: str
    description: str
    status: str                    # pending/completed
    failure_count: int = 0
    revert_reason: str = ""
```

---

## CHANGELOG

| Version | Description | Link |
|---------|-------------|------|
| v1.11 | submit_phase 統一 & タスクオーケストレーション（17個のツール→1個、コンパクト耐性、サーバー強制タスク管理） | [v1.11](updates/v1.11_ja.md) |
| v1.10 | Individual Phase Checks（各フェーズ前の個別チェック、VERIFICATION/IMPACT分離、gate_level再編） | [v1.10](updates/v1.10_ja.md) |
| v1.9 | Performance Optimization（sync_index バッチ処理 - 15-20秒削減） | [v1.9](updates/v1.9_ja.md) |
| v1.8 | Exploration-Only Mode（探索のみモード - --only-explore） | [v1.8](updates/v1.8_ja.md) |
| v1.7 | Parallel Execution（並列実行 - 27-35秒削減） | [v1.7](updates/v1.7_ja.md) |
| v1.6 | Branch Lifecycle（stale 警告、begin_phase_gate） | [v1.6](updates/v1.6_ja.md) |
| v1.5 | Quality Review（品質レビュー） | [v1.5](updates/v1.5_ja.md) |
| v1.4 | Intervention System（介入システム） | [v1.4](updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1_ja.md) |

ドキュメント管理ルールは [DOCUMENTATION_RULES.md](DOCUMENTATION_RULES.md) を参照。

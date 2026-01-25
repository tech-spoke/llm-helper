# Code Intelligence MCP Server - 設計ドキュメント v1.8

> このドキュメントは v1.8 時点の完全なシステム仕様を記述しています。
> バージョン履歴については [CHANGELOG](#changelog) を参照してください。

---

## 目次

1. [概要](#概要)
2. [設計思想](#設計思想)
3. [アーキテクチャ](#アーキテクチャ)
4. [フェーズゲート](#フェーズゲート)
5. [2層コンテキスト](#2層コンテキスト)
6. [ツールリファレンス](#ツールリファレンス)
7. [/code スキルフロー](#code-スキルフロー)
8. [セットアップガイド](#セットアップガイド)
9. [設定](#設定)
10. [内部リファレンス](#内部リファレンス)
11. [CHANGELOG](#changelog)

---

## 概要

Code Intelligence MCP Server は、LLM がコードベースを正確に理解し、安全に実装を行うためのガードレールを提供します。Cursor のような IDE の動作と Claude Code のデフォルト動作のギャップを埋めます。

| 呼び出し元 | デフォルトの動作 |
|-----------|-----------------|
| **Cursor** | コードベース全体を理解してから修正 |
| **Claude Code** | 特定の場所のみを修正しがち |

このサーバーは、実装前に構造化された探索を強制することで、Claude Code を Cursor のように動作させます。

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
| **プロジェクト分離** | プロジェクトごとに独立した学習データ |
| **2層コンテキスト** | 静的プロジェクトルール + 動的タスク固有ルール |
| **ゴミ検出** | Git ブランチでコミット前に変更をレビュー |
| **介入システム** | 検証ループにハマった時のリトライベース介入（v1.4） |
| **品質レビュー** | 実装後の品質チェック、修正後は再チェック必須（v1.5） |
| **ブランチライフサイクル** | stale ブランチ警告、失敗時自動削除、begin_phase_gate 分離（v1.6） |

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

## 処理フロー

処理は4つのレイヤーで構成されます（v1.8）。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  準備フェーズ（Skill 制御 - code.md）                                       │
└─────────────────────────────────────────────────────────────────────────────┘
Step -1:  Flag Check              コマンドオプションのパース
Step 1:   Intent Classification   IMPLEMENT/MODIFY/INVESTIGATE/QUESTION に分類
Step 2:   Session Start           セッション開始、project_rules取得（ブランチ未作成）
Step 2.5: DOCUMENT_RESEARCH       ドキュメント調査（サブエージェント）← --no-doc-research でスキップ
Step 3:   QueryFrame Setup        ユーザー要求を構造化スロットに分解
Step 3.5: begin_phase_gate        フェーズゲート開始、ブランチ作成、stale警告

┌─────────────────────────────────────────────────────────────────────────────┐
│  探索フェーズ（Server強制）                                                 │
└─────────────────────────────────────────────────────────────────────────────┘
Step 4:   EXPLORATION             ソース調査（find_definitions, find_references, search_text）
Step 5:   Symbol Validation       Embeddingで関連性検証
Step 6:   SEMANTIC                セマンティック検索（信頼度低い場合のみ）
Step 7:   VERIFICATION            仮説検証（SEMANTIC実行時のみ）
Step 8:   IMPACT_ANALYSIS         影響範囲分析
          ↓
          [--only-explore ならここで終了、ユーザーに報告して完了]

┌─────────────────────────────────────────────────────────────────────────────┐
│  実装・検証フェーズ（Server強制）                                           │
└─────────────────────────────────────────────────────────────────────────────┘
Step 9:   READY                   実装（Edit/Write/Bash許可）
Step 9.5: POST_IMPL_VERIFY        実装後検証（verifier prompts実行）← --no-verify でスキップ
                                  失敗時は Step 9 に戻る（最大3回）

┌─────────────────────────────────────────────────────────────────────────────┐
│  コミット・品質フェーズ（Server強制）                                       │
└─────────────────────────────────────────────────────────────────────────────┘
Step 10:  PRE_COMMIT              コミット前レビュー
          ├─ review_changes       ゴミ検出（garbage_detection.md）
          └─ finalize_changes     keep/discard判断 + コミット実行 ★ここでコミット

Step 10.5: QUALITY_REVIEW         品質レビュー ← --no-quality でスキップ
          ├─ quality_review.md    チェックリスト確認
          └─ submit_quality_review
              ├─ 問題あり → READY に戻る（修正 → POST_IMPL_VERIFY → PRE_COMMIT → QUALITY_REVIEW）
              └─ 問題なし → 次へ

┌─────────────────────────────────────────────────────────────────────────────┐
│  完了                                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
Step 11:  merge_to_base           タスクブランチを元のブランチにマージ
                                  セッション完了、結果をユーザーに報告
```

### 1. 準備フェーズ（Skill 制御）

Skill プロンプト（code.md）が制御。サーバーは関与しない。

| ステップ | 内容 | スキップ |
|---------|------|---------|
| Step -1: Flag Check | コマンドオプション（`--quick`, `--only-explore` 等）をパース | - |
| Step 1: Intent Classification | IMPLEMENT / MODIFY / INVESTIGATE / QUESTION を判定 | - |
| Step 2: Session Start | セッション開始、project_rules 取得（ブランチ作成は v1.6 で分離） | - |
| Step 2.5: **DOCUMENT_RESEARCH** | サブエージェントで設計ドキュメントを調査、mandatory_rules 抽出 | `--no-doc-research` |
| Step 3: QueryFrame Setup | ユーザー要求を構造化スロットに分解、Quote 検証 | - |

**DOCUMENT_RESEARCH の詳細:**
- Claude Code の Task ツール（Explore エージェント）を使用
- `docs/` 配下のドキュメントを調査
- タスク固有のルール・制約・依存関係を抽出
- `mandatory_rules` として後続フェーズで参照

**--only-explore フラグ（v1.8）:**
- Step -1 で検出時に `skip_implementation=true` フラグを設定
- Session Start（Step 2）時に `skip_implementation` パラメータを渡す
- IMPACT_ANALYSIS（Step 8）完了後、実装フェーズをスキップして終了

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
| `--only-explore` / `-e` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| `--no-verify` | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `--no-quality` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--fast` / `-f` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `--quick` / `-q` | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

#### フェーズ詳細

| フェーズ | 目的 | 許可ツール | 遷移条件 |
|---------|------|-----------|----------|
| EXPLORATION | コードベース理解 | query, find_definitions, find_references, search_text | サーバー評価 "high" |
| SEMANTIC | 情報ギャップを埋める | semantic_search | submit_semantic 完了 |
| VERIFICATION | 仮説を検証 | 全探索ツール | submit_verification 完了 |
| IMPACT_ANALYSIS | 変更影響を確認 | analyze_impact | 全 must_verify ファイル確認済み |
| READY | 実装 | Edit, Write（探索済みファイルのみ） | - |
| POST_IMPL_VERIFY | 動作確認 | Playwright, pytest 等 | 検証成功（3回失敗で介入発動） |
| PRE_COMMIT | ゴミ検出 | review_changes, finalize_changes | ゴミ除去完了 |
| QUALITY_REVIEW | 品質チェック | submit_quality_review（Edit/Write 禁止） | 問題なし → 完了、問題あり → READY 差し戻し |

### 3. 完了

- `merge_to_base` でタスクブランチを元のブランチにマージ
- Git ブランチを削除

---

## フェーズ詳細

### マークアップ緩和

純粋なマークアップファイル（HTML、CSS等）のみを対象とする場合、EXPLORATION フェーズの要件が緩和されます。

**緩和の意味:**
- コード探索をほぼスキップできる（テキスト検索のみでOK）
- シンボル定義や参照の調査が不要
- 素早く実装フェーズに進める

| 拡張子 | 緩和 |
|--------|------|
| `.html`, `.htm`, `.css`, `.scss`, `.sass`, `.less`, `.md` | ✅ 適用 |
| `.blade.php`, `.vue`, `.jsx`, `.tsx`, `.svelte` | ❌ 非適用（ロジックを含む） |

**v1.3 追加 - クロスリファレンス検出:**

緩和モードでも、影響範囲分析（IMPACT_ANALYSIS）では関連ファイルを検出します：
- CSS を修正 → そのクラス/ID を使う HTML ファイルを検出
- HTML を修正 → スタイル定義のある CSS ファイルを検出
- HTML を修正 → DOM 操作する JS ファイルを検出

これらは `should_verify`（確認推奨）として報告されます。

---

## 2層コンテキスト

v1.3 では静的コンテキストと動的コンテキストを明確に分離しています。

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: project_rules（セッション開始時）                       │
│  └── 常に必要なベースラインルール（軽量、キャッシュ済み）          │
│      • ソース: CLAUDE.md                                        │
│      • 内容: DO/DON'T リスト                                    │
│      • 目的: プロジェクト全体の「常識」                           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2: mandatory_rules（DOCUMENT_RESEARCH フェーズ）          │
│  └── タスク固有の詳細ルール（動的、タスクごと）                   │
│      • ソース: docs/**/*.md（サブエージェント調査）               │
│      • 内容: ファイル:行番号 付きのタスク固有制約                 │
│      • 目的:「この実装のルール」                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 比較

| 観点 | project_rules | mandatory_rules |
|------|---------------|-----------------|
| ソース | CLAUDE.md | docs/**/*.md |
| タイミング | セッション開始時 | DOCUMENT_RESEARCH フェーズ |
| 内容 | 汎用 DO/DON'T | タスク固有の制約 |
| 生成 | プリキャッシュ要約 | サブエージェントのライブ調査 |
| スキップ可能 | 不可 | 可能（`--no-doc-research`） |

### DOCUMENT_RESEARCH フェーズ

Claude Code の Task ツール（Explore エージェント）を使用して設計ドキュメントを調査：

1. 調査プロンプトでサブエージェントを起動
2. `docs/` から関連ドキュメントを読み取り
3. ルール、依存関係、警告を抽出
4. ソース引用付きの `mandatory_rules` を返却

**設定**（`.code-intel/context.yml`）:
```yaml
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"
```

---

## ツールリファレンス

### セッション管理

| ツール | 説明 |
|--------|------|
| `start_session` | intent と query でセッション開始 |
| `get_session_status` | 現在のフェーズと状態を取得 |
| `set_query_frame` | 構造化クエリスロットを設定 |

### コード探索

| ツール | 説明 |
|--------|------|
| `query` | 汎用自然言語クエリ |
| `find_definitions` | シンボル定義検索（ctags） |
| `find_references` | 参照検索（ripgrep） |
| `search_text` | テキストパターン検索 |
| `analyze_structure` | コード構造分析（tree-sitter） |
| `get_symbols` | ファイルのシンボル一覧取得 |
| `semantic_search` | Forest/Map のベクトル検索 |

### フェーズ完了

| ツール | 説明 |
|--------|------|
| `submit_understanding` | EXPLORATION フェーズ完了 |
| `submit_semantic` | SEMANTIC フェーズ完了 |
| `submit_verification` | VERIFICATION フェーズ完了 |
| `submit_impact_analysis` | IMPACT_ANALYSIS フェーズ完了 |

### 実装制御

| ツール | 説明 |
|--------|------|
| `analyze_impact` | 実装前の変更影響を分析 |
| `check_write_target` | ファイル修正可能か検証 |
| `add_explored_files` | 探索済みリストにファイル追加 |
| `revert_to_exploration` | EXPLORATION フェーズに戻る |
| `validate_symbol_relevance` | Embedding でシンボル関連性を検証 |

### ゴミ検出 & 品質レビュー（v1.2, v1.5）

| ツール | 説明 |
|--------|------|
| `submit_for_review` | PRE_COMMIT フェーズへ遷移 |
| `review_changes` | 全ファイル変更を表示 |
| `finalize_changes` | keep/discard してコミット |
| `submit_quality_review` | 品質レビュー結果を報告（v1.5） |
| `merge_to_base` | タスクブランチを元のブランチにマージ |
| `cleanup_stale_branches` | 中断セッションをクリーンアップ |

### ブランチライフサイクル（v1.6）

| ツール | 説明 |
|--------|------|
| `begin_phase_gate` | フェーズゲート開始、ブランチ作成（stale チェック付き） |
| `cleanup_stale_branches` | ベースブランチにチェックアウトし、全 `llm_task_*` ブランチを削除 |

### インデックス & 学習

| ツール | 説明 |
|--------|------|
| `sync_index` | ChromaDB インデックスを同期 |
| `update_context` | context.yml の要約を更新 |
| `record_outcome` | 成功/失敗を記録 |
| `get_outcome_stats` | 学習統計を取得 |

---

## /code スキルフロー

### コマンドオプション

| Long | Short | 説明 |
|------|-------|------|
| `--no-verify` | - | 検証をスキップ（介入もスキップ） |
| `--no-quality` | - | 品質レビューをスキップ（v1.5） |
| `--only-verify` | `-v` | 検証のみ実行 |
| `--only-explore` | `-e` | 探索のみ実行（実装スキップ）（v1.8） |
| `--fast` | `-f` | 高速モード: 探索スキップ、ブランチあり |
| `--quick` | `-q` | 最小モード: 探索スキップ、ブランチなし |
| `--doc-research=PROMPTS` | - | 調査プロンプトを指定 |
| `--no-doc-research` | - | ドキュメント調査をスキップ |
| `--clean` | `-c` | ベースブランチにチェックアウトし、stale な `llm_task_*` ブランチを削除 |
| `--rebuild` | `-r` | 全インデックスを強制再構築 |

### 実行フロー

```
┌─────────────────────────────────────────────────────────────────┐
│ 準備フェーズ（Skill制御 - code.md）                              │
└─────────────────────────────────────────────────────────────────┘
Step -1: Flag Check
    └─ コマンドオプションをパース（--quick, --only-explore, --no-verify 等）
       --only-explore / -e 検出時 → skip_implementation=true フラグ設定

Step 1: Intent Classification
    └─ IMPLEMENT / MODIFY / INVESTIGATE / QUESTION に分類
       ★ v1.8: Intent=INVESTIGATE または QUESTION → 自動的に探索専用モード
          (skip_implementation=true, skip_branch=true)

Step 2: Session Start
    ├─ context.yml から project_rules をロード
    ├─ skip_implementation フラグを設定:
    │  - Intent=INVESTIGATE/QUESTION → skip_implementation=true（自動）
    │  - --only-explore フラグ → skip_implementation=true（明示的）
    │  - Intent=IMPLEMENT/MODIFY → skip_implementation=false（デフォルト）
    └─ ChromaDB を同期（必要時）
    ※ ブランチ作成は Step 3.5 で実施（v1.6）

Step 2.5: DOCUMENT_RESEARCH (v1.3)
    ├─ 調査プロンプトでサブエージェントを起動
    └─ docs/ から mandatory_rules を抽出
    ← --no-doc-research でスキップ

Step 3: QueryFrame Setup
    └─ Quote 検証付きで構造化スロットを抽出

Step 3.5: begin_phase_gate（v1.6）
    ├─ stale ブランチをチェック
    ├─ stale ブランチ存在時はユーザー介入（削除/マージ/継続）
    └─ ブランチ作成判定:
       - skip_implementation=true → skip_branch=true（ブランチなし）
       - skip_implementation=false → タスクブランチ作成（通常フロー）
       ★ v1.8: 探索専用モードではブランチ作成をスキップ

┌─────────────────────────────────────────────────────────────────┐
│ 探索フェーズ（Server強制）                                       │
└─────────────────────────────────────────────────────────────────┘
Step 4: EXPLORATION
    ├─ find_definitions, find_references, search_text 等を使用
    ├─ mandatory_rules を acknowledge
    └─ submit_understanding

Step 5: Symbol Validation
    └─ Embedding で NL→Symbol 関連性を検証

Step 6: SEMANTIC（信頼度が低い場合のみ）
    └─ semantic_search → submit_semantic

Step 7: VERIFICATION（SEMANTIC 実行時のみ）
    └─ 仮説をコードで検証 → submit_verification

Step 8: IMPACT_ANALYSIS
    ├─ 対象ファイルの analyze_impact
    └─ 全 must_verify ファイルを確認 → submit_impact_analysis

    ★ v1.8: skip_implementation=true 時（Intent=INVESTIGATE/QUESTION または --only-explore）:
       - submit_impact_analysis が exploration_complete=true を返却
       - 調査結果をユーザーに報告して完了（Step 9 以降をスキップ）
       - ブランチ未作成、実装フェーズなし

┌─────────────────────────────────────────────────────────────────┐
│ 実装・検証フェーズ（Server強制）                                 │
└─────────────────────────────────────────────────────────────────┘
Step 9: READY
    ├─ 各 Edit/Write 前に check_write_target
    └─ 実装
    → submit_for_review で PRE_COMMIT へ

Step 9.5: POST_IMPL_VERIFY
    ├─ ファイルタイプに基づき verifier を選択
    ├─ verifier プロンプトを実行（.code-intel/verifiers/）
    ├─ 失敗時は Step 9 に戻る
    └─ 3回連続失敗で介入発動（v1.4）
    ← --no-verify でスキップ

┌─────────────────────────────────────────────────────────────────┐
│ コミット・品質フェーズ（Server強制）                             │
└─────────────────────────────────────────────────────────────────┘
Step 10: PRE_COMMIT（ゴミ検出 + コミット準備）
    ├─ review_changes（garbage_detection.md で変更をレビュー）
    └─ finalize_changes（keep/discard 判断 + コミット準備）
       ★ v1.8: コミット準備のみ（実行は QUALITY_REVIEW 後）

Step 10.5: QUALITY_REVIEW（v1.5、v1.8で順序変更）
    ├─ quality_review.md に基づき品質チェック
    ├─ 問題発見 → submit_quality_review(issues_found=true)
    │            → Step 9 (READY) に差し戻し（準備したコミット破棄）
    └─ 問題なし → submit_quality_review(issues_found=false)
                 → ★ここでコミット実行 → Step 11 へ
    ← --no-quality でスキップ

┌─────────────────────────────────────────────────────────────────┐
│ 完了                                                            │
└─────────────────────────────────────────────────────────────────┘
Step 11: merge_to_base
    └─ タスクブランチを元のブランチにマージ
       セッション完了、結果をユーザーに報告
```

### 検証システム

実装後の検証は `.code-intel/verifiers/` に格納された verifier プロンプトを使用します：

| Verifier | ファイルタイプ | 方法 |
|----------|---------------|------|
| `backend.md` | `.py`, `.js`, `.ts`, `.php`（非UI） | pytest, npm test |
| `html_css.md` | `.html`, `.css`, `.vue`, `.jsx`, `.tsx`（UI） | Playwright |
| `generic.md` | 設定、ドキュメント、その他 | 手動確認 |

**選択ロジック:**
- 変更されたファイルが verifier 選択を決定
- 混在ファイルタイプ → 主要カテゴリまたは複数 verifier
- 検証失敗 → READY に戻る（最大3回まで）

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
└── .code-intel/
    ├── config.json
    ├── context.yml
    ├── chroma/
    ├── agreements/
    └── logs/
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

### Step 4: スキルをコピー（オプション）

```bash
cp /path/to/llm-helper/.claude/commands/*.md /path/to/your-project/.claude/commands/
```

### Step 5: Claude Code を再起動

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
# Layer 1: プロジェクトルール（常に適用）
project_rules:
  source: "CLAUDE.md"
  summary: |
    DO:
    - Service 層でビジネスロジック
    DON'T:
    - Controller に複雑なロジック
  content_hash: "abc123..."

# Layer 2: ドキュメント調査設定
doc_research:
  enabled: true
  docs_path:
    - "docs/"
  default_prompts:
    - "default.md"

# 影響分析のドキュメント検索
document_search:
  include_patterns:
    - "**/*.md"
  exclude_patterns:
    - "node_modules/**"

last_synced: "2025-01-18T10:00:00"
```

---

## 内部リファレンス

### コアモジュール

| モジュール | ファイル | 責務 |
|-----------|---------|------|
| SessionState | `tools/session.py` | セッション状態、フェーズ遷移 |
| QueryFrame | `tools/query_frame.py` | NL → 構造化クエリ |
| ChromaDB Manager | `tools/chromadb_manager.py` | Forest/Map 管理 |
| ImpactAnalyzer | `tools/impact_analyzer.py` | 変更影響分析 |
| ContextProvider | `tools/context_provider.py` | プロジェクトルール & ドキュメント調査 |
| BranchManager | `tools/branch_manager.py` | Gitブランチ分離 |

### 主要データ構造

```python
class SessionState:
    session_id: str
    intent: str           # IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
    phase: Phase          # 現在のフェーズ
    query_frame: QueryFrame
    task_branch_enabled: bool
    gate_level: str       # high/middle/low/auto/none

class Phase(Enum):
    EXPLORATION = "exploration"
    SEMANTIC = "semantic"
    VERIFICATION = "verification"
    IMPACT_ANALYSIS = "impact_analysis"
    READY = "ready"
    PRE_COMMIT = "pre_commit"

@dataclass
class DocResearchConfig:
    enabled: bool = True
    docs_path: list[str]
    default_prompts: list[str]
```

### データフロー

```
[ユーザーリクエスト]
    ↓
[/code skill] → [start_session] → [SessionState]
    ↓                                   ↓
[DOCUMENT_RESEARCH] ← [Task tool + Explore agent]
    ↓
[set_query_frame] → [Quote 検証付き QueryFrame]
    ↓
[begin_phase_gate] → [stale ブランチチェック] → [ブランチ作成]
    ↓
[EXPLORATION] → [find_definitions/references] → [submit_understanding]
    ↓
[Symbol Validation] → [Embedding 類似度チェック]
    ↓
[SEMANTIC/VERIFICATION] → (必要時)
    ↓
[IMPACT_ANALYSIS] → [analyze_impact] → [submit_impact_analysis]
    ↓
[READY] → [check_write_target] → [Edit/Write]
    ↓
[POST_IMPL_VERIFY] → [verifiers/*.md] → (3回失敗で介入)
    ↓
[PRE_COMMIT] → [review_changes] → [finalize_changes]
    ↓
[QUALITY_REVIEW] → [quality_review.md] → (修正後は再チェック)
    ↓
[merge_to_base]（元のブランチへ）
```

### 改善サイクル

```python
# /code 開始時の自動失敗検出
record_outcome(
    session_id="...",
    outcome="failure",
    phase_at_outcome="READY",
    analysis={"root_cause": "...", "user_feedback": "..."}
)

# 失敗パターン分析
get_improvement_insights(limit=100)
# 返却: tool_failure_correlation, risk_level_correlation 等
```

---

## CHANGELOG

バージョン履歴と詳細な変更内容：

| Version | Description | Link |
|---------|-------------|------|
| v1.9 | Performance Optimization（sync_index バッチ処理、VERIFICATION+IMPACT統合 - 15-20秒削減） | [v1.9](updates/v1.9_ja.md) |
| v1.8 | Exploration-Only Mode（探索のみモード - --only-explore） | [v1.8](updates/v1.8_ja.md) |
| v1.7 | Parallel Execution（並列実行 - 27-35秒削減） | [v1.7](updates/v1.7_ja.md) |
| v1.6 | Branch Lifecycle（stale 警告、begin_phase_gate） | [v1.6](updates/v1.6_ja.md) |
| v1.5 | Quality Review（品質レビュー） | [v1.5](updates/v1.5_ja.md) |
| v1.4 | Intervention System（介入システム） | [v1.4](updates/v1.4_ja.md) |
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3_ja.md) |
| v1.2 | Git Branch Isolation | [v1.2](updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1_ja.md) |

ドキュメント管理ルールは [DOCUMENTATION_RULES.md](DOCUMENTATION_RULES.md) を参照。

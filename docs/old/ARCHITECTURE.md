# Code Intelligence MCP Server v3.8 アーキテクチャ

## 概要

LLM（Claude Code）にコードベースを理解させるための MCP サーバー。
Cursor IDE が内部で行っているコード解析をオープンソースツールで再現する。

## 設計思想

```
LLM に判断をさせない。守らせるのではなく、守らないと進めない設計。
そして、失敗から学ぶ仕組みを持つ。
```

### 核心原則

| 原則 | 実装 |
|------|------|
| フェーズ強制 | ツール使用制限（EXPLORATION で devrag 禁止等） |
| サーバー評価 | confidence はサーバーが算出、LLM の自己申告を排除 |
| 構造化入力 | Quote 検証による幻覚防止 |
| Embedding 検証 | NL→Symbol の関連性をベクトル類似度で客観評価 |
| Write 制限 | 探索済みファイルのみ許可 |
| 改善サイクル | Outcome Log + agreements による学習 |
| プロジェクト分離 | 各プロジェクトごとに独立した学習データ |

---

## システム構成

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Clients (Claude Code)                 │
└─────────────────────────────────────────────────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌─────────────┐ ┌─────────────┐ ┌─────────────────┐
        │ devrag-map  │ │devrag-forest│ │   code-intel    │
        │ (地図)      │ │ (森)        │ │ (オーケストレータ)│
        └─────────────┘ └─────────────┘ └─────────────────┘
               │               │                 │
               └───────────────┼─────────────────┘
                               ▼
                 ┌─────────────────────────┐
                 │  Project/.code-intel/   │  ← プロジェクト固有
                 │  ├─ devrag-map.json     │
                 │  ├─ devrag-forest.json  │
                 │  ├─ vectors-map.db      │
                 │  ├─ vectors-forest.db   │
                 │  ├─ agreements/         │
                 │  └─ learned_pairs.json  │
                 └─────────────────────────┘
```

### プロジェクト分離アーキテクチャ

**ロジック（共有）と記憶（分離）の分離:**

```
llm-helper/                    ← MCP サーバー本体（共有）
├── code_intel_server.py       ← ロジック
├── tools/                     ← ツール実装
├── setup.sh                   ← サーバーセットアップ
└── init-project.sh            ← プロジェクト初期化

ProjectA/
├── .mcp.json                  ← MCP 設定
└── .code-intel/               ← ProjectA 固有の記憶
    ├── devrag-map.json
    ├── devrag-forest.json
    ├── vectors-map.db
    ├── vectors-forest.db
    ├── agreements/
    └── learned_pairs.json

ProjectB/
├── .mcp.json
└── .code-intel/               ← ProjectB 固有の記憶
    └── ...
```

**分離の理由:**

同じ「ログイン機能」という言葉でも、プロジェクトによって実体が異なる：
- ProjectA: 「ログイン」 ↔ `AuthService.py`
- ProjectB: 「ログイン」 ↔ `MembershipModule.ts`

記憶を共有すると、ProjectB で作業中に ProjectA の知識が提示され、誤った誘導の原因になる。

### 森と地図

| 名称 | 役割 | データの性質 | 設定ファイル |
|------|------|-------------|-------------|
| **森 (Forest)** | ソースコード全体の意味検索 | 生データ・HYPOTHESIS | `devrag-forest.json` |
| **地図 (Map)** | 過去の成功ペア・合意事項 | 確定データ・FACT | `devrag-map.json` |

**Short-circuit Logic**: 地図でスコア ≥ 0.7 → 森の探索をスキップ

---

## フェーズゲート

```
start_session
    ↓
set_query_frame (Quote 検証)
    ↓
┌─────────────────────────────────────────────────────────────┐
│  EXPLORATION                                                 │
│  許可: find_definitions, find_references, search_text, etc. │
│  禁止: devrag                                                │
└─────────────────────────────────────────────────────────────┘
    ↓
submit_understanding (mapped_symbols 自動追加)
    ↓
validate_symbol_relevance (Embedding 提案)
    ↓
confirm_symbol_relevance (confidence 確定)
    ↓
    ├─ 十分 → READY
    │
    └─ 不十分 ↓
┌─────────────────────────────────────────────────────────────┐
│  SEMANTIC                                                    │
│  許可: devrag-forest                                         │
│  結果: HYPOTHESIS として記録                                  │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│  VERIFICATION                                                │
│  許可: find_definitions, find_references, etc.              │
│  禁止: devrag                                                │
│  結果: HYPOTHESIS → FACT に昇格 or rejected                  │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│  READY                                                       │
│  許可: すべて（Edit/Write 含む）                              │
│  条件: HYPOTHESIS が残っていないこと                          │
└─────────────────────────────────────────────────────────────┘
    ↓
record_outcome (成功時: agreements 自動生成)
```

---

## 主要コンポーネント

### QueryFrame

自然文を 4+1 スロットで構造化：

| スロット | 説明 | 例 |
|----------|------|-----|
| `target_feature` | 対象機能 | 「ログイン機能」 |
| `trigger_condition` | 再現条件 | 「パスワードが空のとき」 |
| `observed_issue` | 問題 | 「エラーが出ない」 |
| `desired_action` | 期待 | 「チェックを追加」 |
| `mapped_symbols` | 探索で見つけたシンボル | `[MappedSymbol(...)]` |

### MappedSymbol

```python
@dataclass
class MappedSymbol:
    name: str                    # シンボル名
    source: SlotSource           # FACT or HYPOTHESIS
    confidence: float            # 0.0-1.0
    evidence: SlotEvidence | None  # 根拠
```

### SlotSource

```python
class SlotSource(Enum):
    FACT = "FACT"              # EXPLORATION で確定
    HYPOTHESIS = "HYPOTHESIS"  # SEMANTIC で推測（検証必須）
```

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

### セッション管理

| ツール | 用途 |
|--------|------|
| `start_session` | セッション開始、extraction_prompt を返す |
| `set_query_frame` | QueryFrame 設定（Quote 検証） |
| `get_session_status` | 現在のフェーズ・状態を確認 |
| `submit_understanding` | EXPLORATION 完了、mapped_symbols 自動追加 |
| `submit_semantic` | SEMANTIC 完了 |
| `submit_verification` | VERIFICATION 完了 |
| `check_write_target` | Write 可否確認 |
| `validate_symbol_relevance` | Embedding 提案を取得 |
| `confirm_symbol_relevance` | 検証結果確定、confidence 更新 |
| `record_outcome` | 結果記録、agreements 自動生成 |

---

## シンボル検証フロー

```
1. submit_understanding(symbols_identified=["AuthService", "UserRepo"])
   → mapped_symbols に自動追加 (confidence=0.5)

2. validate_symbol_relevance(target_feature="ログイン", symbols=[...])
   → Embedding 類似度を返却
   → embedding_suggestions: [{"symbol": "AuthService", "similarity": 0.85}]

3. confirm_symbol_relevance(
       relevant_symbols=["AuthService"],
       code_evidence="AuthService.login() がユーザー認証を処理"
   )
   → confidence を Embedding スコアに更新 (0.85)
   → code_evidence を SlotEvidence として保存

4. record_outcome(outcome="success")
   → mapped_symbols から agreements を自動生成
   → devrag-map を再インデックス
```

### Embedding 3層判定

| 類似度 | 判定 | 処理 |
|--------|------|------|
| > 0.6 | 高信頼 | FACT として承認 |
| 0.3-0.6 | 中信頼 | 承認するが risk_level を HIGH に |
| < 0.3 | 低信頼 | 拒否 + 再調査ガイダンス |

---

## Agreements（合意事項）

成功した NL→Symbol ペアを Markdown で保存：

```markdown
---
doc_type: agreement
nl_term: ログイン機能
symbol: AuthService
similarity: 0.85
session_id: session_20250112_143000
---

# ログイン機能 → AuthService

## 根拠 (Code Evidence)

AuthService.login() がユーザー認証を処理

## 関連ファイル

- `src/auth/service.py`
```

devrag-map でベクトル検索可能。次回の探索で優先的に参照。

---

## データフロー

```
ユーザークエリ
    ↓
start_session (Intent: MODIFY)
    ↓
set_query_frame
    ↓
devrag-map 検索 ─┬─ ヒット (≥0.7) → PREVIOUS_SUCCESS → READY
                 │
                 └─ ミス → EXPLORATION
                            ↓
                    find_definitions, find_references
                            ↓
                    submit_understanding
                            ↓
                    validate_symbol_relevance
                            ↓
                    confirm_symbol_relevance
                            ↓
                    不十分 → SEMANTIC (devrag-forest)
                            ↓
                    VERIFICATION
                            ↓
                    READY
                            ↓
                    Edit/Write
                            ↓
                    record_outcome (success)
                            ↓
                    agreements 生成 → devrag-map 更新
```

---

## 設定ファイル

### プロジェクト構造

```
your-project/
├── .mcp.json                      ← MCP 設定（手動）
└── .code-intel/                   ← Code Intel データ（自動生成）
    ├── devrag-forest.json         ← 森設定
    ├── devrag-map.json            ← 地図設定
    ├── vectors-forest.db          ← 森 DB
    ├── vectors-map.db             ← 地図 DB
    ├── agreements/                ← 合意事項
    │   └── *.md
    └── learned_pairs.json         ← 学習ペアキャッシュ
```

### devrag-forest.json（森）

```json
{
  "document_patterns": ["../src", "../lib"],
  "db_path": "./vectors-forest.db",
  "chunk_size": 500,
  "search_top_k": 5,
  "model": {
    "name": "multilingual-e5-small",
    "dimensions": 384
  }
}
```

### devrag-map.json（地図）

```json
{
  "document_patterns": ["./agreements"],
  "db_path": "./vectors-map.db",
  "chunk_size": 300,
  "search_top_k": 10,
  "model": {
    "name": "multilingual-e5-small",
    "dimensions": 384
  }
}
```

### .mcp.json

```json
{
  "mcpServers": {
    "devrag-map": {
      "command": "/usr/local/bin/devrag",
      "args": ["--config", "/path/to/project/.code-intel/devrag-map.json"],
      "env": {"LD_LIBRARY_PATH": "/usr/local/lib"}
    },
    "devrag-forest": {
      "command": "/usr/local/bin/devrag",
      "args": ["--config", "/path/to/project/.code-intel/devrag-forest.json"],
      "env": {"LD_LIBRARY_PATH": "/usr/local/lib"}
    },
    "code-intel": {
      "command": "/path/to/llm-helper/venv/bin/python",
      "args": ["/path/to/llm-helper/code_intel_server.py"]
    }
  }
}
```

---

## 関連ドキュメント

- [ROUTER.md](./ROUTER.md) - Router 詳細設計
- [DESIGN_v3.8.md](./DESIGN_v3.8.md) - v3.8 設計詳細

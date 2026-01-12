# Code Intelligence MCP Server v3.7 設計資料

## 背景：なぜこのMCPサーバーが必要か

### 問題

同じ Opus 4.5 モデルでも、呼び出し元によって挙動が大きく異なる：

| 呼び出し元 | 挙動 |
|-----------|------|
| **Cursor** | コードベース全体を理解した上で修正する |
| **Claude Code** | 修正箇所だけを見て修正する傾向がある |

Cursor は内部でコード解析ツール（tree-sitter、LSP 等）を使い、関連コードをプロンプトに自動付与している。
Claude Code にはその仕組みがないため、LLM は「見えている範囲」だけで判断してしまう。

### 解決策

**Claude Code にコードベースを理解させる情報をプロンプトに付与する MCP サーバーを作る。**

```
Cursor が内部でやっていること（非公開）
      ↓ オープンソースツールで再現
Code Intelligence MCP Server
```

### 使用ツール

| 機能 | ツール | 用途 |
|------|--------|------|
| Repo 全体把握 | Repomix | リポジトリを LLM 用にパック |
| 意味検索 | devrag (既存) | ベクトル検索でコード検索 |
| 文字検索 | ripgrep | 高速なテキスト検索 |
| 構造解析 | tree-sitter | AST 解析、関数/クラス抽出 |
| 参照解析 | ctags | 定義・参照の解析 |

---

## v3.7 の主要変更

### 課題（v3.6）

v3.6 では以下の問題が残っていた：

- `BASIC_SYNONYMS` 辞書によるハードコードされた関連性判定
- 文字重複率による意味的一致判定の限界（「サインイン」と「Login」が一致しない）
- `QueryClassifier` の正規表現ベースのルーティング

### 解決策（v3.7）

**Embedding + LLM委譲**: パターンマッチ依存を排除し、意味理解に基づくシステムへ

| 変更領域 | v3.6 | v3.7 |
|----------|------|------|
| クエリカテゴリ | 正規表現キーワードマッチ | Intent × スロット欠損状況 |
| 用語の関連性 | ハードコード辞書 | LLM判定 + Embedding検証 |
| 幻覚チェック | 原文一致 + 文字重複率 | Embedding類似度 |

### 新ツール: `validate_symbol_relevance`

探索で見つけたシンボルの関連性を Embedding で検証：

```python
# 3層類似度判定
if similarity > 0.6:
    return "FACT"           # 高信頼
elif similarity >= 0.3:
    return "FACT + HIGH"    # 承認するがリスク引き上げ
else:
    return "REJECTED"       # 物理的拒否 + 再調査ガイダンス
```

### 成功ペアの自動キャッシュ

LLMが判定し、Embeddingで承認されたNL→Symbolペアを `.code-intel/learned_pairs.json` に保存。
次回以降の探索で優先的に提示。

---

## v3.6 の主要変更

### 課題（v3.5）

v3.5 の `QueryClassifier` はパターンマッチングベースで、以下の限界があった：

- 「ログイン機能でパスワードが空のときエラーが出ない」のような複合的な情報を扱えない
- 自然文の複数要素（対象・条件・問題・期待）を分離できない

### 解決策（v3.6）

**QueryFrame**: 自然文を4+1スロットで構造化

| スロット | 説明 | 例 |
|----------|------|-----|
| `target_feature` | 対象機能 | 「ログイン機能」 |
| `trigger_condition` | 再現条件 | 「パスワードが空のとき」 |
| `observed_issue` | 問題 | 「エラーが出ない」 |
| `desired_action` | 期待 | 「チェックを追加」 |
| `mapped_symbols` | 探索で見つけたシンボル | `["LoginService"]` |

### 新しいアーキテクチャ

```
                    v3.5                              v3.6
              ┌─────────────┐                  ┌─────────────────┐
              │QueryClassifier│                  │  QueryFrame     │
              │ (パターン)   │        →        │ (構造化スロット) │
              └─────────────┘                  └─────────────────┘
                    │                                  │
                    ▼                                  ▼
              ┌─────────────┐                  ┌─────────────────┐
              │ ToolSelector│                  │ Investigation   │
              │ (カテゴリ)  │        →        │ Guidance        │
              └─────────────┘                  │ (欠損スロット)  │
                                               └─────────────────┘
```

---

## 設計思想

```
LLM に判断をさせない。守らせるのではなく、守らないと進めない設計。
そして、失敗から学ぶ仕組みを持つ。
```

### 核心原則

| 原則 | v3.5 実装 | v3.6 追加 |
|------|-----------|-----------|
| フェーズ強制 | ツール使用制限 | - |
| サーバー評価 | confidence算出 | risk_level による動的要件 |
| 構造化入力 | devrag_reason Enum | Quote検証による幻覚防止 |
| 整合性検証 | 量×意味チェック | NL→Symbol整合性チェック |
| Write 制限 | 探索済みファイルのみ | - |
| 改善サイクル | Outcome Log | slot_evidence による追跡 |

### LLM の責務分離（v3.6）

| LLM の役割 | サーバーの役割 |
|-----------|---------------|
| スロット抽出（extraction_prompt に従う） | Quote 検証（幻覚排除） |
| ツール呼び出し（guidance に従う） | risk_level 評価 |
| 成果物提出 | 動的要件チェック |
| - | NL→Symbol 整合性検証 |
| - | FACT/HYPOTHESIS 管理 |

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│  Claude Code + /code スキル                                     │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  LLM の責務                                                │  │
│  │  ✓ Intent 判定（自然言語理解）                             │  │
│  │  ✓ スロット抽出（extraction_prompt に従う）    ← v3.6     │  │
│  │  ✓ ツール呼び出し（guidance に従う）                      │  │
│  │  ✓ 成果物提出（判定はサーバー）                            │  │
│  │  ✗ confidence 判定（禁止）                                 │  │
│  │  ✗ Quote なしの抽出（禁止）                    ← v3.6     │  │
│  │  ✗ HYPOTHESIS の検証スキップ（禁止）           ← v3.6     │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │ MCP Protocol
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Code Intelligence MCP Server                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  サーバーの責務                                            │  │
│  │  ✓ フェーズ管理（ツール制限）                              │  │
│  │  ✓ Quote 検証（幻覚排除）                      ← v3.6     │  │
│  │  ✓ risk_level 評価                            ← v3.6     │  │
│  │  ✓ confidence 算出（成果物から機械的に判定）               │  │
│  │  ✓ NL→Symbol 整合性チェック                   ← v3.6     │  │
│  │  ✓ FACT/HYPOTHESIS 管理                       ← v3.6     │  │
│  │  ✓ slot_evidence 追跡                         ← v3.6     │  │
│  │  ✓ 整合性チェック（量 × 意味）                             │  │
│  │  ✓ devrag_reason 検証（missing 対応）                      │  │
│  │  ✓ Write 対象検証（探索済みのみ）                          │  │
│  │  ✓ 詳細ログ記録（改善分析用）                              │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## フェーズゲート（v3.7）

```
start_session(intent, query)
        │
        ▼ extraction_prompt を返す
┌─────────────────────────────────────────────────────────────────┐
│  set_query_frame                                    ← v3.6     │
│  ─────────────────────────────────────────────────────────────  │
│  LLM: スロット抽出（value + quote）                             │
│  サーバー: Quote 検証、risk_level 評価                          │
│  出力: missing_slots, investigation_guidance                    │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  EXPLORATION                                                    │
│  ─────────────────────────────────────────────────────────────  │
│  許可: query, find_definitions, find_references, search_text    │
│  禁止: devrag_search                                            │
│  完了: submit_understanding(...)                                │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  VALIDATION                                         ← v3.7     │
│  ─────────────────────────────────────────────────────────────  │
│  ツール: validate_symbol_relevance                              │
│  サーバー: Embedding類似度による3層判定                         │
│  - > 0.6: FACT として承認                                       │
│  - 0.3-0.6: 承認 + risk_level を HIGH に強制                    │
│  - < 0.3: 物理的拒否 + 再調査ガイダンス                         │
│  キャッシュ参照: learned_pairs.json から既知ペアを優先提示      │
└─────────────────────────────────────────────────────────────────┘
        │
        ├─ 全承認 + 整合性 OK + HYPOTHESIS なし ───────────┐
        │                                                   │
        ▼ 一部拒否 または HYPOTHESIS あり                   │
┌───────────────────────────────────────┐                  │
│  SEMANTIC                             │                  │
│  ─────────────────────────────────    │                  │
│  許可: devrag_search, search_text     │                  │
│  完了: submit_semantic(...)           │                  │
│                                       │                  │
│  v3.6: 発見情報は HYPOTHESIS として   │                  │
│        slot_source に記録             │                  │
└───────────────────────────────────────┘                  │
        │                                                   │
        ▼                                                   │
┌───────────────────────────────────────┐                  │
│  VERIFICATION                         │                  │
│  ─────────────────────────────────    │                  │
│  許可: query, find_*, search_text     │                  │
│  禁止: devrag_search                  │                  │
│  完了: submit_verification(...)       │                  │
│                                       │                  │
│  v3.6: HYPOTHESIS を FACT に昇格      │                  │
│        または rejected として記録     │                  │
└───────────────────────────────────────┘                  │
        │                                                   │
        └───────────────────────────────────────────────────┤
                                                            │
                                                            ▼
              ┌─────────────────────────────────────────────────────┐
              │  READY                                              │
              │  ─────────────────────────────────────────────────  │
              │  許可: すべて（Edit/Write 含む）                    │
              │                                                     │
              │  v3.6 追加検証:                                     │
              │  - HYPOTHESIS スロット/シンボルが残っていないこと   │
              │  - rejected 仮説に基づく実装は禁止                  │
              └─────────────────────────────────────────────────────┘
```

---

## v3.6 新機能詳細

### 1. QueryFrame

```python
@dataclass
class QueryFrame:
    raw_query: str  # 元のクエリ（証跡）

    # 4つのスロット
    target_feature: str | None = None
    trigger_condition: str | None = None
    observed_issue: str | None = None
    desired_action: str | None = None

    # 探索で見つけたシンボル
    mapped_symbols: list[MappedSymbol] = field(default_factory=list)

    # スロットのソース（FACT/HYPOTHESIS）
    slot_source: dict[str, SlotSource] = field(default_factory=dict)

    # スロットの証拠（slot_evidence）
    slot_evidence: dict[str, SlotEvidence] = field(default_factory=dict)
```

### 2. Quote 検証

LLM が抽出した `quote` が元のクエリに存在するかサーバーが検証：

```python
def validate_slot(slot_name: str, extracted_data: dict, raw_query: str):
    quote = extracted_data.get("quote")

    # 引用が原文に含まれていない → 幻覚
    if not quote or quote not in raw_query:
        return None, None

    return extracted_data["value"], quote
```

### 3. risk_level（動的探索要件）

| risk_level | symbols | entry_points | files | patterns | slot_evidence |
|------------|---------|--------------|-------|----------|---------------|
| HIGH       | 5       | 2            | 4     | 2        | 必須          |
| MEDIUM     | 3       | 1            | 2     | 1        | 推奨          |
| LOW        | 1       | 0            | 1     | 0        | 不要          |

### 4. FACT/HYPOTHESIS 管理

```python
class SlotSource(Enum):
    FACT = "FACT"              # EXPLORATIONで確定
    HYPOTHESIS = "HYPOTHESIS"  # SEMANTICで推測（検証必須）
```

**重要**: HYPOTHESIS のままでは READY に進めない。

### 5. NL→Symbol 整合性

```python
# 自然言語とシンボルの対応を検証
def validate_nl_symbol_mapping(nl_term: str, symbols: list[str]):
    """
    target_feature が 'ログイン機能' なら、
    symbols に 'LoginService' や 'AuthController' が含まれていること
    """
    matched = [s for s in symbols if is_related(nl_term, s)]
    return bool(matched), matched
```

---

## データ構造（v3.6 更新）

### SessionState（更新）

```python
@dataclass
class SessionState:
    session_id: str
    intent: str
    query: str
    phase: Phase

    # v3.6 追加
    query_frame: QueryFrame | None = None
    risk_level: str = "LOW"

    # 既存
    decision_log: dict | None = None
    tool_calls: list[dict] = field(default_factory=list)
```

### SlotEvidence（新規）

```python
@dataclass
class SlotEvidence:
    tool: str                 # 使用したツール
    params: dict              # ツールのパラメータ
    result_summary: str       # 結果の要約
    timestamp: str
```

### DecisionLog（更新）

```python
@dataclass
class DecisionLog:
    session_id: str
    query: str
    timestamp: str
    intent: str

    # v3.6 追加
    query_frame: dict | None = None
    missing_slots: list[str] = field(default_factory=list)
    risk_level: str = "LOW"
    slot_based_tools: list[str] = field(default_factory=list)

    # 既存
    category: str
    category_confidence: float
    selected_tools: list[str]
    # ...
```

---

## 最低成果条件（v3.6）

### 動的要件（risk_level 別）

| risk_level | symbols | entry_points | files | patterns | slot_evidence |
|------------|---------|--------------|-------|----------|---------------|
| HIGH       | 5       | 2            | 4     | 2        | target_feature, observed_issue |
| MEDIUM     | 3       | 1            | 2     | 1        | target_feature |
| LOW        | 3       | 1            | 2     | 1        | なし |

### INVESTIGATE Intent

| 項目 | 条件 |
|------|------|
| symbols_identified | 1個以上 |
| files_analyzed | 1個以上 |

---

## エラーレスポンス（v3.6 追加）

### Quote 検証エラー

```json
{
  "success": false,
  "error": "validation_failed",
  "validation_errors": [
    {"slot": "target_feature", "error": "quote not found in query"}
  ],
  "message": "Some slot validations failed. Check quotes match original query."
}
```

### NL→Symbol 不整合

```json
{
  "success": true,
  "next_phase": "SEMANTIC",
  "evaluated_confidence": "low",
  "missing_requirements": [
    "nl_symbol_mapping: 'ログイン機能' has no matching symbol in ['DatabaseHelper', 'ConfigLoader']"
  ]
}
```

### HYPOTHESIS ブロック

```json
{
  "success": true,
  "next_phase": "SEMANTIC",
  "missing_requirements": [
    "Slot 'target_feature' is still HYPOTHESIS. Must verify with code intel tools first."
  ]
}
```

---

## ツール一覧（v3.7 更新）

### セッション管理ツール

| ツール | 用途 | 変更 |
|--------|------|------|
| `start_session` | セッション開始 | extraction_prompt を返す |
| `set_query_frame` | QueryFrame 設定 | v3.6 |
| `get_session_status` | 状態確認 | QueryFrame 情報を含む |
| `submit_understanding` | EXPLORATION 完了 | NL→Symbol 整合性チェック |
| `submit_semantic` | SEMANTIC 完了 | slot_source を HYPOTHESIS に設定 |
| `submit_verification` | VERIFICATION 完了 | HYPOTHESIS を FACT に昇格 |
| `check_write_target` | Write 可否確認 | HYPOTHESIS チェック |
| `validate_symbol_relevance` | シンボル関連性検証 | **v3.7 新規** |
| `record_outcome` | 結果記録 | **v3.7** 成功ペアをキャッシュ |

---

## 設計の核心（v3.6）

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. LLM に判断をさせない
   - confidence → サーバーが成果物から算出
   - Quote 検証 → LLM の幻覚を物理的に排除        ← v3.6
   - evidence → 構造化（tool, target, result 必須）

2. 動的に適応する
   - risk_level → 情報の不確実性に応じて要件を調整  ← v3.6
   - FACT/HYPOTHESIS → 情報の確実性を追跡         ← v3.6
   - NL→Symbol → 自然言語とコードの対応を検証     ← v3.6

3. フェーズゲートで物理的に制限
   - EXPLORATION で devrag → ブロック
   - HYPOTHESIS のまま READY → ブロック            ← v3.6
   - 最低成果条件未達 → READY に進めない

4. 改善サイクルを回す
   - slot_evidence で探索過程を追跡               ← v3.6
   - missing_slots で不足情報を明示              ← v3.6
   - Outcome Log で成否を記録
   - 設計改善が"再現可能"になる

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 関連ドキュメント

- [ROUTER.md](./ROUTER.md) - Router詳細設計
- [DESIGN_v3.7.md](./DESIGN_v3.7.md) - v3.7設計（Embedding + LLM委譲）
- [DESIGN_v3.6.md](./DESIGN_v3.6.md) - v3.6設計（QueryFrame）
- [code_intel_server.py](../code_intel_server.py) - MCPサーバー実装
- [tools/embedding.py](../tools/embedding.py) - Embedding検証（v3.7）
- [tools/learned_pairs.py](../tools/learned_pairs.py) - 成功ペアキャッシュ（v3.7）
- [tools/query_frame.py](../tools/query_frame.py) - QueryFrame実装
- [tools/session.py](../tools/session.py) - セッション管理
- [tools/outcome_log.py](../tools/outcome_log.py) - 結果ログ

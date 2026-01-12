# Router 設計ドキュメント v3.8

## 概要

クエリを解析し、フェーズゲートに従って探索→検証→実装を制御するコンポーネント。
「LLMに判断をさせない」原則に基づき、サーバー側で検証・制御を行う。

---

## 役割

| 役割 | 説明 |
|------|------|
| クエリ構造化 | 自然文を QueryFrame に分解 |
| Quote 検証 | LLM 抽出結果の幻覚防止 |
| フェーズ制御 | EXPLORATION → SEMANTIC → VERIFICATION → READY |
| ツール制限 | フェーズごとに使用可能ツールを物理的に制限 |
| リスク評価 | 欠損スロットに基づく探索要件の決定 |

---

## フェーズゲート

```
start_session (Intent: MODIFY/IMPLEMENT/INVESTIGATE)
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

## QueryFrame

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

## Quote 検証（幻覚防止）

LLM が抽出した `quote` が元のクエリに存在するかサーバーが検証：

```python
def validate_slot(slot_name: str, extracted_data: dict, raw_query: str):
    value = extracted_data.get("value")
    quote = extracted_data.get("quote")

    # 引用がない、または原文に含まれていない → 幻覚
    if not quote or quote not in raw_query:
        return None, None

    return value, quote
```

**目的**: LLM が勝手に情報を補完・捏造することを防ぐ

---

## リスクレベル

QueryFrame と Intent に基づいてリスクを評価：

| Level | 条件 | 探索要件 |
|-------|------|----------|
| HIGH | MODIFY + issue 不明、action 有り issue 無し | symbols: 5, entry_points: 2, files: 4 |
| MEDIUM | IMPLEMENT、または部分不明 | symbols: 3, entry_points: 1, files: 2 |
| LOW | INVESTIGATE、または全情報あり | symbols: 1, entry_points: 0, files: 1 |

```python
def assess_risk_level(frame: QueryFrame, intent: str) -> str:
    # desired_action があるのに observed_issue がない → 高リスク
    if frame.desired_action and not frame.observed_issue:
        return "HIGH"

    # MODIFY で target_feature が不明 → 高リスク
    if intent == "MODIFY" and not frame.target_feature:
        return "HIGH"

    return "LOW"
```

---

## Investigation Guidance

欠損スロットに基づいてツールを推奨：

| 欠損スロット | 推奨ツール |
|--------------|------------|
| `target_feature` | query, get_symbols, analyze_structure |
| `observed_issue` | search_text, query |
| `trigger_condition` | search_text, find_definitions |
| `desired_action` | find_references, analyze_structure |

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
```

### Embedding 3層判定

| 類似度 | 判定 | 処理 |
|--------|------|------|
| > 0.6 | 高信頼 | FACT として承認 |
| 0.3-0.6 | 中信頼 | 承認するが risk_level を HIGH に |
| < 0.3 | 低信頼 | 拒否 + 再調査ガイダンス |

---

## IntentType

| インテント | 条件 | 例 |
|------------|------|-----|
| `IMPLEMENT` | 作るものが明示 | 「ログイン機能を実装して」 |
| `MODIFY` | 既存コードの変更 | 「バグを直して」 |
| `INVESTIGATE` | 調査・理解のみ | 「どこで定義？」 |

---

## 設計原則

### 1. LLM に判断をさせない

| 処理 | 担当 |
|------|------|
| スロット抽出 | LLM |
| Quote 検証 | サーバー |
| confidence 算出 | サーバー（Embedding） |
| フェーズ遷移 | サーバー |

### 2. FACT / HYPOTHESIS 区別

- **FACT**: EXPLORATION で確定した情報
- **HYPOTHESIS**: SEMANTIC で推測した情報（検証必須）

HYPOTHESIS のまま READY には進めない：

```python
def validate_for_ready(frame: QueryFrame) -> list[str]:
    errors = []
    for symbol in frame.mapped_symbols:
        if symbol.source == SlotSource.HYPOTHESIS:
            errors.append(f"Symbol '{symbol.name}' is still HYPOTHESIS")
    return errors
```

### 3. 物理的制限

フェーズごとにツール使用を物理的にブロック：

| フェーズ | devrag | code-intel | Edit/Write |
|----------|--------|------------|------------|
| EXPLORATION | 禁止 | 許可 | 禁止 |
| SEMANTIC | 許可 | 禁止 | 禁止 |
| VERIFICATION | 禁止 | 許可 | 禁止 |
| READY | 許可 | 許可 | 許可 |

---

## 使用例

```python
# 1. セッション開始
result = await call_tool("start_session", {
    "intent": "MODIFY",
    "query": "ログイン機能でパスワードが空のときエラーが出ない"
})
# → extraction_prompt が返る

# 2. QueryFrame 設定（LLM が抽出した結果を渡す）
result = await call_tool("set_query_frame", {
    "target_feature": {"value": "ログイン機能", "quote": "ログイン機能"},
    "trigger_condition": {"value": "パスワードが空", "quote": "パスワードが空"},
    "observed_issue": {"value": "エラーが出ない", "quote": "エラーが出ない"},
})
# → risk_level: "MEDIUM", missing_slots: ["desired_action"]

# 3. 探索（EXPLORATION）
# → find_definitions, find_references を使用

# 4. 探索結果提出
result = await call_tool("submit_understanding", {
    "symbols_identified": ["LoginService", "AuthController"],
    "entry_points": ["LoginService.authenticate()"],
    "files_analyzed": ["auth/login_service.py"],
})
# → mapped_symbols に自動追加

# 5. シンボル検証
result = await call_tool("validate_symbol_relevance", {
    "target_feature": "ログイン機能",
    "symbols": ["LoginService", "AuthController"],
})
# → embedding_suggestions が返る

# 6. 検証確定
result = await call_tool("confirm_symbol_relevance", {
    "relevant_symbols": ["LoginService"],
    "code_evidence": "LoginService.authenticate() がパスワード検証を行う",
})
# → confidence 更新、READY へ遷移

# 7. 実装後、結果記録
result = await call_tool("record_outcome", {
    "outcome": "success",
    "symbols_used": ["LoginService"],
    "files_modified": ["auth/login_service.py"],
})
# → agreements 自動生成
```

---

## 関連ドキュメント

- [ARCHITECTURE.md](./ARCHITECTURE.md) - システム全体設計
- [DESIGN_v3.8.md](./DESIGN_v3.8.md) - v3.8 設計詳細

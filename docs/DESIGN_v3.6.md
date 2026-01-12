# Code Intelligence MCP Server v3.6 設計（確定版）

## 変更の動機

### 現状の問題

自然文の修正依頼に対応できない：

```
ログイン機能でXXXを入力したときに、XXXの問題があるので、XXXするように修正する
```

この文は**複合的な情報**を含む：

| 要素 | 内容 |
|------|------|
| 対象機能 | ログイン機能 |
| 再現条件 | XXXを入力したとき |
| 観測された問題 | XXXの問題がある |
| 期待する修正 | XXXするように修正 |

現在の `QueryClassifier` は「1クエリ = 1カテゴリ」を前提としており、このような複合文を適切に処理できない。

### 設計方針

```
分類（Classification）ではなく、分解（Decomposition）

LLMは情報収集・構造化の道具として積極的に使う
サーバーは判断・ゲートを担う
```

---

## 新コンポーネント：QueryFrame

自然文を構造化したデータ：

```python
@dataclass
class QueryFrame:
    """自然文から抽出された構造化情報"""

    raw_query: str                    # 元のクエリ（証跡）

    # 抽出されたスロット（すべてOptional）
    target_feature: str | None        # 対象機能・モジュール
    trigger_condition: str | None     # 再現条件・トリガー
    observed_issue: str | None        # 観測された問題
    desired_action: str | None        # 期待する修正・動作

    # メタ情報
    extraction_confidence: float      # LLM抽出の信頼度（参考値）
    missing_slots: list[str]          # 埋まらなかったスロット
```

### スロット定義

| スロット | 説明 | 例 |
|----------|------|-----|
| `target_feature` | 対象となる機能・クラス・モジュール | 「ログイン機能」「AuthService」「認証API」 |
| `trigger_condition` | 問題が発生する条件・手順 | 「XXXを入力したとき」「API呼び出し時」 |
| `observed_issue` | 観測された問題・不具合 | 「エラーが出る」「動かない」「遅い」 |
| `desired_action` | 期待する修正・変更 | 「XXXするように修正」「追加して」 |

---

## 新コンポーネント：QueryDecomposer

LLMを使って自然文を `QueryFrame` に分解する：

```python
class QueryDecomposer:
    """LLMを使った自然文の構造化"""

    def decompose(self, query: str) -> QueryFrame:
        """
        1. LLMにスロット抽出を依頼
        2. サーバーが結果を検証
        3. QueryFrameを返す
        """

        # Step 1: LLM抽出
        raw_frame = self._llm_extract(query)

        # Step 2: サーバー検証（幻覚チェック）
        validated_frame = self._validate(raw_frame, query)

        return validated_frame

    def _llm_extract(self, query: str) -> dict:
        """LLMにスロット抽出を依頼"""
        prompt = """
以下のクエリから情報を抽出してください。

ルール：
- 明示されている情報のみ抽出
- 推測・補完は禁止
- 不明な場合は null

出力形式（JSON）：
{
  "target_feature": "対象機能（明示されている場合のみ）",
  "trigger_condition": "再現条件（明示されている場合のみ）",
  "observed_issue": "問題（明示されている場合のみ）",
  "desired_action": "期待する修正（明示されている場合のみ）"
}

クエリ: {query}
"""
        return self._call_llm(prompt)

    def _validate(self, raw: dict, original_query: str) -> QueryFrame:
        """
        サーバーによる検証：
        - 抽出された値がoriginal_queryに根拠を持つか
        - LLMが推測・幻覚していないか
        """
        validated = {}
        missing = []

        for slot, value in raw.items():
            if value is None:
                missing.append(slot)
            elif self._has_evidence(value, original_query):
                validated[slot] = value
            else:
                # 根拠なし → 幻覚として却下
                validated[slot] = None
                missing.append(slot)

        return QueryFrame(
            raw_query=original_query,
            **validated,
            missing_slots=missing,
        )

    def _has_evidence(self, value: str, query: str) -> bool:
        """valueがqueryに根拠を持つか（単純な包含チェック or 類似度）"""
        # 実装例：キーワードの一部がqueryに含まれるか
        ...
```

---

## Router の責務再定義

### Before（v3.5）

```
Router の責務：
├── クエリを理解する
├── 正しいカテゴリを選ぶ
├── 正しいツールを選ぶ
└── 判断の中核

問題：知能過多・責任過多
```

### After（v3.6）

```
Router の責務：
├── QueryFrameを受け取る
├── 不足スロットを特定する
├── 初期探索ツールを決める（最適化ヒント程度）
└── 危険度を判定する

役割：交通整理係（判断機関ではない）
```

### 新しいRouter設計

```python
@dataclass
class RoutingDecision:
    """Routerの出力"""
    initial_phase: str                # 常に "EXPLORATION"
    initial_tools: list[str]          # 最初に使うツール群
    priority_slots: list[str]         # 優先的に埋めるべきスロット
    risk_level: str                   # "HIGH" | "MEDIUM" | "LOW"


class Router:
    """交通整理係：判断ではなく整理"""

    def __init__(self):
        self.classifier = QueryClassifier()  # 最適化ヒント用に残す

    def route(self, frame: QueryFrame, intent: IntentType) -> RoutingDecision:
        """
        QueryFrameに基づいてルーティング決定

        重要：ここでは「正解ルート」を決めない
        初期探索の方向性を示すだけ
        """

        # 1. 不足スロットから優先度を決定
        priority_slots = self._prioritize_missing(frame.missing_slots, intent)

        # 2. 初期ツール選択（カテゴリはヒント程度）
        category, _ = self.classifier.classify(frame.raw_query)
        initial_tools = self._select_initial_tools(frame, category)

        # 3. 危険度判定
        risk = self._assess_risk(frame, intent)

        return RoutingDecision(
            initial_phase="EXPLORATION",  # 常にEXPLORATIONから
            initial_tools=initial_tools,
            priority_slots=priority_slots,
            risk_level=risk,
        )

    def _prioritize_missing(
        self,
        missing: list[str],
        intent: IntentType
    ) -> list[str]:
        """不足スロットの優先順位"""

        # MODIFY/IMPLEMENT の場合、target_feature が最優先
        if intent in [IntentType.MODIFY, IntentType.IMPLEMENT]:
            priority_order = [
                "target_feature",
                "observed_issue",
                "trigger_condition",
                "desired_action",
            ]
        else:
            priority_order = [
                "target_feature",
                "trigger_condition",
                "observed_issue",
                "desired_action",
            ]

        return [s for s in priority_order if s in missing]

    def _select_initial_tools(
        self,
        frame: QueryFrame,
        category: QuestionCategory
    ) -> list[str]:
        """初期ツール選択"""

        # target_feature が分かっていれば定義検索から
        if frame.target_feature:
            return ["find_definitions", "find_references", "search_text"]

        # 分からなければ広く探索
        return ["query", "search_text", "analyze_structure"]

    def _assess_risk(self, frame: QueryFrame, intent: IntentType) -> str:
        """危険度判定"""

        # desired_action があるのに observed_issue がない → 高リスク
        if frame.desired_action and not frame.observed_issue:
            return "HIGH"

        # MODIFY で target_feature が不明 → 高リスク
        if intent == IntentType.MODIFY and not frame.target_feature:
            return "HIGH"

        # observed_issue が曖昧 → 中リスク
        if frame.observed_issue and len(frame.observed_issue) < 10:
            return "MEDIUM"

        return "LOW"
```

### QueryClassifier の新しい位置づけ

| Before | After |
|--------|-------|
| 正解カテゴリを当てる | 探索順序のヒント |
| 失敗すると致命的 | 失敗しても探索で補正 |
| Routerの中核 | Routerの補助 |

---

## EXPLORATION フェーズの拡張

### Before（v3.5）

```
EXPLORATION:
├── query / find_definitions / find_references 等を使う
├── submit_understanding で成果提出
└── サーバーが成果条件を評価
```

### After（v3.6）

```
EXPLORATION:
├── Step 0: QueryFrame構築（自動）
├── Step 1: 不足スロットの探索
├── Step 2: コード理解の探索
├── Step 3: submit_understanding で成果提出
└── サーバーが成果条件を評価
```

### 新しいフロー

```
User Query
    │
    ▼
┌─────────────────┐
│ QueryDecomposer │  ← LLMが構造化
│ (サーバー検証)   │  ← サーバーが幻覚チェック
└────────┬────────┘
         │ QueryFrame
         ▼
┌─────────────────┐
│     Router      │  ← 交通整理のみ
│ (判断しない)     │
└────────┬────────┘
         │ RoutingDecision
         ▼
┌─────────────────────────────────────┐
│         EXPLORATION フェーズ         │
│                                     │
│  1. priority_slots を埋める探索     │
│     └─ LLMがツールを使う            │
│     └─ サーバーは成果条件で検証     │
│                                     │
│  2. コード理解の探索                │
│     └─ symbols / entry_points      │
│     └─ existing_patterns           │
│                                     │
│  3. submit_understanding            │
│     └─ QueryFrame + 探索結果        │
│     └─ サーバーが総合評価           │
└─────────────────────────────────────┘
         │
         ▼
    SEMANTIC / READY
```

### submit_understanding の拡張

```python
# v3.5
submit_understanding(
    symbols_identified=[...],
    entry_points=[...],
    existing_patterns=[...],
    files_analyzed=[...],
)

# v3.6（追加）
submit_understanding(
    # 既存
    symbols_identified=[...],
    entry_points=[...],
    existing_patterns=[...],
    files_analyzed=[...],

    # 新規：QueryFrameの補完結果
    resolved_frame={
        "target_feature": "AuthService",           # 探索で特定
        "trigger_condition": "invalid token input", # 探索で特定
        "observed_issue": "JWT validation fails",   # 探索で特定
        "desired_action": "add token refresh",      # 元のまま
    },
    slot_evidence={
        "target_feature": {
            "tool": "find_definitions",
            "result": "AuthService defined in auth/service.py:15"
        },
        ...
    }
)
```

---

## 成果条件の拡張

### v3.5 の成果条件

```python
# IMPLEMENT/MODIFY の最低要件
symbols_identified: 3個以上
entry_points: 1個以上
files_analyzed: 2個以上
existing_patterns: 1個以上
```

### v3.6 の成果条件（追加）

```python
# QueryFrame関連の追加条件
if intent in [IMPLEMENT, MODIFY]:
    # target_feature は必須（探索で特定できなければ SEMANTIC へ）
    assert resolved_frame.target_feature is not None

    # observed_issue がある場合、証拠が必要
    if original_frame.observed_issue:
        assert slot_evidence.get("observed_issue") is not None
```

---

## LLMの役割整理

### やること（積極的に使う）

| 役割 | 具体的な作業 |
|------|-------------|
| 構造化 | 自然文 → QueryFrame |
| 探索 | ツールを使って情報収集 |
| 要約 | 探索結果の整理 |
| 仮説生成 | SEMANTICフェーズでの推測 |

### やらないこと（サーバーが担う）

| 役割 | サーバーの実装 |
|------|---------------|
| 抽出結果の検証 | `_has_evidence()` で幻覚チェック |
| 成果条件の判定 | `submit_understanding` の評価ロジック |
| フェーズ遷移の決定 | confidence計算とゲート判定 |
| Writeの許可 | `check_write_target` |

---

## 他AIからの提案と検討

### 提案1: mapped_symbols スロットの追加

**提案内容:**

```python
@dataclass
class QueryFrame:
    # 既存スロット
    target_feature: str | None
    trigger_condition: str | None
    observed_issue: str | None
    desired_action: str | None

    # 追加候補
    mapped_symbols: list[str] = field(default_factory=list)  # 探索で見つけた実際のシンボル名
```

**目的:** NL（自然言語）→ コードの架け橋

| フェーズ | target_feature | mapped_symbols |
|---------|----------------|----------------|
| 初期 | "ログイン機能" | [] |
| 探索後 | "ログイン機能" | ["AuthService", "LoginController"] |

**私の見解:** 採用。NLとコードの紐付けを明示的に持つことで、整合性検証が明確になる。

---

### 提案2: アンカー・チェック（幻覚検証）

**提案内容:**

LLMに「抽出した文字列がraw_queryのどの位置（オフセット）にあるか」を返させ、サーバーが検証する。

**私の懸念:**

- 日本語でのオフセット計算はLLMにとって難しい（文字カウントが不正確になりやすい）
- 複雑化の割にメリットが少ない

**代替案: 引用ベースの検証**

```python
# LLMへのプロンプト
"""
各スロットについて、raw_queryから該当部分を引用してください。

出力形式：
{
  "target_feature": {
    "value": "ログイン機能",
    "quote": "ログイン機能で"  ← raw_queryからの引用
  },
  ...
}
"""

# サーバーの検証
def _validate_with_quote(self, extracted: dict, raw_query: str) -> dict:
    validated = {}
    for slot, data in extracted.items():
        quote = data.get("quote", "")
        value = data.get("value", "")

        # 引用がraw_queryに含まれているか
        if quote and quote in raw_query:
            validated[slot] = value
        else:
            validated[slot] = None  # 幻覚として却下

    return validated
```

**メリット:**
- オフセット計算より単純
- 日本語でも安定
- 引用が含まれているかの文字列検索だけで済む

---

### 提案3: スロット欠損 → ツール選択のマッピング

**提案内容:**

| 欠損スロット | 初期ツール | 理由 |
|-------------|-----------|------|
| `target_feature` | `query`, `get_symbols` | 全体把握が必要 |
| `trigger_condition` | `search_text` | 条件文言をコードから探す |
| `observed_issue` | `search_text` | エラーメッセージ等を探す |
| `desired_action` | `find_references` | 影響範囲を把握 |

**私の見解:** 採用。Routerの「機械的な選択」をより明確にできる。

```python
SLOT_TO_TOOLS: dict[str, list[str]] = {
    "target_feature": ["query", "get_symbols", "analyze_structure"],
    "trigger_condition": ["search_text", "find_definitions"],
    "observed_issue": ["search_text", "query"],
    "desired_action": ["find_references", "analyze_structure"],
}

def _select_tools_from_missing(self, missing_slots: list[str]) -> list[str]:
    tools = []
    for slot in missing_slots:
        tools.extend(SLOT_TO_TOOLS.get(slot, []))
    return list(dict.fromkeys(tools))[:4]  # 重複排除、最大4つ
```

---

### 提案4: NL→シンボル整合性検証

**提案内容:**

`submit_understanding` 時に、`target_feature`（NL）と `symbols_identified`（コード）の整合性をチェック。

```python
def validate_exploration(frame: QueryFrame, symbols_found: list[str]) -> list[str]:
    errors = []

    # target_feature に対応するシンボルが見つかっているか
    if frame.target_feature:
        if not any(is_related(frame.target_feature, sym) for sym in symbols_found):
            errors.append(
                f"'{frame.target_feature}'に関連するシンボルが見つかっていません"
            )

    return errors

def is_related(nl_term: str, symbol: str) -> bool:
    """NL用語とシンボルの関連性チェック"""
    # 例: "ログイン機能" と "LoginController" → 関連あり
    # 実装: 部分一致、シノニム辞書、または軽量LLM判定
    ...
```

**私の見解:** 採用。ただし `is_related` の実装は段階的に：

1. **Phase 1:** 単純な部分一致（"login" in "LoginController".lower()）
2. **Phase 2:** シノニム辞書（"認証" ↔ "Auth"）
3. **Phase 3:** 必要なら軽量LLM判定

---

## 議論ポイント（更新）

### 1. QueryFrame のスロット設計

**結論案:** 5スロットに拡張

```python
target_feature: str | None        # NL: 対象機能
trigger_condition: str | None     # NL: 再現条件
observed_issue: str | None        # NL: 問題
desired_action: str | None        # NL: 期待する修正
mapped_symbols: list[str]         # Code: 探索で見つけたシンボル（空から開始）
```

### 2. 幻覚チェックの実装

**結論案:** 引用ベースの検証

- LLMに「raw_queryからの引用」を返させる
- サーバーは引用が実際にraw_queryに含まれるかを検証
- オフセットは不要

### 3. Router の残存価値

**結論案:** QueryClassifier は「補助」として残す

- スロット欠損 → ツール選択がメイン
- カテゴリ分類は優先度調整のヒント程度

### 4. フェーズ構成

**結論案:** 案B（EXPLORATION統合）

```
start_session
    ↓
QueryDecomposer（自動実行、セッション開始時）
    ↓
EXPLORATION（スロット埋め + コード理解）
    ↓
SEMANTIC（必要時）
    ↓
VERIFICATION（devrag使用時）
    ↓
READY
```

理由：
- フェーズ数を増やさない
- QueryDecomposerは「セッション開始の一部」として自然
- 既存の成果条件の枠組みで検証可能

---

## 設計思想まとめ

```
自然文を直接解決しようとするから失敗する。

自然文を構造（QueryFrame）に変え、
その「構造の穴」をコード探索ツールで埋めていく。
```

### 依頼の解像度に応じた可変速実装

| 依頼タイプ | QueryFrame状態 | 動作 |
|-----------|---------------|------|
| 曖昧な依頼 | target_feature以外が空 | 探索で「事実」を見つけるまでREADYに行けない |
| 具体的な依頼 | 全スロット埋まり | 整合性確認だけで素早くREADYへ |

---

## バリデーションロジックの具体化

### validate_slot 関数

```python
def validate_slot(
    self,
    slot_name: str,
    extracted_data: dict,
    raw_query: str
) -> str | None:
    """
    スロットの値を検証し、幻覚を排除する。

    Returns:
        検証済みの値、または None（幻覚として却下）
    """
    value = extracted_data.get("value")
    quote = extracted_data.get("quote")

    # 1. 引用が存在しない、または原文に含まれていない場合は「幻覚」
    if not quote or quote not in raw_query:
        return None

    # 2. 抽出された値が引用と無関係な場合も却下
    # 例: 引用が「ログイン」なのに値が「ログアウト」
    if not self._is_semantically_consistent(value, quote):
        return None

    return value

def _is_semantically_consistent(self, value: str, quote: str) -> bool:
    """値が引用と意味的に一致しているか"""
    # Phase 1: 単純な包含チェック
    value_lower = value.lower()
    quote_lower = quote.lower()

    # 値の主要な単語が引用に含まれているか
    value_words = set(value_lower.split())
    quote_words = set(quote_lower.split())

    # 少なくとも1単語が共通していればOK
    return bool(value_words & quote_words) or value_lower in quote_lower
```

---

## LLM出力スキーマ（JSON Schema）

LLMに「引用」を強制するためのスキーマ定義：

```json
{
  "type": "object",
  "properties": {
    "target_feature": { "$ref": "#/definitions/slot_data" },
    "trigger_condition": { "$ref": "#/definitions/slot_data" },
    "observed_issue": { "$ref": "#/definitions/slot_data" },
    "desired_action": { "$ref": "#/definitions/slot_data" }
  },
  "definitions": {
    "slot_data": {
      "type": ["object", "null"],
      "properties": {
        "value": {
          "type": "string",
          "description": "正規化された値"
        },
        "quote": {
          "type": "string",
          "description": "raw_queryからの一致する引用（原文そのまま）"
        }
      },
      "required": ["value", "quote"]
    }
  }
}
```

### LLMへのプロンプト例

```
以下のクエリから情報を抽出してください。

ルール：
- 明示されている情報のみ抽出
- 各スロットには「value」と「quote」を必ず含める
- 「quote」は raw_query からの正確な引用（原文そのまま）
- 推測・補完は禁止。不明な場合は null

クエリ: ログイン機能でXXXを入力したときに、XXXの問題があるので、XXXするように修正する

出力例：
{
  "target_feature": {
    "value": "ログイン機能",
    "quote": "ログイン機能で"
  },
  "trigger_condition": {
    "value": "XXXを入力したとき",
    "quote": "XXXを入力したときに"
  },
  ...
}
```

---

## 調査指示の自動生成

スロットが欠損している場合、サーバーが「何を調べるべきか」を指示：

```python
INVESTIGATION_HINTS: dict[str, dict] = {
    "target_feature": {
        "hint": "対象となる機能やモジュールが特定できません。",
        "action": "get_symbols や query を使用して、関連しそうなコードの全体像を把握してください。",
        "tools": ["query", "get_symbols", "analyze_structure"],
    },
    "observed_issue": {
        "hint": "修正の動機となる『現状の問題』が不明です。",
        "action": "search_text でエラー文言やログを確認してください。",
        "tools": ["search_text", "query"],
    },
    "trigger_condition": {
        "hint": "問題が発生する再現条件が特定されていません。",
        "action": "コード内の if 文や例外処理の条件を探索してください。",
        "tools": ["search_text", "find_definitions"],
    },
    "desired_action": {
        "hint": "期待する修正方針が不明です。",
        "action": "find_references で影響範囲を把握してください。",
        "tools": ["find_references", "analyze_structure"],
    },
}

def generate_investigation_guidance(missing_slots: list[str]) -> dict:
    """欠損スロットに基づいて調査指示を生成"""
    guidance = {
        "missing_slots": missing_slots,
        "hints": [],
        "recommended_tools": [],
    }

    for slot in missing_slots:
        if slot in INVESTIGATION_HINTS:
            info = INVESTIGATION_HINTS[slot]
            guidance["hints"].append({
                "slot": slot,
                "hint": info["hint"],
                "action": info["action"],
            })
            guidance["recommended_tools"].extend(info["tools"])

    # 重複排除
    guidance["recommended_tools"] = list(dict.fromkeys(guidance["recommended_tools"]))

    return guidance
```

---

## 未解決の問いへの見解

### Q1: mapped_symbols の確定タイミング

**問い:** EXPLORATION中に動的に更新すべきか、最後に一括で提出すべきか？

**私の見解:** **動的更新 + 最終確認** のハイブリッド

```python
@dataclass
class QueryFrame:
    # ... 既存フィールド ...

    # 動的に更新される（ツール使用ごとに追加可能）
    mapped_symbols: list[str] = field(default_factory=list)

    # 最終確認用（submit_understanding時に確定）
    confirmed_symbols: list[str] | None = None
```

**理由:**
1. **動的更新**: 探索中にLLMが「これが関連シンボルだ」と判断したら即座に追加
2. **最終確認**: `submit_understanding` 時にサーバーが整合性チェック
3. **証跡**: どのツールでどのシンボルを見つけたかを `slot_evidence` に記録

```python
# 探索中（動的追加）
session.frame.mapped_symbols.append("AuthService")

# submit_understanding時（最終確認）
submit_understanding(
    symbols_identified=["AuthService", "LoginController"],
    resolved_frame={
        "target_feature": "ログイン機能",
        "mapped_symbols": ["AuthService", "LoginController"],  # 確定
    },
    slot_evidence={
        "AuthService": {"tool": "find_definitions", "file": "auth/service.py"},
        "LoginController": {"tool": "find_references", "file": "controllers/login.py"},
    }
)
```

---

### Q2: シノニム辞書の管理

**問い:** "ログイン" = "AuthService" のような紐付けを自動生成すべきか？

**私の見解:** **Phase 1では不要、Phase 2以降で検討**

**理由:**

| アプローチ | メリット | デメリット |
|-----------|---------|-----------|
| 自動生成 | 手間が省ける | 精度が不安定、メンテナンスコスト |
| 手動定義 | 精度が高い | 初期コスト |
| LLM判定 | 柔軟 | 毎回コストがかかる |

**Phase 1の方針:**

```python
def is_related(nl_term: str, symbol: str) -> bool:
    """単純な部分一致 + 基本的なシノニム"""

    # 1. 部分一致
    nl_lower = nl_term.lower()
    sym_lower = symbol.lower()

    if nl_lower in sym_lower or sym_lower in nl_lower:
        return True

    # 2. 基本的なシノニム（ハードコード、最小限）
    BASIC_SYNONYMS = {
        "ログイン": ["login", "auth", "signin"],
        "認証": ["auth", "authentication"],
        "ユーザー": ["user", "account"],
    }

    for jp, en_list in BASIC_SYNONYMS.items():
        if jp in nl_term:
            if any(en in sym_lower for en in en_list):
                return True

    return False
```

**Phase 2以降（必要に応じて）:**
- コードベースからシノニム候補を自動抽出
- 確定したペアを学習データとして蓄積
- `outcome_log` の成功パターンから逆算

---

## 可変速実装のメカニズム（確定版）

### 曖昧な依頼の場合

```
入力: 「ログイン機能直して」

QueryFrame:
  target_feature: "ログイン機能"
  trigger_condition: null
  observed_issue: null      ← 欠損
  desired_action: "直して"

Router判定:
  missing_slots: ["trigger_condition", "observed_issue"]
  risk_level: HIGH
  recommended_tools: ["search_text", "query"]

挙動:
  → 強制的に長い EXPLORATION
  → observed_issue の証拠が見つかるまで READY に行けない
```

### 詳細な依頼の場合

```
入力: 「XXXのときにXXXというエラーが出るので、XXXを修正して」

QueryFrame:
  target_feature: "XXX"（探索で特定）
  trigger_condition: "XXXのとき"
  observed_issue: "XXXというエラー"
  desired_action: "XXXを修正"

Router判定:
  missing_slots: []
  risk_level: LOW
  recommended_tools: ["find_definitions", "find_references"]

挙動:
  → 最短ルートで探索
  → mapped_symbols の整合性確認のみで READY へ
```

---

## Slot Filling の流れ

ツール結果をスロットに流し込むフロー：

```
1. ツール実行
   find_definitions("AuthService")
   → result: {file: "auth/service.py", line: 15, ...}

2. LLMが解釈
   「AuthServiceはログイン機能の中核クラスである」

3. スロット更新
   frame.mapped_symbols.append("AuthService")
   slot_evidence["AuthService"] = {
     tool: "find_definitions",
     result: "auth/service.py:15"
   }

4. 欠損スロットの充足確認
   if "AuthService" relates to frame.target_feature:
     → target_feature は確定

5. 次のツール選択
   remaining_missing = get_missing_slots(frame)
   next_tools = generate_investigation_guidance(remaining_missing)
```

---

## DEVRAG の位置付け（v3.6）

### 原則：「事実」と「推論」の分離を徹底

v3.6の設計思想は「自然文を構造化し、その穴を**事実**で埋める」こと。
DEVRAGを初期段階で使うと、LLMが「事実」を見つける前に「それっぽい場所」への推論を優先し、サボり癖が再発する。

| ツール | 性格 | v3.6での役割 |
|--------|------|-------------|
| Code Intel（ripgrep等） | **事実確認** | スロットの引用に基づき、コード上の実体を特定 |
| DEVRAG | **仮説・推論** | 事実検索で埋まらなかった時の意味的補完 |

### DEVRAGの使用タイミング

```
EXPLORATION（事実探索）
    ↓ スロットが埋まらない
SEMANTIC（DEVRAG許可）
    ↓ 仮説を生成
VERIFICATION（事実で検証）
    ↓ 確認できた
READY
```

**ルール:**
- **EXPLORATION**: DEVRAG使用禁止
- **SEMANTIC**: missing_slots を解消するためにDEVRAG使用
- **VERIFICATION**: DEVRAGが埋めたスロットを再度Code Intelで確認

---

### slot_source による FACT/HYPOTHESIS の区別

```python
@dataclass
class QueryFrame:
    # 既存スロット
    target_feature: str | None
    trigger_condition: str | None
    observed_issue: str | None
    desired_action: str | None
    mapped_symbols: list[str] = field(default_factory=list)

    # 新規：どのフェーズで確定したか
    slot_source: dict[str, str] = field(default_factory=dict)
    # 例: {"target_feature": "FACT", "observed_issue": "HYPOTHESIS"}
```

| ソース | 意味 | 次のアクション |
|--------|------|---------------|
| `FACT` | EXPLORATIONで確定 | そのままREADYへ進める |
| `HYPOTHESIS` | SEMANTICで推測 | VERIFICATION必須 |

---

### DEVRAG起動条件の厳格化

```python
def should_allow_devrag(frame: QueryFrame, exploration_results: dict) -> tuple[bool, str | None]:
    """DEVRAGへの移行を許可するか判定"""

    # 1. 最低限の事実探索を行ったか
    tools_used = exploration_results.get("tools_used", [])
    required_tools = ["find_definitions", "find_references", "search_text"]

    if not all(t in tools_used for t in required_tools):
        return False, "Required tools not used yet"

    # 2. 事実探索で埋まらなかったスロットがあるか
    unfilled_slots = [
        slot for slot in ["target_feature", "observed_issue"]
        if frame.slot_source.get(slot) != "FACT"
    ]

    if not unfilled_slots:
        return False, "All critical slots already filled with FACT"

    # 3. DEVRAGを使う理由が正当か
    valid_reasons = {
        "target_feature": ["no_definition_found", "architecture_unknown"],
        "observed_issue": ["no_similar_implementation", "context_fragmented"],
    }

    return True, unfilled_slots[0]  # 最初の未充足スロットを返す
```

---

### mapped_symbols の確実性管理

DEVRAGで見つけたシンボルは「確実性スコア」を持つ：

```python
@dataclass
class MappedSymbol:
    name: str
    source: Literal["FACT", "HYPOTHESIS"]
    confidence: float  # 0.0-1.0
    evidence: dict

# 例
frame.mapped_symbols = [
    MappedSymbol(
        name="AuthService",
        source="FACT",
        confidence=1.0,
        evidence={"tool": "find_definitions", "file": "auth/service.py:15"}
    ),
    MappedSymbol(
        name="TokenValidator",
        source="HYPOTHESIS",
        confidence=0.7,
        evidence={"tool": "devrag", "query": "JWT validation"}
    ),
]
```

**VERIFICATIONでの確認:**

```python
def verify_hypothesis_symbols(frame: QueryFrame) -> list[str]:
    """HYPOTHESISシンボルを事実で確認"""
    errors = []

    for sym in frame.mapped_symbols:
        if sym.source == "HYPOTHESIS":
            # 実際にコード上に存在するか確認
            exists = code_intel.find_definitions(sym.name)
            if not exists:
                errors.append(f"Symbol '{sym.name}' not found in codebase")

    return errors
```

---

### 私の見解

**他AIの提案に完全に同意します。**

理由：

1. **v3.5の原則を維持**: DEVRAGは「最後の切り札」であり、安易に使わせない
2. **QueryFrameとの整合性**: slot_source で FACT/HYPOTHESIS を区別することで、追跡可能性が向上
3. **物理的制約の強化**: HYPOTHESISはVERIFICATION必須という制約が明確

**追加提案:**

```python
# submit_understanding時のチェック
def validate_for_ready(frame: QueryFrame) -> list[str]:
    errors = []

    # HYPOTHESISスロットが残っていればブロック
    for slot, source in frame.slot_source.items():
        if source == "HYPOTHESIS":
            errors.append(
                f"Slot '{slot}' is still HYPOTHESIS. "
                f"Must verify with code intel tools first."
            )

    return errors
```

これにより「DEVRAGで推測 → そのままREADY」というショートカットを物理的に不可能にします。

---

## 実装時の注意点（他AIからのアドバイス）

### 1. slot_evidence の強制記録

「LLMがツール結果を解釈してスロットを埋める」際、サーバー側で**どのツール実行がどのスロットを埋めたかのリンクを強制的に記録**する。

```python
# スロット更新時に必ず evidence を記録
def update_slot(
    frame: QueryFrame,
    slot_name: str,
    value: str,
    evidence: dict  # 必須
) -> None:
    setattr(frame, slot_name, value)
    frame.slot_evidence[slot_name] = evidence  # 強制記録

# evidence の構造
{
    "tool": "find_definitions",
    "params": {"symbol": "AuthService"},
    "result_summary": "auth/service.py:15",
    "timestamp": "2024-01-15T10:30:00"
}
```

**目的:** `/outcome` で失敗報告された際、「どのツールの解釈ミスが原因か」を `DecisionLog` から正確に追跡可能にする。

---

### 2. risk_level の動的反映

`risk_level: HIGH` の場合、EXPLORATION フェーズの**最低成果条件を自動的に引き上げる**。

```python
def get_exploration_requirements(risk_level: str, intent: IntentType) -> dict:
    """リスクレベルに応じた成果条件"""

    base = {
        "symbols_identified": 3,
        "entry_points": 1,
        "files_analyzed": 2,
        "existing_patterns": 1,
    }

    if risk_level == "HIGH":
        # 高リスク時は条件を厳しく
        return {
            "symbols_identified": 5,  # 3 → 5
            "entry_points": 2,         # 1 → 2
            "files_analyzed": 4,       # 2 → 4
            "existing_patterns": 2,    # 1 → 2
            "required_slot_evidence": ["target_feature", "observed_issue"],  # 追加
        }
    elif risk_level == "MEDIUM":
        return {
            **base,
            "required_slot_evidence": ["target_feature"],
        }
    else:
        return base
```

**目的:** 設計思想を「物理的な制約」として機能させる。曖昧な依頼では探索を強制的に深くする。

---

## 設計の進化点まとめ

| 観点 | v3.5 | v3.6 |
|------|------|------|
| 自然文対応 | 1クエリ=1カテゴリ | QueryFrame（構造化） |
| 幻覚チェック | なし | 引用ベース検証 |
| Router責務 | 判断の中核 | 交通整理（スロット→ツール） |
| 成果条件 | 固定 | risk_level で動的調整 |
| 追跡可能性 | tool_calls | slot_evidence で因果関係を記録 |

---

## 次のステップ

### Phase 1: 基盤作成
1. `QueryFrame` クラスの実装
2. `QueryDecomposer`（引用チェック付き）の実装
3. `validate_slot` の実装

### Phase 2: Router更新
4. Router のロジックをスロットベースへ移行
5. `generate_investigation_guidance` の実装

### Phase 3: フェーズ拡張
6. `submit_understanding` の拡張（resolved_frame, slot_evidence）
7. risk_level による成果条件の動的調整
8. `is_related`（NL→シンボル整合性）の実装

### Phase 4: 検証
9. テストケース作成（曖昧な依頼 / 詳細な依頼）
10. outcome_log との連携確認

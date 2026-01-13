# Code Intelligence MCP Server v3.7 設計資料

## 概要

v3.6の「スロット構造」を土台に、パターンマッチ依存を排除し、真の意味理解に基づくシステムへ進化させる。

---

## 設計方針の転換

| 領域 | v3.6 (現状) | v3.7 (提案) |
|------|-------------|-------------|
| クエリカテゴリ | 正規表現キーワードマッチ | Intent × スロット欠損状況 |
| 用語の関連性 | ハードコード辞書 + 文字列包含 | LLM判定 + ベクトル類似度 |
| 幻覚チェック | 原文一致 + 文字重複率 | ベクトル空間での意味的一致 |

---

## 提案1: Router の完全スロット化

### 現状の問題

`router.py` の `QueryClassifier` が正規表現ベースで残存:

```python
class QueryClassifier:
    PATTERNS = {
        "debug": [r"バグ", r"エラー", r"動かない", ...],
        "feature": [r"追加", r"実装", r"作成", ...],
        # ...
    }
```

### 変更内容

1. **QueryClassifier クラスを削除**

2. **ルーティング判断基準を2点に絞る:**
   - `Intent` (MODIFY / ADD / DELETE / EXPLORE)
   - `missing_slots` (どのスロットが空か)

3. **制御ロジックの完全スロット化:**

```python
# 新しい router.py
class Router:
    # カテゴリではなく、スロット欠損からツールを決定
    SLOT_TO_TOOLS = {
        "target_feature": ["find_definitions", "search_text"],
        "trigger_condition": ["find_references", "search_text"],
        "observed_issue": ["search_text", "analyze_structure"],
        "desired_action": [],  # 探索不要
    }

    def create_plan(self, intent: str, query_frame: QueryFrame) -> Plan:
        missing = query_frame.get_missing_slots()
        tools = []

        for slot in missing:
            tools.extend(self.SLOT_TO_TOOLS.get(slot, []))

        # Intent による調整
        if intent == "EXPLORE":
            tools.append("analyze_structure")

        return Plan(
            tools=list(dict.fromkeys(tools)),  # 重複排除
            risk_level=self._calc_risk(missing),
        )

    def _calc_risk(self, missing: list) -> str:
        if len(missing) >= 3:
            return "HIGH"
        elif len(missing) >= 2:
            return "MEDIUM"
        return "LOW"
```

### 削除対象

- `QueryClassifier` クラス全体
- `PATTERNS` 定数
- `analyze_intent()` メソッド
- カテゴリに基づく重み付けロジック

---

## 提案2: 意味的紐付けの LLM 委譲

### 現状の問題

`query_frame.py` のハードコード辞書と文字列照合:

```python
BASIC_SYNONYMS = {
    "ログイン": ["login", "signin", "auth"],
    "ユーザー": ["user", "member", "account"],
    # ...
}

def is_related(nl_term: str, symbol: str) -> bool:
    # 辞書ベースの部分一致...
```

### 変更内容

1. **BASIC_SYNONYMS を削除**

2. **is_related を LLM 再問いかけツールに置換**

3. **新ツール: `validate_symbol_relevance`**

```python
# code_intel_server.py に追加
@server.tool()
async def validate_symbol_relevance(
    target_feature: str,
    symbols_identified: list[str],
) -> dict:
    """
    LLMに関連性判定を委譲するためのプロンプトを返す。
    サーバーは判定しない。LLMが判定する。
    """
    return {
        "validation_prompt": f"""
以下のシンボル群から、対象機能「{target_feature}」に関連するものを選んでください。

シンボル一覧:
{chr(10).join(f"- {s}" for s in symbols_identified)}

回答形式:
{{
  "relevant_symbols": ["関連するシンボル名"],
  "reasoning": "選定理由",
  "code_evidence": "コード上の根拠（メソッド名、コメント、命名規則など）"
}}

※ code_evidence は必須。根拠なしの判定は無効。
""",
        "action_required": "LLMがこのプロンプトに回答し、set_query_frame で mapped_symbols を更新",
    }
```

**重要: LLMに根拠説明を強制**

単に「関連あり」と言わせるだけでなく、`code_evidence` フィールドでコード上の根拠を説明させる。これにより「なんとなく関連」というサボりを防止。

```python
# 回答例
{
  "relevant_symbols": ["AuthService"],
  "reasoning": "ログイン機能を担当するサービスクラス",
  "code_evidence": "AuthService.login() メソッドが存在、クラスコメントに「認証処理」と記載"
}
```

4. **submit_understanding 時の検証フロー変更:**

```python
# session.py
def validate_exploration_consistency(self) -> ValidationResult:
    # 旧: is_related() で辞書マッチング
    # 新: LLM判定結果の存在確認のみ

    if not self.query_frame.mapped_symbols:
        return ValidationResult(
            valid=False,
            reason="mapped_symbols が空。validate_symbol_relevance を実行してください。"
        )

    # LLMが判定した結果を信頼（サーバーは再検証しない）
    return ValidationResult(valid=True)
```

### 削除対象

- `BASIC_SYNONYMS` 定数
- `is_related()` 関数
- `validate_nl_symbol_mapping()` 関数

---

## 提案3: 埋め込み（Embedding）の活用

### 現状の問題

`_is_semantically_consistent` の文字重複率判定:

```python
def _is_semantically_consistent(value: str, quote: str) -> bool:
    # 文字レベルの重複チェック
    overlap_ratio = len(common) / min(len(value_chars), len(quote_chars))
    return overlap_ratio >= 0.5
```

「サインイン」と「Login」のように、一文字も重ならないが意味が同一のケースを判定できない。

### 変更内容

1. **ベクトル類似度による意味的一致判定**

```python
# tools/embedding.py (新規)
from sentence_transformers import SentenceTransformer

class EmbeddingValidator:
    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2"):
        self.model = SentenceTransformer(model_name)
        self.threshold = 0.6  # コサイン類似度の閾値

    def is_semantically_similar(self, text1: str, text2: str) -> tuple[bool, float]:
        """2つのテキストの意味的類似度を判定"""
        embeddings = self.model.encode([text1, text2])
        similarity = cosine_similarity([embeddings[0]], [embeddings[1]])[0][0]
        return similarity >= self.threshold, float(similarity)

    def find_related_symbols(
        self,
        nl_term: str,
        symbols: list[str],
        top_k: int = 3
    ) -> list[dict]:
        """自然言語用語に関連するシンボルをベクトル検索"""
        nl_embedding = self.model.encode([nl_term])[0]
        symbol_embeddings = self.model.encode(symbols)

        similarities = cosine_similarity([nl_embedding], symbol_embeddings)[0]

        results = []
        for i, (sym, sim) in enumerate(zip(symbols, similarities)):
            if sim >= self.threshold:
                results.append({
                    "symbol": sym,
                    "similarity": float(sim),
                    "rank": len(results) + 1
                })

        return sorted(results, key=lambda x: x["similarity"], reverse=True)[:top_k]
```

2. **幻覚チェックの改善**

```python
# query_frame.py
class QueryDecomposer:
    def __init__(self, embedding_validator: EmbeddingValidator):
        self.embedder = embedding_validator

    def validate_slot(self, slot: str, value: str, quote: str, raw_query: str) -> SlotValidation:
        # Step 1: 原文に quote が含まれるか（完全一致）
        if quote in raw_query:
            return SlotValidation(valid=True, source="FACT")

        # Step 2: ベクトル類似度で意味的一致を判定
        is_similar, similarity = self.embedder.is_semantically_similar(value, quote)
        if is_similar:
            return SlotValidation(
                valid=True,
                source="FACT",
                confidence=similarity,
                note=f"Semantic match (similarity: {similarity:.2f})"
            )

        return SlotValidation(valid=False, reason="Quote not found and semantically dissimilar")
```

3. **提案2との統合（ハイブリッドアプローチ）**

```python
# LLM委譲 + Embedding検証の組み合わせ
async def validate_symbol_relevance(
    target_feature: str,
    symbols_identified: list[str],
    use_embedding: bool = True
) -> dict:
    result = {
        "validation_prompt": "...",  # LLM用プロンプト
    }

    if use_embedding and embedding_validator:
        # サーバー側でもベクトル検索でヒントを提供
        suggestions = embedding_validator.find_related_symbols(
            target_feature, symbols_identified
        )
        result["embedding_suggestions"] = suggestions
        result["note"] = "embedding_suggestions はサーバー算出。LLM判定の参考にしてください。"

    return result
```

### 依存関係の追加

```
# requirements.txt に追加
sentence-transformers>=2.2.0
scikit-learn>=1.0.0
```

### モデル選択

| モデル | サイズ | 多言語 | 採用 |
|--------|--------|--------|------|
| multilingual-e5-small | 470MB | ○ | **採用**（devrag統一） |
| paraphrase-multilingual-MiniLM-L12-v2 | 420MB | ○ | - |
| all-MiniLM-L6-v2 | 80MB | × | - |

**決定: `multilingual-e5-small`** - devragと同一ベクトル空間、遅延ロード

---

## EmbeddingValidator 実装仕様

### ライブラリとモデル

```python
# 使用ライブラリ
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

# モデル
model_name = "intfloat/multilingual-e5-small"
```

### E5モデルの特性

E5モデルは「クエリ」と「文書」を区別する性質がある。
比較時に `query:` 接頭辞を付けることで精度が向上。

```python
sentences = [f"query: {text1}", f"query: {text2}"]
embeddings = self.model.encode(sentences)
```

### 前処理：キャメルケース分解

シンボル名（`AuthService`）を自然言語（「認証機能」）と比較する際、
キャメルケースをスペース区切りに分解すると類似度スコアが安定。

```python
import re

def split_camel_case(symbol: str) -> str:
    """AuthService → Auth Service"""
    return re.sub(r'([a-z])([A-Z])', r'\1 \2', symbol)
```

### 推奨実装パターン

```python
class EmbeddingValidator:
    def __init__(self, model_name="intfloat/multilingual-e5-small"):
        self.model_name = model_name
        self._model = None  # 遅延ロード用

    @property
    def model(self):
        if self._model is None:
            # 最初の呼び出し時にのみロード (2〜5秒)
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def get_similarity(self, text1: str, text2: str) -> float:
        # E5モデルの特性を活かすため接頭辞を付与
        sentences = [f"query: {text1}", f"query: {text2}"]
        embeddings = self.model.encode(sentences)

        # コサイン類似度の算出
        sim = cosine_similarity([embeddings[0]], [embeddings[1]])[0][0]
        return float(sim)

    def validate_relevance(self, nl_term: str, symbol: str) -> dict:
        # キャメルケースを分解
        symbol_normalized = self.split_camel_case(symbol)
        similarity = self.get_similarity(nl_term, symbol_normalized)

        # 3層判定ロジック
        if similarity > 0.6:
            return {"status": "FACT", "score": similarity, "approved": True}
        elif similarity >= 0.3:
            return {"status": "FACT", "score": similarity, "approved": True, "risk": "HIGH"}
        else:
            return {"status": "REJECTED", "score": similarity, "approved": False}

    @staticmethod
    def split_camel_case(symbol: str) -> str:
        import re
        return re.sub(r'([a-z])([A-Z])', r'\1 \2', symbol)
```

### メモリとパフォーマンス

- モデルサイズ: 約470MB
- 遅延ロード採用（初回呼び出し時のみロード）
- シングルトン的に保持（SessionState経由）

---

## 3層類似度判定（スタック防止機構）

LLMが「関連あり」と判定しても、Embeddingの類似度に基づいてサーバーが最終判断を下す。
単純な拒否ではなく、「再調査への誘導」をセットにすることでスタックを防止。

### 判定ロジック

| similarity | 処理 | 効果 |
|------------|------|------|
| > 0.6 | **FACT として承認** | 高信頼、そのまま進行 |
| 0.3 - 0.6 | **承認するが risk_level を HIGH に強制** | 探索ノルマ増加 |
| < 0.3 | **物理的拒否 + 再調査ガイダンス** | 幻覚とみなす |

### 実装

```python
# tools/embedding.py
class EmbeddingValidator:
    THRESHOLD_HIGH = 0.6   # FACT として承認
    THRESHOLD_LOW = 0.3    # 物理的拒否ライン

    def validate_relevance(
        self,
        nl_term: str,
        symbol: str,
        llm_approved: bool,
        code_evidence: str
    ) -> ValidationResult:
        """LLM判定をEmbeddingで検証"""
        similarity = self.calculate_similarity(nl_term, symbol)

        if similarity > self.THRESHOLD_HIGH:
            return ValidationResult(
                approved=True,
                source="FACT",
                similarity=similarity,
                risk_adjustment=None
            )

        elif similarity >= self.THRESHOLD_LOW:
            # グレーゾーン: 承認するがリスク引き上げ
            return ValidationResult(
                approved=True,
                source="FACT",
                similarity=similarity,
                risk_adjustment="HIGH",
                warning=f"類似度 {similarity:.2f} はグレーゾーン。探索を強化してください。"
            )

        else:
            # 物理的拒否
            return ValidationResult(
                approved=False,
                source="REJECTED",
                similarity=similarity,
                reinvestigation_guidance=self._create_guidance(nl_term, symbol)
            )

    def _create_guidance(self, nl_term: str, symbol: str) -> dict:
        """拒否時の再調査ガイダンスを生成"""
        return {
            "reason": f"'{nl_term}' と '{symbol}' の類似度が低すぎます（< 0.3）",
            "next_actions": [
                f"search_text で '{nl_term}' に関連する別のシンボルを探す",
                f"find_references で '{symbol}' の使用箇所を確認し、本当に関連するか検証",
                "関連性を証明するコード片（コメント、命名規則）を見つける",
            ],
            "fallback": "事実検索で見つからない場合は submit_semantic で DEVRAG を使用"
        }
```

### 拒否時のレスポンス例

```json
{
  "approved": false,
  "source": "REJECTED",
  "similarity": 0.15,
  "reinvestigation_guidance": {
    "reason": "'ログイン機能' と 'ConfigLoader' の類似度が低すぎます（< 0.3）",
    "next_actions": [
      "search_text で 'ログイン機能' に関連する別のシンボルを探す",
      "find_references で 'ConfigLoader' の使用箇所を確認し、本当に関連するか検証",
      "関連性を証明するコード片（コメント、命名規則）を見つける"
    ],
    "fallback": "事実検索で見つからない場合は submit_semantic で DEVRAG を使用"
  }
}
```

### スタック回避フロー

```
LLMが「ConfigLoader はログイン機能に関連」と判定
        ↓
Embedding検証: similarity = 0.15 (< 0.3)
        ↓
サーバーが拒否 + 再調査ガイダンスを返却
        ↓
LLMは別のシンボルを search_text で探索
        ↓
「AuthService」を発見、similarity = 0.72
        ↓
FACT として承認、探索続行
```

**拒否 = 停止ボタンではなく、より正確な探索への切り替えスイッチ**

---

## アーキテクチャ変更の全体像

```
v3.6:
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ QueryClassifier │ → │ BASIC_SYNONYMS │ → │ 文字重複率  │
│ (正規表現)      │     │ (辞書)          │     │ (overlap)   │
└─────────────┘     └──────────────┘     └─────────────┘

v3.7:
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ Intent +     │ → │ LLM再問いかけ │ → │ Embedding   │
│ missing_slots │     │ (委譲)        │     │ (ベクトル)  │
└─────────────┘     └──────────────┘     └─────────────┘
```

---

## 実装フェーズ

### Phase 1: Router完全スロット化
1. `QueryClassifier` の呼び出し箇所を特定
2. スロットベースのルーティングに置換
3. `QueryClassifier` クラスを削除
4. テスト更新

### Phase 2: LLM委譲
1. `validate_symbol_relevance` ツールを追加
2. `submit_understanding` の検証ロジック変更
3. `BASIC_SYNONYMS`, `is_related` を削除
4. `/code` スキルのプロンプト更新

### Phase 3: Embedding導入
1. `EmbeddingValidator` クラス実装
2. `_is_semantically_consistent` をベクトル判定に置換
3. `validate_symbol_relevance` との統合
4. モデルダウンロード・初期化処理

### Phase 4: 成功ペアの自動キャッシュ

LLMが「関連あり」と判定し、Embeddingで承認されたペアを自動的にキャッシュし、次回以降の探索に活用する。

1. **キャッシュ構造**

```python
# .code-intel/learned_pairs.json
{
  "version": 1,
  "pairs": [
    {
      "nl_term": "ログイン",
      "symbol": "AuthService",
      "similarity": 0.72,
      "code_evidence": "AuthService.login() メソッドが存在",
      "session_id": "sess_abc123",
      "learned_at": "2025-01-12T10:00:00"
    }
  ]
}
```

2. **学習トリガー**

```python
# session.py
def record_outcome(self, outcome: str) -> None:
    if outcome == "success":
        # 成功時に mapped_symbols を learned_pairs に追加
        for symbol in self.query_frame.mapped_symbols:
            self._cache_learned_pair(
                nl_term=self.query_frame.target_feature,
                symbol=symbol,
                code_evidence=self.query_frame.slot_evidence.get("mapped_symbols", {}).get("code_evidence")
            )
```

3. **キャッシュ活用**

```python
# validate_symbol_relevance の拡張
async def validate_symbol_relevance(
    target_feature: str,
    symbols_identified: list[str],
) -> dict:
    # キャッシュから既知のペアを検索
    cached = load_learned_pairs()
    known_matches = [
        p for p in cached["pairs"]
        if p["nl_term"] == target_feature and p["symbol"] in symbols_identified
    ]

    return {
        "validation_prompt": "...",
        "cached_matches": known_matches,  # 「前回成功したペア」として提示
        "note": "cached_matches は過去に成功したペア。優先的に採用してください。"
    }
```

4. **キャッシュの有効期限・クリーンアップ**
   - 一定期間（例: 30日）経過したペアは削除
   - コードが大幅に変更された場合のリセット機構

---

## 決定済み事項

1. **LLMに根拠説明を強制** ✅
   - `code_evidence` フィールドを必須化
   - 「なんとなく関連」を許さない

2. **Phase 4: 成功ペアの自動キャッシュ** ✅
   - `.code-intel/learned_pairs.json` に蓄積
   - 次回探索時に優先的に提示

3. **サーバーの物理的拒否権（3層判定）** ✅
   - スタック防止のため、拒否時は再調査ガイダンスを提供
   - 詳細は「3層類似度判定」セクション参照

4. **Embedding ON/OFF オプション** → **不要**
   - 軽量LLMではこのMCPサーバー自体を使用しない
   - Embeddingは常時有効

---

## 追加決定事項

5. **Embeddingモデル選択** ✅
   - `multilingual-e5-small` に統一（devragと共有）
   - 理由: ベクトル空間の同一性、日英混合への適応力、キャッシュ効率

6. **モデルロード戦略** ✅
   - 遅延ロード（Lazy Loading）を採用
   - 理由: MCPクライアントの起動タイムアウト回避
   - 初回の `validate_symbol_relevance` 呼び出し時に2〜5秒のロード

---

## 残りの検討事項（実装後に判断）

1. **パフォーマンス**
   - Embedding計算のオーバーヘッド → 実測で判断
   - LLM再問いかけによるトークン消費増加 → 実測で判断

2. **エッジケース**
   - Embeddingもマッチしない、LLMも判断できない場合
   - → 3層判定のフォールバック（DEVRAG）で対応済み

---

## 実装状況

- [x] Phase 1: Router完全スロット化 - `tools/router.py`
- [x] Phase 2: LLM委譲 + code_evidence - `code_intel_server.py`
- [x] Phase 3: Embedding導入 - `tools/embedding.py`
- [x] Phase 4: 成功ペアキャッシュ - `tools/learned_pairs.py`
- [x] `/code` スキルプロンプト更新 - `.claude/commands/code.md`

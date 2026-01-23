# Step 5: Symbol Validation の分析

## 注意

**今回の実測（session_20260123_160021）ではStep 5は実行されていません。**

理由: submit_understanding 後のサーバー評価が "low" または一貫性NGのため、Step 6（SEMANTIC）へ直接スキップ。

以下は、Step 5が実行される場合の想定分析です。

---

## 実行条件

```
Step 4完了後のサーバー判定:
- 評価 "high" + 一貫性OK → Step 5実行
- それ以外 → Step 6へスキップ
```

---

## 想定フロー

```
16:03:40  submit_understanding 完了
  ↓
  ↓ [< 0.1秒: サーバー評価]
  ↓   - 評価: "high"
  ↓   - 一貫性: OK
  ↓   → Step 5実行を指示
  ↓
  ↓ [2-3秒: LLM思考]
  ↓   - サーバー指示を理解
  ↓   - シンボル抽出（discovered_symbols から）
  ↓   - validate_symbol_relevance 呼び出し準備
  ↓
16:03:42  validate_symbol_relevance 開始
  ↓
  ↓ [0.5-1秒: Embedding計算 + 類似度判定]
  ↓   - target_feature との類似度計算
  ↓   - cached_matches 検索
  ↓   - embedding_suggestions 生成
  ↓
16:03:43  validate_symbol_relevance 完了
  ↓
  ↓ [10-15秒: LLM思考]
  ↓   - cached_matches を優先的に確認
  ↓   - embedding_suggestions を参考
  ↓   - 各シンボルの承認/却下判断
  ↓   - code_evidence 記述（承認シンボルのみ）
  ↓
16:03:53  confirm_symbol_relevance 開始
  ↓
  ↓ [< 0.1秒: 確定処理]
  ↓   - サーバーが3段階判定（similarity）
  ↓   - > 0.6: FACT
  ↓   - 0.3-0.6: HIGH risk
  ↓   - < 0.3: 却下
  ↓
16:03:53  confirm_symbol_relevance 完了
  ↓
  ↓ [2-3秒: LLM思考]
  ↓   - 結果を確認
  ↓   - 次のステップ決定
  ↓
16:03:56  次のフェーズへ
```

---

## 想定内訳

| 処理 | 所要時間 | 割合 |
|------|----------|------|
| validate_symbol_relevance 実行 | 0.5-1秒 | 3-6% |
| confirm_symbol_relevance 実行 | < 0.1秒 | < 1% |
| LLM思考（validate前） | 2-3秒 | 12-18% |
| LLM思考（判断 + code_evidence） | 10-15秒 | 60-75% |
| LLM思考（confirm後） | 2-3秒 | 12-18% |
| **合計** | **15-22秒** | **100%** |

---

## ボトルネック分析

### 1位: シンボル判断とcode_evidence記述（10-15秒、60-75%）

**内容**:
- cached_matches と embedding_suggestions を分析
- 各シンボルが target_feature に関連するか判断
- 承認する場合、code_evidence を具体的に記述

**例**:
```json
{
  "mapped_symbols": [
    {
      "symbol": "AuthService",
      "approved": true,
      "code_evidence": "AuthService.login() method handles user authentication"
    },
    {
      "symbol": "Logger",
      "approved": false,
      "code_evidence": ""
    }
  ]
}
```

**問題点**:
- **この思考は削減不可**（LLMの判断が必要）
- シンボル数が多いと時間増加

### 2位: 前後のLLM思考（4-6秒、24-36%）

**内容**:
- validate前: ツール呼び出し準備（2-3秒）
- confirm後: 結果確認 + 次ステップ決定（2-3秒）

**問題点**:
- **この待機は削減可能**（バッチ化）

---

## 改善策

### ❌ バッチ化は不可

**validate と confirm の間にLLM判断が必要**

```
validate_symbol_relevance
  ↓ [10-15秒: LLM判断 - 必須]
  ↓   - 各シンボルの承認/却下
  ↓   - code_evidence 記述
confirm_symbol_relevance
```

**問題点**:
- validate の結果を見て、LLMが判断する必要がある
- バッチ化しても、この判断時間（10-15秒）は削減できない
- 削減できるのは前後の待機のみ（4-6秒）

**結論**: 判断が必要なため、バッチ化の優先度は低い

---

### ⚡ 中優先度（検討の価値あり）

**自動承認の活用**

cached_matches（過去に承認済み）は自動承認可能：

```python
def validate_and_confirm_symbols_auto(symbols, target_feature):
    result = validate_symbol_relevance(symbols, target_feature)

    # cached_matches は自動承認
    auto_approved = [
        {
            "symbol": match["symbol"],
            "approved": True,
            "code_evidence": match["cached_evidence"]
        }
        for match in result["cached_matches"]
    ]

    # embedding_suggestions のみLLM判断が必要
    needs_llm_judgment = [
        s for s in result["embedding_suggestions"]
        if s["symbol"] not in [m["symbol"] for m in auto_approved]
    ]

    return {
        "auto_approved": auto_approved,
        "needs_judgment": needs_llm_judgment
    }
```

**削減**: cached_matches が多い場合、判断時間を短縮（3-5秒）

---

### 💡 低優先度

**シンボル数の制限**

- 一度に判断するシンボル数を制限（例: 10個まで）
- 多すぎる場合は優先度の高いものだけ

**削減**: 判断時間を数秒短縮（シンボル数による）

---

## バッチ化の実現可能性

### 課題1: LLM判断は省略できない

**validate と confirm の間の判断は必須**:
- どのシンボルを承認するか
- code_evidence の記述

**解決策**: バッチ化しても判断時間（10-15秒）は残る

### 課題2: cached_matches の活用

**現状**: LLMが cached_matches を見て判断

**改善**: サーバー側で自動承認
- cached_matches は過去に承認済み → 自動でOK
- embedding_suggestions のみLLM判断

---

## 実装案: validate_and_confirm_batch

```python
def validate_and_confirm_batch(
    session_id: str,
    symbols: list[str],
    target_feature: str,
    auto_approve_cached: bool = True
) -> dict:
    """
    Symbol Validation を1ツールで実行。

    Args:
        auto_approve_cached: cached_matches を自動承認

    Returns:
        {
            "auto_approved": [...],  # cached_matches
            "needs_judgment": [...], # embedding_suggestions
            "next_step": "Review and confirm symbols..."
        }
    """
    # validate実行
    validation = validate_symbol_relevance(session_id, symbols, target_feature)

    result = {
        "cached_matches": validation["cached_matches"],
        "embedding_suggestions": validation["embedding_suggestions"]
    }

    if auto_approve_cached:
        # cached は自動承認
        result["auto_approved"] = validation["cached_matches"]
        result["needs_judgment"] = validation["embedding_suggestions"]

    return result
```

---

## 削減見込み

| 施策 | 削減時間 | 実装難易度 | 優先度 | 備考 |
|------|----------|------------|--------|------|
| validate + confirm バッチ化 | 4-6秒 | 低 | ❌ | 判断が必要なため不採用 |
| cached_matches 自動承認 | 3-5秒 | 中 | ⚡ | 検討の価値あり |
| シンボル数制限 | 2-3秒 | 低 | 💡 | 効果限定的 |

**現在**: 15-22秒（想定）
**v1.7での改善**: なし（判断が必要なフェーズのため）

---

## 注意事項

### Step 5は条件付き実行

**実行される条件**:
- EXPLORATION後の評価が "high"
- 一貫性チェックがOK

**実行されない条件**（今回のケース）:
- 評価が "low" → Step 6（SEMANTIC）へスキップ
- 一貫性NG → Step 6へスキップ

したがって、**Step 5の最適化は、実行される場合のみ効果がある**。

---

## 次のアクション候補

### A. 保守的アプローチ

1. **validate + confirm のバッチ化**
   - 既存フローを維持しつつ、待機時間削減
   - 削減: 4-6秒
   - リスク: 低

### B. 積極的アプローチ

1. **cached_matches 自動承認**
   - LLM判断を省略
   - 削減: 3-5秒（追加）
   - リスク: 中（自動承認の妥当性）

---

**Total削減見込み（Step 5実行時）**: 4-11秒（15-22秒 → 6-13秒）

**ただし、今回の実測ではStep 5は未実行のため、実際の効果は不明。**

次: Step 6以降の分析へ

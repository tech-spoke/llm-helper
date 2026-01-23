# Step 8: IMPACT_ANALYSIS フェーズの分析

## 実測データ

**時刻**: 16:04:35 → 16:04:53（約18秒）
**ツール呼び出し**: 2回

| # | ツール | 時刻 | 所要時間 | 説明 |
|---|--------|------|----------|------|
| 12 | analyze_impact | 16:04:45 | < 1秒 | 影響分析実行 |
| 13 | submit_impact_analysis | 16:04:53 | < 0.1秒 | 影響確認完了 |

---

## タイムライン

```
16:04:35  submit_verification 完了
  ↓
  ↓ [10秒: LLM思考]
  ↓   - VERIFICATION結果を確認
  ↓   - 次のステップ（IMPACT_ANALYSIS）を理解
  ↓   - analyze_impact 呼び出し準備
  ↓
16:04:45  analyze_impact 開始
  ↓
  ↓ [< 1秒: 影響分析実行]
  ↓   - 変更対象ファイルの依存関係解析
  ↓   - must_verify ファイルリスト生成
  ↓   - 影響スコープ算出
  ↓
16:04:45  analyze_impact 完了
  ↓
  ↓ [7秒: LLM思考]
  ↓   - must_verify リストを確認
  ↓   - 影響範囲を理解
  ↓   - submit_impact_analysis 準備
  ↓
16:04:53  submit_impact_analysis
  ↓
  ↓ [< 0.1秒: 影響確認完了]
  ↓
16:04:53  IMPACT_ANALYSIS完了 → READYへ
```

---

## 内訳

| 処理 | 所要時間 | 割合 |
|------|----------|------|
| LLM思考（analyze前） | 10秒 | 56% |
| analyze_impact 実行 | < 1秒 | 5% |
| LLM思考（analyze後） | 7秒 | 39% |
| submit_impact_analysis 実行 | < 0.1秒 | < 1% |
| **合計** | **18秒** | **100%** |

---

## ボトルネック分析

### 1位: analyze前のLLM思考（10秒、56%）

**内容**:
- VERIFICATION完了後、次のステップを理解
- analyze_impact の呼び出し準備
- 特に複雑な判断は不要

**問題点**:
- **この待機は削減可能**（バッチ化）
- VERIFICATIONとIMPACT_ANALYSISを統合できれば削減

### 2位: analyze後のLLM思考（7秒、39%）

**内容**:
- must_verify リストを確認
- 各ファイルを検討：
  - will_modify: 変更予定
  - no_change_needed: 確認したが変更不要（reason必須）
  - not_affected: 影響なし（reason必須）
- verified_files 準備

**問題点**:
- **この判断は削減不可**（LLM判断が必要）
- 各ファイルの影響を個別に評価する必要がある

---

## 改善策

### ❌ analyze + submit のバッチ化は不可

**analyze と submit の間にLLM判断が必要**

現在のフロー：
```
analyze_impact
  ↓ must_verify: ["CartService.php", "ProductTest.php"]
  ↓ [7秒: LLM判断 - 必須]
  ↓   - CartService.php → will_modify
  ↓   - ProductTest.php → no_change_needed (reason: "Test uses mock data")
submit_impact_analysis
  verified_files: [...]
```

**理由**:
- analyze の結果（must_verify リスト）を見て、LLMが各ファイルを個別評価
- will_modify / no_change_needed / not_affected を判断
- status != will_modify の場合、reason記述が必要
- **この判断（7秒）は削減不可**

**削減**: なし（判断が必要なため）

---

### ⚡ 中優先度（10秒削減）

**VERIFICATION + IMPACT_ANALYSIS の統合**

現在のフロー：
```
submit_verification
  ↓ [10秒: LLM思考]
analyze_impact
  ↓ [< 1秒: 実行]
  ↓ [7秒: LLM思考]
submit_impact_analysis
```

改善案：
```
submit_verification_with_impact()
  ↓ VERIFICATION submit
  ↓ 自動で analyze_impact
  ↓ 自動で submit_impact_analysis
  ↓ 全結果返却
```

**削減**: 17秒（VERIFICATION後の待機10秒 + analyze後の待機7秒）

**ただし**:
- VERIFICATION自体はLLM判断が必要（30秒）
- submit_verification で仮説検証が完了してから impact実行
- 統合しても VERIFICATION の30秒は残る

---

## 削減見込み

| 施策 | 削減時間 | 実装難易度 | 優先度 | 備考 |
|------|----------|------------|--------|------|
| analyze + submit バッチ化 | - | - | ❌ | 判断が必要なため不可 |
| VERIFICATION + IMPACT統合 | 10秒 | 中 | ⚡ | analyze前の待機のみ削減可 |

**現在**: 18秒
**v1.7での改善**: なし（LLM判断が必要）
**v1.8検討（VERIFICATION+IMPACT統合）**: 8秒（10秒削減）

---

## バッチ化の実現可能性

### analyze + submit のバッチ化 → 不可

**不可能な理由**:
1. analyze の結果（must_verify リスト）を見て、LLMが個別判断が必要
2. 各ファイルについて:
   - will_modify: このファイルを変更する
   - no_change_needed: 確認したが変更不要（reason必須）
   - not_affected: 影響なし（reason必須）
3. **この判断は機械的にできない**（LLMの理解が必要）

**例**:
```json
must_verify: ["app/Services/CartService.php", "tests/Feature/ProductTest.php"]

LLMが判断:
- CartService.php → will_modify（price型変更の影響あり）
- ProductTest.php → no_change_needed（reason: "Test uses mock data, not affected"）
```

この判断に7秒かかる。

---

### VERIFICATION + IMPACT の統合

**可能な理由**:
1. submit_verification の後、必ず IMPACT_ANALYSIS が実行される
2. 順序は固定（VERIFICATION → IMPACT）

**実装案**:
```python
def submit_verification_with_impact(
    session_id: str,
    found_clues: list[dict]
) -> dict:
    """
    仮説検証を完了し、影響分析も自動実行。

    Returns:
        {
            "verification_result": {...},
            "impact_analysis": {...},
            "must_verify_files": [...],
            "next_step": "Call check_write_target to start READY phase"
        }
    """
    # VERIFICATION submit
    verification = submit_verification_internal(session_id, found_clues)

    # 自動で IMPACT_ANALYSIS
    impact = analyze_impact(session_id)
    submit_impact_analysis(session_id)

    return {
        "verification_result": verification,
        "impact_analysis": impact,
        "next_step": "Call check_write_target to start READY phase"
    }
```

**注意**:
- VERIFICATION前の30秒のLLM思考は削減不可
- 削減できるのはVERIFICATION→IMPACT間の待機のみ

---

## 推奨実装

### v1.7: バッチ化不可

**Step 8（IMPACT_ANALYSIS）はLLM判断が必要**:
- analyze_impact の結果を見て、各ファイルを個別評価
- will_modify / no_change_needed / not_affected を判断
- reason記述が必要
- **バッチ化不可**

**削減可能な部分**:
- analyze_impact 前の待機（10秒）のみ
- VERIFICATION + IMPACT 統合で削減可能

### v1.8以降: VERIFICATION + IMPACT 統合（検討）

- 実装難易度: 中
- 削減: 10秒（VERIFICATION → IMPACT間の待機のみ）
- リスク: 中（VERIFICATION処理の複雑化）
- **v1.8以降で検討**

---

## まとめ

**Step 8もLLM判断が必要**:
- analyze_impact の結果（must_verify リスト）を確認
- 各ファイルの影響を個別評価（will_modify / no_change_needed / not_affected）
- reason記述が必要な場合あり
- **analyze + submit のバッチ化は不可**

**v1.7での削減見込み**: なし（判断が必要なため）

**削減可能な部分**:
- VERIFICATION → IMPACT_ANALYSIS 間の待機（10秒）
  - v1.8以降で検討（VERIFICATION + IMPACT統合）

次: **Step 9以降**の分析へ

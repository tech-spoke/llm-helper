# Step 6-7: SEMANTIC & VERIFICATION フェーズの分析

## 実測データ

### Step 6: SEMANTIC

**時刻**: 16:03:53 → 16:04:05（約12秒）
**ツール呼び出し**: 2回（1回失敗リトライ）

| # | ツール | 時刻 | 所要時間 | 説明 |
|---|--------|------|----------|------|
| 9 | submit_semantic | 16:03:53 | < 0.1秒 | 失敗（reason不適切） |
| 10 | submit_semantic | 16:04:05 | < 0.1秒 | 成功 |

**内訳**:
- ツール実行: < 0.1秒
- LLM思考（1回目）: 13秒（submit_understanding → submit_semantic）
- LLM思考（リトライ）: 12秒（失敗 → 成功）
- **合計**: 約25秒

---

### Step 7: VERIFICATION

**時刻**: 16:04:05 → 16:04:35（約30秒）
**ツール呼び出し**: 1回

| # | ツール | 時刻 | 所要時間 | 説明 |
|---|--------|------|----------|------|
| 11 | submit_verification | 16:04:35 | < 0.1秒 | 仮説検証完了 |

**内訳**:
- ツール実行: < 0.1秒
- LLM思考: 約30秒（コード確認 + エビデンス収集）

---

## タイムライン

```
16:03:40  submit_understanding 完了（Step 4）
  ↓ [13秒: LLM思考]
  ↓   - EXPLORATION結果の評価が "low"
  ↓   - Step 5をスキップしてStep 6へ
  ↓   - 仮説（hypotheses）生成
  ↓   - semantic_reason 決定
  ↓   - search_queries 準備
  ↓
16:03:53  submit_semantic（1回目）
  ↓ [< 0.1秒: サーバー検証]
  ↓   - reason が不適切（エラー）
  ↓
  ↓ [12秒: LLM思考]
  ↓   - エラーメッセージを確認
  ↓   - semantic_reason を修正
  ↓   - 再度submit準備
  ↓
16:04:05  submit_semantic（2回目）成功
  ↓
  ↓ [30秒: LLM思考]
  ↓   - 仮説とコードを照合
  ↓   - エビデンス収集
  ↓   - found_clues 準備
  ↓   - submit_verification 準備
  ↓
16:04:35  submit_verification
  ↓
  ↓ [< 0.1秒: サーバー処理]
  ↓
16:04:35  VERIFICATION完了 → IMPACT_ANALYSISへ
```

---

## ボトルネック分析

### Step 6: SEMANTIC（25秒）

#### 内訳

| 処理 | 所要時間 | 割合 |
|------|----------|------|
| 1回目の準備 | 13秒 | 52% |
| 1回目submit実行 | < 0.1秒 | < 1% |
| リトライ準備 | 12秒 | 48% |
| 2回目submit実行 | < 0.1秒 | < 1% |
| **合計** | **25秒** | **100%** |

#### 1位: 仮説生成とsemantic_reason決定（13秒、52%）

**内容**:
- EXPLORATION結果から不足情報を特定
- 仮説（hypotheses）を生成
  - 例: "AuthService is called directly from Controller"
  - confidence付き
- semantic_reason を決定
  - no_similar_implementation / isolated_usage / edge_case 等
- search_queries 準備

**問題点**:
- **この思考は削減不可**（LLM判断が必要）
- 仮説生成は高度な推論

#### 2位: リトライの準備（12秒、48%）

**内容**:
- サーバーエラー（reason不適切）を理解
- semantic_reason を修正
- 再度submit

**問題点**:
- **エラーがなければ不要**
- reason の検証を事前に強化すれば回避可能

---

### Step 7: VERIFICATION（30秒）

#### 内訳

| 処理 | 所要時間 | 割合 |
|------|----------|------|
| LLM思考（仮説検証） | 30秒 | 100% |
| submit実行 | < 0.1秒 | < 1% |
| **合計** | **30秒** | **100%** |

#### 唯一のボトルネック: 仮説とコードの照合（30秒、100%）

**内容**:
- SEMANTIC で生成した仮説をコードで検証
- エビデンス（found_clues）収集
  - コード断片
  - ファイルパス
  - 行番号
- 検証結果のまとめ

**問題点**:
- **この思考は削減不可**（LLM判断が必要）
- 仮説検証は慎重な確認が必要

---

## 改善策

### ❌ バッチ化は不可（両フェーズとも）

#### Step 6: SEMANTIC

```
submit_semantic
  ↓ [13秒: LLM思考 - 必須]
  ↓   - 仮説生成
  ↓   - semantic_reason決定
```

**単一ツール呼び出し**のため、バッチ化の余地なし。

#### Step 7: VERIFICATION

```
submit_verification
  ↓ [30秒: LLM思考 - 必須]
  ↓   - 仮説とコードの照合
  ↓   - エビデンス収集
```

**単一ツール呼び出し**のため、バッチ化の余地なし。

---

### ⚡ 中優先度（エラー削減）

**semantic_reason のバリデーション強化**

現在のフロー:
```
submit_semantic（reason: X）
  ↓ サーバーがエラー返却
  ↓ [12秒: LLM思考]
submit_semantic（reason: Y）成功
```

改善案:
```
code.md で semantic_reason の選択肢を明示
  → LLMが適切な reason を最初から選択
  → リトライ不要
```

**削減**: 12秒（リトライ時のみ）

---

### 💡 低優先度

**SEMANTIC フェーズのスキップ条件最適化**

現状:
- EXPLORATION評価が "low" → SEMANTIC実行
- Step 5をスキップした場合 → SEMANTIC実行

改善:
- 評価基準の見直し
- "medium" でも SEMANTIC スキップ可能か検討

**削減**: 25秒（SEMANTIC全体スキップ時）
**リスク**: 高（情報不足のまま実装に進む可能性）

---

## 削減見込み

| 施策 | 削減時間 | 実装難易度 | 優先度 | 備考 |
|------|----------|------------|--------|------|
| SEMANTIC バッチ化 | - | - | ❌ | 単一ツール、判断必要 |
| VERIFICATION バッチ化 | - | - | ❌ | 単一ツール、判断必要 |
| semantic_reason バリデーション強化 | 12秒 | 低 | ⚡ | リトライ発生時のみ |
| SEMANTIC スキップ条件最適化 | 25秒 | 高 | 💡 | リスク高 |

**現在**: 55秒（SEMANTIC 25秒 + VERIFICATION 30秒）
**v1.7での改善**: なし（両フェーズともLLM判断が必要）

---

## 重要な発見

### Step 6-7 は高度な推論フェーズ

1. **SEMANTIC**: 不足情報から仮説を生成
2. **VERIFICATION**: 仮説をコードで検証

**この2フェーズは本質的にLLMの高度な推論が必要**:
- バッチ化不可
- 並列化不可
- 削減余地ほぼなし

### バッチ化が有効なのは「判断不要」なフェーズのみ

| フェーズ | 判断必要 | バッチ化 |
|---------|---------|---------|
| 準備（Step 2-3.5） | ❌ | ✅ 可能 |
| EXPLORATION（Step 4） | ⚡ 部分的 | ✅ 可能（search_text複数） |
| Symbol Validation（Step 5） | ✅ | ❌ 不可 |
| SEMANTIC（Step 6） | ✅ | ❌ 不可 |
| VERIFICATION（Step 7） | ✅ | ❌ 不可 |

---

## 次のアクション

Step 6-7 は最適化対象外（LLM判断が必須）。

次: **Step 8（IMPACT_ANALYSIS）** の分析へ
- 2つのツール（analyze_impact + submit_impact_analysis）
- バッチ化の余地あり（⭐）

# PRE_COMMIT と QUALITY_REVIEW の順序問題

## 現状の問題

### 現在のフロー

```
Step 10: PRE_COMMIT
  ├─ review_changes（ゴミ検出）
  ├─ finalize_changes（keep/discard判断 + コミット実行）★ ここでコミット
  └─ コミット完了

Step 10.5: QUALITY_REVIEW
  ├─ quality_review.md に基づいて品質チェック
  │   - Unused imports / dead code
  │   - CLAUDE.md rules 遵守
  │   - Security (hardcoded secrets)
  │   - Performance (N+1 queries)
  └─ 問題発見時
      → READY に戻る
      → 修正
      → POST_IMPL_VERIFY → PRE_COMMIT（新しいコミット）→ QUALITY_REVIEW
```

### 問題点

1. **コミット後に品質チェック**
   - finalize_changes でコミット実行
   - その後 QUALITY_REVIEW で品質問題を発見
   - 既にコミット済み

2. **リワーク時のコミット増加**
   - 品質問題発見 → READY に戻る
   - 修正後、PRE_COMMIT を再通過
   - **新しいコミットが作成される**（修正コミット）
   - 本来は1コミットで済むべき

3. **非効率なフロー**
   - コミット前に品質チェックすれば、1回で完了
   - 現状は「コミット → チェック → 問題あり → 別コミット」

---

## 理想的なフロー

### 提案: コミット前に品質チェック

```
Step 10: PRE_COMMIT（名前変更: PRE_COMMIT_REVIEW）
  ├─ review_changes（ゴミ検出）
  │   - debug.log, console.log 検出
  │   - 不要ファイル検出
  │   - keep/discard 判断（コミットはまだしない）
  │
  ├─ quality_review（品質チェック）
  │   - Unused imports / dead code
  │   - CLAUDE.md rules 遵守
  │   - Security / Performance
  │
  ├─ 問題あり？
  │   YES → READY に戻る
  │   NO  → finalize_changes（コミット実行）
  │
  └─ finalize_changes（コミット）
      → 問題なければ1回だけコミット
```

### メリット

1. **コミット数の削減**
   - 品質問題があっても、コミット前に検出
   - 修正後、1回だけコミット

2. **効率的なフロー**
   - ゴミ検出 → 品質チェック → コミット
   - 論理的な順序

3. **Git履歴のクリーン化**
   - 「修正コミット」が不要
   - 1タスク = 1コミット

---

## 実装の影響範囲

### 変更が必要なファイル

1. **code_intel_server.py**
   - `finalize_changes` をコミット実行からコミット準備に変更
   - `submit_quality_review` の後にコミット実行を移動
   - Phase 遷移ロジックの変更

2. **.claude/commands/code.md**
   - Step 10 の説明を更新
   - Step 10.5 を Step 10 に統合
   - 新しいフロー図を追加

3. **tools/session.py**
   - `finalize_changes` の実装変更
   - コミットタイミングの変更

### リスク

| リスク | 影響 | 対策 |
|--------|------|------|
| フロー変更による既存セッションの互換性 | 中 | Phase 移行期にバージョンチェック |
| quality_review.md がない場合の処理 | 低 | 既存のスキップロジックを維持 |
| --no-quality フラグの挙動 | 低 | フラグ時は品質チェックスキップ |

---

## 実装案

### Phase 1: 最小限の変更（v1.8候補）

**Step 10 の再構成:**

```python
# 現在
def finalize_changes(...):
    # keep/discard 判断
    # → コミット実行
    return {"commit_hash": "..."}

# 提案
def prepare_commit(...):  # 名前変更
    # keep/discard 判断
    # → コミット準備（実行はしない）
    return {"prepared": True, "kept_files": [...]}

def submit_quality_review(...):
    # 品質チェック
    if issues_found:
        # READY に戻る（コミット未実行）
        return {"phase": "READY", "issues": [...]}
    else:
        # 問題なし → コミット実行
        commit_hash = _execute_commit()
        return {"commit_hash": commit_hash, "next": "merge_to_base"}
```

**フロー変更:**

```
現在:
review_changes → finalize_changes（コミット）→ submit_quality_review

提案:
review_changes → prepare_commit（準備）→ submit_quality_review（チェック + コミット）
```

---

### Phase 2: ツール統合（v1.9候補）

**統合ツール: `pre_commit_review_and_commit`**

```python
def pre_commit_review_and_commit(
    reviewed_files: list[dict],  # keep/discard 判断
    commit_message: str,
    skip_quality: bool = False
) -> dict:
    """
    PRE_COMMIT と QUALITY_REVIEW を統合。

    フロー:
    1. ゴミ検出（reviewed_files を適用）
    2. 品質チェック（skip_quality=False の場合）
    3. 問題なければコミット

    Returns:
        問題なし: {"commit_hash": "...", "next": "merge_to_base"}
        問題あり: {"phase": "READY", "issues": [...]}
    """
    # 1. keep/discard 適用
    _apply_reviewed_files(reviewed_files)

    # 2. 品質チェック
    if not skip_quality:
        issues = _run_quality_review()
        if issues:
            return {"phase": "READY", "issues": issues}

    # 3. コミット
    commit_hash = _execute_commit(commit_message)
    return {"commit_hash": commit_hash, "next": "merge_to_base"}
```

**削減見込み:**
- LLM往復: finalize_changes → submit_quality_review 間（2-3秒）
- **ただし、v1.7 の優先度は低い**（フロー変更のリスク）

---

## 実測データ分析

### session_20260123_160021

```
16:06:33  review_changes 開始
  ↓ [16秒: LLM思考]
  ↓   - 各ファイルを確認
  ↓   - keep/discard 判断
  ↓   - commit_message 作成
16:06:49  finalize_changes（コミット実行）
  ↓ [< 1秒: Git commit]
  ↓
  ↓ [25秒: LLM思考]
  ↓   - コミット完了を確認
  ↓   - quality_review.md を読む
  ↓   - 品質チェック実施
16:07:14  submit_quality_review（問題なし）
  ↓ [< 0.1秒: サーバー処理]
  ↓
  ↓ [11秒: LLM思考]
16:07:25  merge_to_base
```

### 内訳

| 処理 | 所要時間 | 割合 |
|------|----------|------|
| review_changes 実行 | < 1秒 | 2% |
| finalize_changes 実行 | < 1秒 | 2% |
| submit_quality_review 実行 | < 0.1秒 | < 1% |
| LLM思考（review前） | 16秒 | 31% |
| LLM思考（finalize後） | 25秒 | 48% |
| LLM思考（quality後） | 11秒 | 21% |
| **合計** | **52秒** | **100%** |

### ボトルネック

1. **finalize_changes 後の待機（25秒、48%）**
   - コミット完了確認
   - quality_review.md 読み込み
   - 品質チェック実施
   - **この待機は削減可能**（統合すれば削減）

2. **review_changes 前の待機（16秒、31%）**
   - 各ファイルの keep/discard 判断
   - **この思考は削減不可**（判断が必要）

---

## 削減見込み

### Phase 1（順序変更のみ）

**削減**: なし（LLM思考時間は同じ）
**メリット**: コミット数削減、Git履歴クリーン化

### Phase 2（ツール統合）

**削減**: 2-3秒（finalize → quality 間の待機）
**メリット**: 削減 + コミット数削減

---

## 推奨実装

### v1.7: 順序変更を実装（決定）

**優先度**: 高（v1.7で実装）
**削減**: 2-3秒（finalize → quality 間の待機）
**実装時間**: 3-4時間

**実装内容**:
1. finalize_changes: コミット準備のみ（実行はしない）
2. submit_quality_review: 品質チェック後、問題なければコミット実行

**メリット**:
- Git履歴のクリーン化（1タスク = 1コミット）
- 論理的なフロー順序
- リワーク時のコミット削減

### v1.8以降: さらなる統合（検討）

**優先度**: 低
**削減**: なし（v1.7で順序変更済み）
**実装時間**: 2-3時間

**内容**: ツールの完全統合（必要に応じて）

---

## まとめ

### 現状の問題

- **コミット後に品質チェック** → リワーク時にコミット増加
- **review_changes（ゴミ検出）** と **quality_review（品質チェック）** は別の目的
- 理想的な順序: ゴミ検出 → 品質チェック → コミット

### v1.7 での扱い

**実装しない** - リスクと削減効果のバランスから、v1.8 以降で検討

### v1.8 以降の検討事項

1. Phase 1（順序変更）: Git履歴クリーン化
2. Phase 2（ツール統合）: 2-3秒削減

次: v1.7 の最終まとめと削減見込みの集計

# Ctags Cache Test Plan

テスト対象: Phase 1（セッションキャッシュ）とPhase 2（永続キャッシュ）

## 前提条件
- Claude Codeを再起動済み
- sample/calculator.py が存在
- sample/.code-intel/ が初期化済み

---

## Test 1: 基本動作テスト

**目的:** find_definitionsが正常に動作することを確認

**手順:**
```
cd sample
/code Calculatorクラスの実装を確認
```

**期待結果:**
- Calculatorクラスの定義が見つかる
- エラーが発生しない
- 結果に `cache_hit: false` が含まれる（初回なのでキャッシュミス）
- 結果に `cache_stats` が含まれる

---

## Test 2: Phase 1（セッションキャッシュ）テスト

**目的:** 同一セッション内でキャッシュが効くことを確認

**手順:**
```
# Test 1に続けて、同じセッション内で実行
/code calculate_sum関数の実装を確認
```

**期待結果:**
- calculator.pyが再度スキャンされる際、**セッション内では同じシンボルが再検索されていないため、このテストではキャッシュヒットが見えない可能性がある**
- より良いテスト: 同じシンボル（Calculator）を含むクエリを再実行

**修正した手順:**
```
# Test 1の直後、同じセッション内で
/code Calculatorクラスのaddメソッドを確認
```

**期待結果:**
- `cache_hit: true` が含まれる（セッションキャッシュヒット）
- `cache_stats.hits: 1` 以上
- find_definitionsが高速に完了（ctagsを実行しない）

---

## Test 3: Phase 2（永続キャッシュ）テスト

**目的:** セッションをまたいでファイルレベルキャッシュが効くことを確認

**手順:**
```
# 1. 一度Test 1を実行してキャッシュを構築
# 2. Claude Codeを終了
# 3. Claude Codeを再起動（新しいセッション）
# 4. 再度実行
cd sample
/code Calculatorクラスのmultiplyメソッドを確認
```

**期待結果:**
- `files_cached: 1` 以上（calculator.pyがキャッシュから読み込まれた）
- `persistent_cache_enabled: true`
- `.code-intel/ctags_cache/cache_index.json` ファイルが存在
- キャッシュファイルの内容:
```json
{
  "calculator.py": {
    "file_path": "calculator.py",
    "hash": "...",
    "tags": [...],
    "cached_at": "...",
    "language": null
  }
}
```

**確認コマンド:**
```bash
ls -la sample/.code-intel/ctags_cache/
cat sample/.code-intel/ctags_cache/cache_index.json | python3 -m json.tool
```

---

## Test 4: キャッシュ無効化テスト

**目的:** ファイル変更時にキャッシュが自動無効化されることを確認

**手順:**
```bash
# 1. calculator.pyを編集（コメント追加など）
echo "# Test comment" >> sample/calculator.py

# 2. 再度検索
cd sample
/code Calculatorクラスを確認
```

**期待結果:**
- `files_cached: 0`（キャッシュが無効化された）
- 新しいSHA256ハッシュで再キャッシュされる
- cache_index.jsonのハッシュ値が更新される

**確認コマンド:**
```bash
cat sample/.code-intel/ctags_cache/cache_index.json | python3 -m json.tool | grep hash
```

---

## Test 5: 統合テスト（オプション）

**目的:** 両フェーズが協調して動作することを確認

**手順:**
1. 新規セッションで初回実行（Phase 2でキャッシュ構築）
2. 同じセッション内で再実行（Phase 1でセッションキャッシュヒット）
3. 新規セッション（Phase 2で永続キャッシュヒット + Phase 1で新規セッションキャッシュ構築）

**期待結果:**
- 初回: cache_hit=false, files_cached=0
- 2回目: cache_hit=true（セッション）, files_cached=1（永続）
- 3回目（新セッション）: cache_hit=false（セッションリセット）, files_cached=1（永続）
- 3回目の2回目: cache_hit=true（セッション）, files_cached=1（永続）

---

## 確認コマンドまとめ

```bash
# キャッシュディレクトリの存在確認
ls -la sample/.code-intel/ctags_cache/

# キャッシュ内容の確認
cat sample/.code-intel/ctags_cache/cache_index.json | python3 -m json.tool

# タグ数の確認
cat sample/.code-intel/ctags_cache/cache_index.json | python3 -c "import json, sys; data=json.load(sys.stdin); print(f'Files cached: {len(data)}'); print(f'Total tags: {sum(len(v[\"tags\"]) for v in data.values())}')"

# キャッシュのクリア（テストやり直し用）
rm -rf sample/.code-intel/ctags_cache/
rm -rf sample/.code-intel/chroma/
rm -rf sample/.code-intel/sync_state.json
```

---

## 成功基準

- ✅ Test 1: エラーなく動作
- ✅ Test 2: cache_hit=true でセッションキャッシュヒット
- ✅ Test 3: files_cached > 0 で永続キャッシュヒット
- ✅ Test 4: ファイル変更後にキャッシュ無効化
- ✅ Test 5: 両フェーズが協調動作

すべて成功すれば、2-10倍の高速化が実現できています。

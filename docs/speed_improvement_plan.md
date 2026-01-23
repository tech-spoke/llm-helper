# Speed Improvement Plan: Cursor並みの高速化

## 現状分析（10分 → 30秒へ）

### ボトルネック特定

現在の処理時間内訳：
- **find_definitions**: 2-3分（全ファイル走査）
- **find_references**: 2-3分（ripgrepフルスキャン）
- **semantic_search**: 2-3分（ChromaDB検索）
- **analyze_impact**: 1-2分

**合計**: 7-11分

### 根本原因

#### 1. ディレクトリスキャン時の全ファイル走査
```python
# tools/ctags_tool.py:176
for ext in extensions:
    for file_path in search_path.rglob(f"*{ext}"):  # O(全ファイル数)
        tags = await _scan_file_with_cache(file_path, ...)
```

**問題点**:
- 1000ファイルのプロジェクトで1000回ファイルアクセス
- キャッシュがあっても、全ファイルパスを走査してキャッシュチェック
- O(n) where n = 全ファイル数

#### 2. シンボル名インデックスの不在
```python
# tools/ctags_tool.py:198-208
# 全タグを取得後にフィルタリング
for tag in all_tags:  # 全ファイルの全タグ
    if symbol.lower() in tag_name.lower():
        definitions.append(...)
```

**問題点**:
- シンボル名 → ファイルの逆引きインデックスなし
- 毎回全タグをメモリに読み込んでフィルタリング

#### 3. 並列処理の未実装
- ファイルスキャンが順次実行
- I/O待ちで時間を無駄にしている

---

## 改善プラン

### Phase 1: シンボルインデックスの実装（最優先）

**目標**: シンボル名から直接ファイルを引けるようにする

#### 1.1 インデックスデータ構造
```python
# .code-intel/ctags_cache/symbol_index.json
{
  "AuthService": {
    "files": ["app/Services/AuthService.php"],
    "kind": "class",
    "language": "PHP"
  },
  "login": {
    "files": ["app/Services/AuthService.php", "routes/web.php"],
    "kind": "method",
    "language": "PHP"
  },
  "_index_hash": "abc123",  # プロジェクト全体のハッシュ
  "_last_updated": "2026-01-23T10:00:00"
}
```

#### 1.2 検索アルゴリズム変更
```python
# Before: O(全ファイル数)
for file_path in search_path.rglob("*.py"):
    tags = scan_file(file_path)
    if symbol in tags:
        results.append(...)

# After: O(1) or O(log n)
symbol_entry = symbol_index.get(symbol)  # O(1)
for file_path in symbol_entry["files"]:   # O(該当ファイル数のみ)
    tags = get_cached_tags(file_path)
    results.append(...)
```

**期待効果**: 2-3分 → 0.5-1秒（95%削減）

---

### Phase 2: 並列処理の実装

#### 2.1 ファイルスキャンの並列化
```python
# tools/ctags_tool.py
import asyncio

# Before: 順次実行
for file_path in files:
    tags = await scan_file(file_path)

# After: 並列実行（最大10並列）
semaphore = asyncio.Semaphore(10)
tasks = [scan_file_parallel(f, semaphore) for f in files]
results = await asyncio.gather(*tasks)
```

**期待効果**: さらに50%削減（I/O待ち時間の削減）

---

### Phase 3: 増分更新の実装

#### 3.1 ファイル変更検知
```python
# Watchdog or inotify for file monitoring
from watchdog.observers import Observer

class CtagsIndexWatcher:
    def on_modified(self, event):
        # 変更されたファイルのみ再インデックス
        file_path = Path(event.src_path)
        self.update_file_index(file_path)
```

#### 3.2 プロジェクト全体ハッシュ
```python
# Git commit hashを利用
def get_project_hash():
    result = subprocess.run(["git", "rev-parse", "HEAD"], ...)
    return result.stdout.strip()
```

**期待効果**: 2回目以降のスキャンがほぼゼロ秒

---

### Phase 4: find_referencesの高速化

#### 4.1 ripgrepインデックスの作成
```python
# .code-intel/ripgrep_cache/references_index.json
{
  "AuthService": {
    "references": [
      {"file": "app/Http/Controllers/LoginController.php", "line": 25},
      {"file": "tests/Feature/AuthTest.php", "line": 15}
    ],
    "last_updated": "2026-01-23T10:00:00"
  }
}
```

#### 4.2 差分更新
- Gitの変更ファイルのみripgrepを再実行
- 未変更ファイルはキャッシュから取得

**期待効果**: 2-3分 → 0.5秒

---

## 実装優先度

### 🔥 Phase 1: シンボルインデックス（最優先）
- **実装時間**: 3-4時間
- **効果**: 95%削減
- **影響範囲**: `tools/ctags_tool.py`, `tools/ctags_cache.py`
- **破壊的変更**: なし（既存キャッシュとは別）

### 🔥 Phase 2: 並列処理
- **実装時間**: 1-2時間
- **効果**: さらに50%削減
- **影響範囲**: `tools/ctags_tool.py`
- **破壊的変更**: なし

### ⚡ Phase 3: 増分更新
- **実装時間**: 2-3時間
- **効果**: 2回目以降ほぼゼロ秒
- **影響範囲**: 新規ファイル追加
- **破壊的変更**: なし

### ⚡ Phase 4: find_references高速化
- **実装時間**: 2-3時間
- **効果**: 90%削減
- **影響範囲**: `tools/ripgrep_tool.py`
- **破壊的変更**: なし

---

## 期待される最終結果

### Before（現状）
```
合計: 7-11分
├─ find_definitions: 2-3分
├─ find_references: 2-3分
├─ semantic_search: 2-3分
└─ analyze_impact: 1-2分
```

### After（Phase 1-2実装後）
```
合計: 30秒 - 1分
├─ find_definitions: 0.5秒（シンボルインデックス + 並列）
├─ find_references: 0.5秒（参照インデックス）
├─ semantic_search: 2-3分（別途改善必要）
└─ analyze_impact: 10秒（依存ファイル減少）
```

### After（Phase 3実装後、2回目以降）
```
合計: 10-30秒
├─ find_definitions: 0.1秒（インデックスヒット）
├─ find_references: 0.1秒（インデックスヒット）
├─ semantic_search: 2-3秒（ChromaDB改善必要）
└─ analyze_impact: 5秒
```

---

## 実装戦略

### 段階的ロールアウト
1. **Phase 1実装 → テスト**（他プロジェクトに影響なし）
2. **Phase 2実装 → テスト**
3. **デフォルト有効化**（`use_symbol_index=True`）
4. **Phase 3-4実装**

### フラグによる制御
```python
# code_intel_server.py
async def find_definitions(..., use_symbol_index: bool = True):
    if use_symbol_index and symbol_index.exists():
        return await find_definitions_indexed(...)
    else:
        return await find_definitions_legacy(...)  # 既存実装
```

### 後方互換性
- 既存の`CtagsCacheManager`はそのまま維持
- シンボルインデックスは新規追加
- フラグで切り替え可能

---

## リスク管理

### 考慮事項
1. **インデックスサイズ**: 大規模プロジェクトでインデックスが肥大化する可能性
2. **インデックス更新**: ファイル変更時の更新漏れ
3. **競合状態**: 並列更新時の整合性

### 対策
1. インデックスの圧縮 + 定期的なクリーンアップ
2. Git hookによる自動更新
3. ロックファイルによる排他制御

---

## 次のステップ

1. ✅ このプランをレビュー
2. Phase 1の詳細設計
3. プロトタイプ実装（小規模プロジェクトでテスト）
4. 本実装 + テストケース追加
5. ドキュメント更新

---

## 参考

- Cursor: 事前インデックス + 増分更新
- ripgrep: `--json`フォーマットでパース可能
- ctags: `--output-format=json`でパース可能
- asyncio: `asyncio.gather()`で並列実行

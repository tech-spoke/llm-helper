# /exp - 高速コード探索コマンド

**目的**: 並列検索を活用した軽量な探索・調査

## 特徴

- **並列実行**: search_text、Read、Grep を自動的に並列実行
- **軽量**: 実装はせず、探索と理解に特化
- **高速**: 並列実行により通常の探索より20-30秒高速

## 使い方

探索したい内容を自然言語で指定するだけ：

```
/exp Find all authentication related code
/exp Understand how the modal system works
/exp List all API endpoints
```

## 実行プロセス

### 1. タスク理解
ユーザーの探索目的を分析

### 2. 並列検索の実行

**⚠️ CRITICAL: 必ず並列実行を使用**

#### search_text (複数パターン)
探索に必要なパターンを特定し、**1回の呼び出し**で並列検索：

✅ **正しい方法**:
```
mcp__code-intel__search_text で ["pattern1", "pattern2", "pattern3"] を検索
```

❌ **間違った方法**:
```
search_text("pattern1")
<!-- 待機 -->
search_text("pattern2")
<!-- 待機 -->
search_text("pattern3")
```

#### Read (複数ファイル)
関連ファイルを**1メッセージ**で並列読み込み：

✅ **正しい方法**:
```xml
<Read file_path="file1.py" />
<Read file_path="file2.py" />
<Read file_path="file3.py" />
```

#### Grep (複数パターン)
複数パターンを**1メッセージ**で並列検索：

✅ **正しい方法**:
```xml
<Grep pattern="class.*Service" />
<Grep pattern="function.*handler" />
<Grep pattern="async def" />
```

### 3. 結果の整理と報告

探索結果を整理して以下の形式で報告：

```markdown
## 探索結果

### 発見したファイル
- file1.py: 役割の説明
- file2.py: 役割の説明

### 主要なパターン
- パターン1: 説明
- パターン2: 説明

### アーキテクチャ
簡潔な説明

### 次のステップ（オプション）
推奨される調査方向
```

## 並列実行の原則

**同じツールを複数回使う場合は必ず1メッセージ/1呼び出しにまとめる**

| ツール | 並列化方法 | 時間削減 |
|--------|----------|---------|
| search_text | 配列でパターンを渡す | 15-20秒 |
| Read | 1メッセージで複数呼び出し | 4-6秒 |
| Grep | 1メッセージで複数呼び出し | 2-4秒 |

## 使用例

### 例1: 認証機能の理解
```
/exp Find all authentication related code
```

実行内容：
1. search_text(["auth", "login", "session", "token", "password"])
2. 発見したファイルを並列Read
3. 構造を分析して報告

### 例2: API エンドポイントのリスト
```
/exp List all API endpoints
```

実行内容：
1. Glob("**/*controller*.py"), Glob("**/*route*.py"), Glob("**/*api*.py")
2. 発見したファイルを並列Read
3. エンドポイント一覧を抽出して報告

### 例3: モーダルシステムの理解
```
/exp Understand how the modal system works
```

実行内容：
1. search_text(["modal", "dialog", "popup", "overlay"])
2. 関連ファイルを並列Read
3. システムの仕組みを説明

## 禁止事項

- ❌ Edit/Write/Bash は使用不可（探索のみ）
- ❌ 実装作業は不可
- ❌ 順次実行（必ず並列実行）
- ❌ git 操作は不可

## `/code` との違い

| 項目 | /exp | /code |
|------|------|-------|
| 目的 | 探索・理解 | 実装 |
| 実装 | ❌ | ✅ |
| フェーズゲート | なし | あり |
| 所要時間 | 30-90秒 | 402秒 |
| 並列実行 | 必須 | 自動 |
| git 操作 | なし | あり |

## 重要な注意

**このコマンドは並列実行を前提としています。**

- search_text は必ず複数パターンを配列で渡す
- Read/Grep は必ず1メッセージで複数呼び出し
- 順次実行すると時間削減効果がなくなります

詳細は `.claude/README.md` の並列実行ガイドを参照してください。

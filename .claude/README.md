# Claude Code 効率化ガイド

## ⚡ 並列実行による時間短縮（v1.7）

Claude Codeは複数のツール呼び出しを1メッセージ内で並列実行できます。これにより大幅な時間短縮が可能です。

### 基本原則

**同じツールを複数回呼び出す場合は、必ず1メッセージ内でまとめて呼び出す**

### 効果的なパターン

#### 1. 複数ファイルの読み込み

❌ **遅い方法** (順次実行):
```xml
<Read file_path="file1.py" />
<!-- 待機 -->
<Read file_path="file2.py" />
<!-- 待機 -->
<Read file_path="file3.py" />
```

✅ **速い方法** (並列実行):
```xml
<Read file_path="file1.py" />
<Read file_path="file2.py" />
<Read file_path="file3.py" />
```

**削減時間**: 4-6秒

#### 2. 複数パターンの検索（Grep）

❌ **遅い方法**:
```xml
<Grep pattern="class.*Service" />
<!-- 待機 -->
<Grep pattern="function.*calculate" />
<!-- 待機 -->
<Grep pattern="interface.*Repository" />
```

✅ **速い方法**:
```xml
<Grep pattern="class.*Service" />
<Grep pattern="function.*calculate" />
<Grep pattern="interface.*Repository" />
```

**削減時間**: 2-4秒

#### 3. 複数パターンのテキスト検索（search_text、v1.7新機能）

❌ **遅い方法**:
```
mcp__code-intel__search_text で "modal" を検索
<!-- 待機 -->
mcp__code-intel__search_text で "dialog" を検索
<!-- 待機 -->
mcp__code-intel__search_text で "popup" を検索
```

✅ **速い方法**:
```
mcp__code-intel__search_text で ["modal", "dialog", "popup"] を検索
```

**削減時間**: 15-20秒

#### 4. ドキュメント整備

❌ **遅い方法**:
```xml
<Read file_path="README.md" />
<!-- 内容確認 -->
<Edit file_path="README.md" ... />
<!-- 待機 -->
<Read file_path="CHANGELOG.md" />
<!-- 内容確認 -->
<Edit file_path="CHANGELOG.md" ... />
```

✅ **速い方法**:
```xml
<!-- 最初に必要なファイルを全て読む -->
<Read file_path="README.md" />
<Read file_path="CHANGELOG.md" />
<Read file_path="docs/guide.md" />
<!-- 内容確認後、編集も並列実行 -->
<Edit file_path="README.md" ... />
<Edit file_path="CHANGELOG.md" ... />
<Edit file_path="docs/guide.md" ... />
```

**削減時間**: 5-10秒

### 適用可能なツール

以下のツールは並列実行が効果的です:

| ツール | 並列実行 | 効果 |
|--------|---------|------|
| Read | ✅ | 4-6秒/ファイル |
| Grep | ✅ | 2-3秒/パターン |
| Glob | ✅ | 1-2秒/パターン |
| search_text (v1.7) | ✅ | 複数パターンを配列で渡す |
| Edit | ✅ | 2-3秒/ファイル |
| Write | ✅ | 2-3秒/ファイル |
| Bash | ❌ | 依存関係があるため順次実行 |

### 使用例

#### コードレビュー作業
```xml
<!-- 関連ファイルを全て並列読み込み -->
<Read file_path="src/auth/service.py" />
<Read file_path="src/auth/controller.py" />
<Read file_path="tests/test_auth.py" />
<Read file_path="docs/auth_design.md" />

<!-- 分析後、複数ファイルを並列更新 -->
<Edit file_path="src/auth/service.py" ... />
<Edit file_path="tests/test_auth.py" ... />
```

#### ドキュメント一括更新
```xml
<!-- ドキュメントを並列読み込み -->
<Read file_path="README.md" />
<Read file_path="README_ja.md" />
<Read file_path="docs/api.md" />

<!-- 内容確認後、並列更新 -->
<Edit file_path="README.md" ... />
<Edit file_path="README_ja.md" ... />
<Edit file_path="docs/api.md" ... />
```

### 総削減時間の例

典型的な `/code` タスク (402秒):
- EXPLORATION: search_text並列化で **20秒削減**
- READY: Read/Grep並列化で **5-10秒削減**
- その他フェーズ: **2-5秒削減**

**合計削減**: 27-35秒 (約7-9%)

### 注意事項

1. **依存関係がある場合は順次実行**
   - 例: ファイル作成後にそのファイルを読む場合
   - Bashコマンドは `&&` で連結して順次実行

2. **search_textの制限**
   - 最大5パターンまで
   - それ以上は複数回に分割

3. **トランケート対策**
   - 大量のデータを並列取得する場合、30,000文字制限に注意
   - search_textはパターン数を5個に制限することで対処

## 参考資料

- [v1.7 更新内容](../docs/updates/v1.7_ja.md)
- [/code コマンド](commands/code.md) - Step 4, Step 9の並列実行セクション
- [/test-parallel コマンド](commands/test-parallel.md) - 並列実行検証ツール

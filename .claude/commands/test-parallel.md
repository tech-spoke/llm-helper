# /test-parallel - 並列実行検証コマンド

**目的**: LLMが並列実行指示に従うかを短時間で検証

## 実行フェーズ（約60-90秒）

このコマンドは、code-intel MCPサーバーの並列実行機能を検証するための軽量テストです。

### 1. EXPLORATION検証（30秒）

**タスク**: 「Find modal implementation patterns in the codebase」

**手順**:
1. まず`search_text`を複数パターンで1回呼び出し
   - ✅ 正しい: `search_text(patterns=["modal", "dialog", "popup"])`
   - ❌ 間違い: `search_text("modal")` → 待機 → `search_text("dialog")` → 待機

2. 結果を簡潔に報告

### 2. READY検証（30秒）

**タスク**: 「List all Python files in tools/ directory」

**手順**:
1. 複数Readを1メッセージで呼び出し
   - ✅ 正しい: 1メッセージで Read("tools/a.py"), Read("tools/b.py"), Read("tools/c.py")
   - ❌ 間違い: Read → 待機 → Read → 待機

2. ファイルリストを簡潔に報告

### 3. 結果レポート出力

以下の形式でレポートを出力:

```markdown
## 並列実行検証レポート

### EXPLORATION検証
- search_text呼び出し回数: X回
- 並列実行: ✅/❌
- 所要時間: X秒

### READY検証
- Read呼び出し回数: X回
- 並列実行: ✅/❌
- 所要時間: X秒

### 総合評価
- 並列実行成功: ✅/❌
- 総所要時間: X秒
```

## スキップするフェーズ

以下のフェーズは検証対象外のためスキップ:
- SEMANTIC（並列化対象外）
- VERIFICATION（実装検証のみ、時間節約のため簡略化）
- IMPACT_ANALYSIS（並列化対象外）
- 実装作業（READY後半）
- コミット、品質チェック

## 重要な注意事項

⚠️ **必須**: 並列実行の指示に従うこと

- search_textで複数パターンを検索する場合、必ず1回の呼び出しでリストとして渡す
- 複数ファイルを読む場合、必ず1メッセージで複数Readツールを呼び出す
- これにより15-25秒の時間削減が可能

詳細は`.claude/commands/code.md`の並列実行セクションを参照してください。

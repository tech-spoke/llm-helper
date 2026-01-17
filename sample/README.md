# sample - llm-helper Demo Programs

llm-helper MCPサーバーの動作確認用デモプログラム集です。

## Files

| File | Description |
|------|-------------|
| `demo_mcp_client.py` | ツールモジュールの直接呼び出しとワークフローのデモ |

## Usage

```bash
cd /home/kazuki/public_html/llm-helper
python sample/demo_mcp_client.py
```

## Demo Contents

### 1. Direct Tool Calls
- `ContextProvider`: context.yml読み込みとドキュメント検出
- `ImpactAnalyzer`: 変更影響分析

### 2. Session Workflow (Simulated)
フェーズゲートワークフローの説明:
```
EXPLORATION → SEMANTIC → VERIFICATION → IMPACT_ANALYSIS → READY
```

### 3. ChromaDB Semantic Search
- ChromaDBコレクション一覧
- セマンティック検索のサンプル

## Note

実際のMCPプロトコル経由での呼び出しは、Claude Code等のMCPクライアントを使用してください。

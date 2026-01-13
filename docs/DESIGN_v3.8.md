# Code Intelligence MCP Server v3.8 設計

## 概要

v3.8 では「森（コード検索）」と「地図（合意事項）」を分離し、プロジェクト固有の知識を蓄積・活用するアーキテクチャを導入する。

### 主要な変更点

| バージョン | 特徴 |
|-----------|------|
| v3.6 | QueryFrame による自然文構造化 |
| v3.7 | Embedding による意味検証 + learned_pairs.json |
| **v3.8** | **森/地図の分離 + devrag 二重化** |

---

## 1. アーキテクチャ

### 1.1 全体構成

```
┌─────────────────────────────────────────────────────────────────────┐
│                         MCP Clients (Claude等)                      │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
            ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐
            │ devrag-map  │ │devrag-forest│ │   code-intel        │
            │ (地図)      │ │ (森)        │ │   (オーケストレータ)│
            └──────┬──────┘ └──────┬──────┘ └──────────┬──────────┘
                   │               │                   │
                   ▼               ▼                   ▼
            ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐
            │vectors-map  │ │vectors-forest│ │ セッション管理      │
            │    .db      │ │    .db      │ │ QueryFrame          │
            └─────────────┘ └─────────────┘ │ EmbeddingValidator  │
                   ▲                        └──────────┬──────────┘
                   │                                   │
            ┌──────┴──────────────────────────────────┘
            │
     .code-intel/
     ├── learned_pairs.json   ← Source of Truth (人間編集可能)
     └── agreements/          ← devrag-map 用 Markdown
         ├── login_auth.md
         └── user_session.md
```

### 1.2 「森」と「地図」の役割

| 名称 | devrag インスタンス | 役割 | データの性質 |
|------|-------------------|------|-------------|
| **森 (Forest)** | devrag-forest | ソースコード全体の意味検索 | 生データ・HYPOTHESIS |
| **地図 (Map)** | devrag-map | 過去の成功ペア・設計規約 | 確定データ・FACT |

### 1.3 検索の優先順序（Short-circuit Logic）

```
1. devrag-map で検索（地図優先）
   ├─ スコア ≥ 0.7: PREVIOUS_SUCCESS として採用
   │                → 森の探索をスキップ（高速化）
   │
   └─ スコア < 0.7: 次へ

2. devrag-forest で検索（森の探索）
   └─ 結果は HYPOTHESIS として扱う
      → VERIFICATION フェーズで確認必須
```

---

## 2. 設定ファイル

### 2.1 MCP 設定 (.mcp.json)

```json
{
  "mcpServers": {
    "devrag-map": {
      "type": "stdio",
      "command": "/usr/local/bin/devrag",
      "args": ["--config", "devrag-map.json"]
    },
    "devrag-forest": {
      "type": "stdio",
      "command": "/usr/local/bin/devrag",
      "args": ["--config", "devrag-forest.json"]
    },
    "code-intel": {
      "type": "stdio",
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/code_intel_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/llm-helper"
      }
    }
  }
}
```

### 2.2 devrag-map.json（地図用）

```json
{
  "document_patterns": ["./.code-intel/agreements"],
  "db_path": "./.code-intel/vectors-map.db",
  "chunk_size": 300,
  "search_top_k": 10,
  "model": {
    "name": "multilingual-e5-small",
    "dimensions": 384
  }
}
```

### 2.3 devrag-forest.json（森用）

```json
{
  "document_patterns": ["./documents", "./src"],
  "db_path": "./vectors-forest.db",
  "chunk_size": 500,
  "search_top_k": 5,
  "model": {
    "name": "multilingual-e5-small",
    "dimensions": 384
  }
}
```

---

## 3. データ構造

### 3.1 agreements/ Markdown 形式

```markdown
---
doc_type: agreement
nl_term: ログイン機能
symbol: AuthService
similarity: 0.85
session_id: session_20250112_143000
learned_at: 2025-01-12T14:35:00
---

# ログイン機能 → AuthService

## 根拠 (Code Evidence)

- `AuthService.login()` がユーザー認証を処理
- `AuthService` は `UserRepository` と連携してDB照合を行う

## 関連ファイル

- `src/auth/service.py:42`
- `src/controllers/login_controller.py:15`

## セッション情報

- Intent: MODIFY
- 成功時の QueryFrame:
  - target_feature: ログイン機能
  - trigger_condition: パスワードが空のとき
  - observed_issue: エラーが出ない
  - desired_action: バリデーション追加
```

### 3.2 learned_pairs.json（Source of Truth）

```json
{
  "version": 2,
  "updated_at": "2025-01-12T14:35:00",
  "embedding_model": "intfloat/multilingual-e5-small",
  "pairs": [
    {
      "nl_term": "ログイン機能",
      "symbol": "AuthService",
      "similarity": 0.85,
      "code_evidence": "AuthService.login() handles authentication",
      "session_id": "session_20250112_143000",
      "learned_at": "2025-01-12T14:35:00",
      "agreement_file": "login_auth.md"
    }
  ]
}
```

---

## 4. 実装変更点

### 4.1 SessionState への追加

```python
@dataclass
class SessionState:
    # 既存フィールド
    session_id: str
    intent: str
    query: str
    phase: Phase
    query_frame: QueryFrame | None

    # v3.8 追加
    repo_path: str = "."  # プロジェクトルート
    map_results: list[dict] = field(default_factory=list)  # 地図検索結果
    forest_results: list[dict] = field(default_factory=list)  # 森検索結果
```

### 4.2 record_outcome の拡張

```python
async def record_outcome(outcome_log: OutcomeLog, session: SessionState):
    # 既存: JSON に書き込み
    result = save_outcome_log(outcome_log)

    if outcome_log.outcome == "success" and session.query_frame:
        qf = session.query_frame

        # 1. learned_pairs.json に追加
        cache_successful_pair(
            nl_term=qf.target_feature,
            symbol=sym.name,
            similarity=sym.confidence,
            code_evidence=sym.evidence.result_summary if sym.evidence else None,
            session_id=session.session_id,
            project_root=session.repo_path,  # v3.8: 追加
        )

        # 2. agreements/ に Markdown を生成
        await generate_agreement_markdown(
            nl_term=qf.target_feature,
            symbol=sym.name,
            query_frame=qf,
            session=session,
        )

        # 3. devrag-map に再インデックス依頼（オプション）
        # await trigger_devrag_reindex("map")

    return result
```

### 4.3 探索フロー（EXPLORATION フェーズ）

```python
async def explore_with_map_priority(query: str, session: SessionState):
    """
    v3.8: 地図優先の探索フロー
    """

    # Step 1: 地図を検索
    map_results = await call_mcp_tool("devrag-map", "search", {"query": query})

    if map_results and map_results[0]["score"] >= 0.7:
        # 地図にヒット → PREVIOUS_SUCCESS として採用
        session.map_results = map_results
        return {
            "source": "map",
            "status": "PREVIOUS_SUCCESS",
            "results": map_results,
            "message": "過去の成功パターンが見つかりました。森の探索をスキップします。",
            "next_action": "submit_understanding で確認してください。",
        }

    # Step 2: 森を検索
    forest_results = await call_mcp_tool("devrag-forest", "search", {"query": query})
    session.forest_results = forest_results

    return {
        "source": "forest",
        "status": "HYPOTHESIS",
        "results": forest_results,
        "message": "コードベースから候補を取得しました。VERIFICATION が必要です。",
    }
```

### 4.4 新規ツール追加

| ツール名 | 説明 | 状態 |
|---------|------|------|
| `confirm_symbol_relevance` | LLM検証後に mapped_symbols の confidence を確定 | **実装済み** |
| `search_map` | 地図（合意事項）のみを検索（devrag-map 経由） | devrag経由 |
| `search_forest` | 森（コード）のみを検索（devrag-forest 経由） | devrag経由 |
| `search_smart` | 地図優先で検索（Short-circuit） | 未実装 |
| `add_agreement` | 手動で合意事項を追加 | 未実装 |
| `list_agreements` | 現在の合意事項一覧 | 未実装 |

### 4.5 submit_understanding の変更（実装済み）

`symbols_identified` を自動的に `mapped_symbols` に追加：

```python
# v3.8: symbols_identified を query_frame.mapped_symbols に自動追加
if session.query_frame and symbols_identified:
    for symbol in symbols_identified:
        session.query_frame.add_mapped_symbol(
            name=symbol,
            source=SlotSource.FACT,  # EXPLORATION で発見 = FACT
            confidence=0.5,  # デフォルト値（confirm_symbol_relevance で更新）
        )
```

### 4.6 シンボル検証フロー（実装済み）

```
submit_understanding(symbols_identified=["AuthService", "UserRepository"])
    ↓ mapped_symbols に自動追加 (confidence=0.5)

validate_symbol_relevance(target_feature="ログイン機能", symbols=[...])
    ↓ Embedding 提案を返却

confirm_symbol_relevance(
    relevant_symbols=["AuthService"],
    code_evidence="AuthService.login() がユーザー認証を処理"
)
    ↓ confidence を Embedding スコアに更新 (例: 0.85)
    ↓ code_evidence を SlotEvidence として保存

record_outcome(outcome="success")
    ↓ mapped_symbols から agreements を自動生成
```

---

## 5. フェーズ制御の変更

### 5.1 フェーズ遷移図

```
                    ┌──────────────────────────────────┐
                    │                                  │
                    ▼                                  │
┌─────────────┐   地図ヒット   ┌─────────────┐        │
│ EXPLORATION │──────────────▶│    READY    │        │
└──────┬──────┘               └─────────────┘        │
       │                                              │
       │ 地図ミス                                     │
       ▼                                              │
┌─────────────┐            ┌─────────────┐           │
│  SEMANTIC   │───────────▶│VERIFICATION │───────────┘
│  (森の探索) │            │  (仮説検証) │
└─────────────┘            └─────────────┘
```

### 5.2 地図ヒット時のスキップ条件

| 条件 | 動作 |
|------|------|
| `map_score >= 0.8` | EXPLORATION → READY（即座に実装可能） |
| `0.7 <= map_score < 0.8` | EXPLORATION → VERIFICATION（簡易確認） |
| `map_score < 0.7` | 通常フロー（SEMANTIC へ） |

---

## 6. マイグレーション

### 6.1 v3.7 → v3.8

```bash
# 1. 新しい設定ファイルを作成
cp config.json devrag-forest.json
cat > devrag-map.json << 'EOF'
{
  "document_patterns": ["./.code-intel/agreements"],
  "db_path": "./.code-intel/vectors-map.db",
  "chunk_size": 300,
  "search_top_k": 10,
  "model": {"name": "multilingual-e5-small", "dimensions": 384}
}
EOF

# 2. agreements ディレクトリを作成
mkdir -p .code-intel/agreements

# 3. 既存の learned_pairs.json から agreements/ を生成（マイグレーションスクリプト）
python -m tools.migrate_to_v38

# 4. .mcp.json を更新（devrag-map, devrag-forest を追加）

# 5. devrag-map を初期化
LD_LIBRARY_PATH=/usr/local/lib devrag --config devrag-map.json sync
```

---

## 7. 未解決の課題

| 課題 | 説明 | 優先度 |
|------|------|--------|
| A | devrag-map の自動再インデックス | 中 |
| B | agreements/ の競合解決（Git マージ） | 低 |
| C | 地図の有効期限管理 | 低 |
| D | 複数プロジェクト間での地図共有（オプション） | 低 |

---

## 8. 期待される効果

| 効果 | 説明 |
|------|------|
| **探索時間短縮** | 過去の成功パターンで森の探索をスキップ |
| **一貫性向上** | プロジェクト固有の「方言」を記憶 |
| **透明性** | learned_pairs.json + agreements/ で人間が確認可能 |
| **LLM サボり防止** | 地図がない = 未知領域 → 強制的に森を探索 |

---

## 9. 実装ガイドライン（レビュー反映）

### 9.1 setup.sh での自動生成

```bash
#!/bin/bash
# setup.sh (v3.8)

# devrag 設定ファイルの自動生成
generate_devrag_configs() {
    local project_root="${1:-.}"

    # devrag-forest.json（森：コード検索用）
    cat > "${project_root}/devrag-forest.json" << 'EOF'
{
  "document_patterns": ["./documents", "./src"],
  "db_path": "./vectors-forest.db",
  "chunk_size": 500,
  "search_top_k": 5,
  "model": {"name": "multilingual-e5-small", "dimensions": 384}
}
EOF

    # devrag-map.json（地図：合意事項用）
    cat > "${project_root}/devrag-map.json" << 'EOF'
{
  "document_patterns": ["./.code-intel/agreements"],
  "db_path": "./.code-intel/vectors-map.db",
  "chunk_size": 300,
  "search_top_k": 10,
  "model": {"name": "multilingual-e5-small", "dimensions": 384}
}
EOF

    # .code-intel ディレクトリ構造
    mkdir -p "${project_root}/.code-intel/agreements"

    echo "Generated devrag configs for: ${project_root}"
}

# プロジェクトごとに独立した .code-intel/ を作成
# → プロジェクト間で「地図」が混ざるリスクを回避
```

### 9.2 キャメルケース分解の適用箇所

v3.7 の `EmbeddingValidator.split_camel_case()` を以下に適用：

| 適用箇所 | 理由 |
|---------|------|
| agreements/ Markdown 生成時 | 自然言語検索でヒットしやすくなる |
| devrag-map 登録時 | ベクトル類似度が向上 |
| learned_pairs.json 保存時 | JSON 検索との一貫性 |

```python
# tools/agreements.py (新規)
from tools.embedding import EmbeddingValidator

def generate_agreement_content(nl_term: str, symbol: str, evidence: str) -> str:
    """agreements/ 用の Markdown を生成"""
    symbol_normalized = EmbeddingValidator.split_camel_case(symbol)

    return f"""---
doc_type: agreement
nl_term: {nl_term}
symbol: {symbol}
symbol_normalized: {symbol_normalized}
---

# {nl_term} → {symbol}

**シンボル（分解）**: {symbol_normalized}

## 根拠 (Code Evidence)

{evidence}
"""
```

### 9.3 自動再インデックス（record_outcome 連携）

```python
# code_intel_server.py の record_outcome 拡張

async def record_outcome_with_map_sync(
    outcome_log: OutcomeLog,
    session: SessionState
):
    """v3.8: 成功時に地図を更新し、devrag-map を再インデックス"""

    result = await record_outcome(outcome_log, session)

    if outcome_log.outcome == "success":
        # 1. agreements/ に Markdown を生成
        agreement_file = await generate_agreement_markdown(
            session.query_frame,
            session
        )

        # 2. devrag-map の再インデックス（バックグラウンド）
        asyncio.create_task(
            trigger_devrag_map_sync(session.repo_path)
        )

        result["map_updated"] = True
        result["agreement_file"] = agreement_file
        result["note"] = "次回の探索から新しい地図が有効になります"

    return result


async def trigger_devrag_map_sync(repo_path: str):
    """devrag-map の再インデックスをトリガー"""
    config_path = Path(repo_path) / "devrag-map.json"

    if config_path.exists():
        process = await asyncio.create_subprocess_exec(
            "devrag",
            "--config", str(config_path),
            "sync",  # または適切なサブコマンド
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
```

---

## 10. 議論ポイント

1. **地図のスコア閾値**: 0.7 で適切か？
2. **agreements/ のファイル命名規則**: `{nl_term}_{symbol}.md` vs `{session_id}.md`
3. **devrag-map の同期タイミング**: record_outcome 直後 vs バッチ処理
4. **既存 devrag との後方互換性**: devrag-forest への改名は必要か？

---

*Created: 2025-01-12*
*Version: 3.8*

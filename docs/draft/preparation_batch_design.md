# 準備フェーズバッチ化 設計ドキュメント

## 概要

現在3回のLLM往復が必要な準備フェーズ（Step 2-3.5）を、1回のツール呼び出しに統合することで、6-10秒の高速化を実現する。

## 現在のフロー（問題点）

```
Step 2: start_session
  ↓ (LLM往復: 2-3秒)
  ↓ LLMが「次はset_query_frame」と判断
  ↓
Step 3: set_query_frame
  ↓ (LLM往復: 2-3秒)
  ↓ LLMが「次はbegin_phase_gate」と判断
  ↓
Step 3.5: begin_phase_gate
  ↓
EXPLORATION開始

合計: 3往復 + ツール実行時間 + LLM思考時間 ≒ 19.5秒
- sync_index: 11.5秒
- set_query_frame: 3秒（LLM処理）
- begin_phase_gate: 5秒
- LLM往復×2: 4-6秒
- LLM思考（ツール選択）: 0秒（バッチ化で削減）
```

## 改善後のフロー

```
Step 2-3.5: prepare_session_batch
  サーバー側で:
    1. start_session 実行
    2. sync_index 実行（11.5秒）
    3. set_query_frame 実行（LLM API呼び出し: 3秒）
    4. begin_phase_gate 実行（5秒）
  ↓
  1回のレスポンスで全結果返却
  ↓
EXPLORATION開始

合計: 1往復 + ツール実行時間 ≒ 19.5秒
- sync_index: 11.5秒
- set_query_frame: 3秒（LLM処理）
- begin_phase_gate: 5秒
- LLM往復×1: 0秒（内部処理）
- LLM思考: 0秒

削減: 4-6秒（LLM往復2回分）
```

---

## 新しいMCPツール: prepare_session_batch

### ツール定義

```python
{
    "name": "prepare_session_batch",
    "description": "Batch execution of preparation phase (Step 2-3.5). "
                   "Combines start_session, sync_index, set_query_frame, and begin_phase_gate.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["IMPLEMENT", "MODIFY", "INVESTIGATE", "QUESTION"],
                "description": "Intent type from Step 1"
            },
            "query": {
                "type": "string",
                "description": "User's original request"
            },
            "gate_level": {
                "type": "string",
                "enum": ["high", "middle", "low", "auto", "none"],
                "default": "high",
                "description": "Gate level for exploration phases"
            },
            "skip_branch": {
                "type": "boolean",
                "default": false,
                "description": "True for --quick mode (no branch creation)"
            },
            "doc_research_enabled": {
                "type": "boolean",
                "default": false,
                "description": "Enable DOCUMENT_RESEARCH phase (v1.3)"
            }
        },
        "required": ["intent", "query"]
    }
}
```

### レスポンス形式

```json
{
  "success": true,
  "session_id": "session_20260123_160021",
  "phase": "EXPLORATION",
  "query_frame": {
    "target_feature": {"value": "...", "quote": "..."},
    "trigger_condition": {"value": "...", "quote": "..."},
    "observed_issue": {"value": "...", "quote": "..."},
    "desired_action": {"value": "...", "quote": "..."}
  },
  "risk_level": "HIGH",
  "branch": {
    "created": true,
    "name": "llm_task_session_20260123_160021_from_main",
    "base_branch": "main"
  },
  "chromadb": {
    "synced": true,
    "duration_ms": 11554,
    "added": 0,
    "modified": 3,
    "deleted": 0
  },
  "essential_context": {
    "project_rules": {
      "source": ".claude/CLAUDE.md",
      "summary": "DO:\n- Use Service layer...\nDON'T:\n- Write complex logic..."
    }
  },
  "next_action": "Start EXPLORATION phase with available tools"
}
```

---

## 実装詳細

### 1. code_intel_server.py への追加

```python
# code_intel_server.py

async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute a code intelligence tool."""

    # ... 既存のツール処理 ...

    elif name == "prepare_session_batch":
        # v1.9: Batch execution of preparation phase
        intent = arguments.get("intent")
        query = arguments.get("query")
        gate_level = arguments.get("gate_level", "high")
        skip_branch = arguments.get("skip_branch", False)
        doc_research_enabled = arguments.get("doc_research_enabled", False)

        if not intent or not query:
            result = {
                "success": False,
                "error": "intent and query are required"
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Step 2: start_session
        session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        session = session_manager.create_session(
            intent=intent,
            query=query,
            session_id=session_id,
            repo_path=".",
            gate_level=gate_level
        )

        # ChromaDB sync (if needed)
        chromadb_manager = get_chromadb_manager(".")
        chromadb_result = {}

        if chromadb_manager.needs_sync():
            sync_result = chromadb_manager.sync_forest()
            chromadb_result = {
                "synced": True,
                "duration_ms": sync_result.duration_ms,
                "added": sync_result.added,
                "modified": sync_result.modified,
                "deleted": sync_result.deleted
            }
        else:
            chromadb_result = {
                "synced": False,
                "reason": "No sync needed (cache valid)"
            }

        # Load essential context (project_rules)
        context_provider = ContextProvider(".")
        essential_context = {}

        try:
            project_rules = context_provider.get_project_rules()
            if project_rules:
                essential_context["project_rules"] = {
                    "source": project_rules.get("source", "CLAUDE.md"),
                    "summary": project_rules.get("summary", "")
                }
        except Exception as e:
            logger.debug(f"Failed to load project_rules: {e}")

        # Step 2.5: DOCUMENT_RESEARCH (optional, not implemented in batch for now)
        # TODO: Implement if doc_research_enabled=true

        # Step 3: set_query_frame (LLM API call on server side)
        query_frame_dict = await _extract_query_frame_server_side(query)

        # Validate and set query_frame
        query_frame = QueryFrame(
            target_feature=query_frame_dict.get("target_feature"),
            trigger_condition=query_frame_dict.get("trigger_condition"),
            observed_issue=query_frame_dict.get("observed_issue"),
            desired_action=query_frame_dict.get("desired_action")
        )

        # Set query_frame in session
        session.set_query_frame(query_frame)

        # Assess risk level
        risk_level = assess_risk_level(query_frame, intent)
        session.risk_level = risk_level

        # Step 3.5: begin_phase_gate
        branch_manager = None
        branch_result = {}

        if not skip_branch:
            # Check for stale branches
            stale_info = await BranchManager.check_stale_task_branches(".")

            if stale_info and stale_info.get("has_stale_branches"):
                # Stale branches detected - return error and require user intervention
                result = {
                    "success": False,
                    "error": "stale_branches_detected",
                    "stale_branches": stale_info,
                    "recovery_options": {
                        "delete": "Run cleanup_stale_branches, then retry",
                        "merge": "Run merge_to_base for each branch, then retry",
                        "continue": "Not supported in batch mode - use standard flow"
                    }
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            # Create branch
            branch_manager = BranchManager(".", session.session_id)
            setup_result = await branch_manager.setup_task_branch()

            if setup_result.success:
                session.task_branch_enabled = True
                session.task_branch_name = setup_result.branch_name

                # Determine starting phase based on gate_level
                if gate_level == "none":
                    session.transition_to_phase(Phase.READY, reason="batch_prepare_gate_none")
                else:
                    session.transition_to_phase(Phase.EXPLORATION, reason="batch_prepare")

                branch_result = {
                    "created": True,
                    "name": setup_result.branch_name,
                    "base_branch": setup_result.base_branch
                }
            else:
                result = {
                    "success": False,
                    "error": "branch_creation_failed",
                    "message": setup_result.error
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
        else:
            # skip_branch=true (--quick mode)
            session.task_branch_enabled = False
            session.transition_to_phase(Phase.READY, reason="batch_prepare_skip_branch")

            branch_result = {
                "created": False,
                "reason": "skip_branch=true (quick mode)"
            }

        # Build result
        result = {
            "success": True,
            "session_id": session.session_id,
            "phase": session.phase.name,
            "query_frame": query_frame.to_dict() if query_frame else None,
            "risk_level": risk_level,
            "branch": branch_result,
            "chromadb": chromadb_result,
            "essential_context": essential_context,
            "next_action": f"Start {session.phase.name} phase with available tools"
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]


async def _extract_query_frame_server_side(query: str) -> dict:
    """
    Extract query frame on server side using Claude API.

    This function makes a direct API call to Claude to extract structured slots
    from the user's natural language query.
    """
    import anthropic
    import os

    # Get API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    # Extraction prompt (from QueryFrame)
    extraction_prompt = f"""以下のクエリから情報を抽出してください。

ルール：
- 明示されている情報のみ抽出
- 各スロットには「value」と「quote」を必ず含める
- 「quote」は raw_query からの正確な引用（原文そのまま）
- 推測・補完は禁止。不明な場合は null

出力形式（JSON）：
{{
  "target_feature": {{"value": "対象機能", "quote": "raw_queryからの引用"}} または null,
  "trigger_condition": {{"value": "再現条件", "quote": "raw_queryからの引用"}} または null,
  "observed_issue": {{"value": "問題", "quote": "raw_queryからの引用"}} または null,
  "desired_action": {{"value": "期待する修正", "quote": "raw_queryからの引用"}} または null
}}

クエリ: {query}"""

    # Call Claude API
    response = client.messages.create(
        model="claude-3-5-haiku-20241022",  # Use Haiku for speed
        max_tokens=1024,
        messages=[
            {"role": "user", "content": extraction_prompt}
        ]
    )

    # Parse response
    response_text = response.content[0].text

    # Extract JSON from response
    import json
    import re

    # Try to find JSON in response
    json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
    if json_match:
        query_frame_dict = json.loads(json_match.group(0))
        return query_frame_dict
    else:
        # Fallback: empty query_frame
        return {
            "target_feature": None,
            "trigger_condition": None,
            "observed_issue": None,
            "desired_action": None
        }
```

---

### 2. ツール定義の追加

```python
# code_intel_server.py の Tool 定義セクションに追加

Tool(
    name="prepare_session_batch",
    description="Batch execution of preparation phase (Step 2-3.5). "
                "Combines start_session, sync_index, set_query_frame, and begin_phase_gate. "
                "Use this instead of calling each step separately for better performance.",
    inputSchema={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["IMPLEMENT", "MODIFY", "INVESTIGATE", "QUESTION"],
                "description": "Intent type from Step 1"
            },
            "query": {
                "type": "string",
                "description": "User's original request"
            },
            "gate_level": {
                "type": "string",
                "enum": ["high", "middle", "low", "auto", "none"],
                "default": "high",
                "description": "Gate level for exploration phases"
            },
            "skip_branch": {
                "type": "boolean",
                "default": False,
                "description": "True for --quick mode (no branch creation)"
            },
            "doc_research_enabled": {
                "type": "boolean",
                "default": False,
                "description": "Enable DOCUMENT_RESEARCH phase (v1.3)"
            }
        },
        "required": ["intent", "query"]
    }
)
```

---

### 3. code.md の更新

```markdown
## Step 2-3.5: Batch Preparation (v1.9 Performance Optimization)

**Purpose:** Execute preparation steps (start_session + sync + query_frame + phase_gate) in one call for better performance.

**Execute once:**
```
mcp__code-intel__prepare_session_batch
  intent: "MODIFY"
  query: "user's original request"
  gate_level: "high"
  skip_branch: false
```

**Response:**
```json
{
  "success": true,
  "session_id": "session_20260123_160021",
  "phase": "EXPLORATION",
  "query_frame": {
    "target_feature": {"value": "...", "quote": "..."},
    ...
  },
  "branch": {
    "created": true,
    "name": "llm_task_...",
    "base_branch": "main"
  },
  "next_action": "Start EXPLORATION phase"
}
```

**What this tool does internally:**
1. Create session
2. Sync ChromaDB (if needed)
3. Extract QueryFrame (calls Claude API on server side)
4. Create task branch (unless skip_branch=true)
5. Transition to appropriate phase

**Replaces:**
- Step 2: start_session
- Step 3: set_query_frame
- Step 3.5: begin_phase_gate

**Performance gain:** ~6-10 seconds (eliminates 2 LLM round-trips)

---

<!-- Old steps (kept for reference)

## Step 2: Session Start
...

## Step 3: QueryFrame Setup
...

## Step 3.5: Begin Phase Gate
...

-->
```

---

## エラーハンドリング

### 1. Stale Branch Detection

バッチモードではユーザー介入が必要な場合、エラーを返す：

```json
{
  "success": false,
  "error": "stale_branches_detected",
  "stale_branches": {...},
  "recovery_options": {
    "delete": "Run cleanup_stale_branches, then retry",
    "merge": "Run merge_to_base for each branch, then retry",
    "continue": "Not supported in batch mode - use standard flow"
  }
}
```

→ LLMは cleanup_stale_branches を実行してから、prepare_session_batch をリトライ

### 2. ChromaDB Sync Failure

```json
{
  "success": false,
  "error": "chromadb_sync_failed",
  "message": "Failed to sync: [error details]"
}
```

### 3. Query Frame Extraction Failure

```json
{
  "success": true,
  "session_id": "...",
  "query_frame": null,
  "warning": "Failed to extract query_frame: [error details]",
  "phase": "EXPLORATION"
}
```

→ query_frame が null でも続行可能（EXPLORATION で補完）

---

## テスト計画

### 1. ユニットテスト

```python
# tests/test_prepare_batch.py

async def test_prepare_session_batch_success():
    """Normal case: all steps succeed"""
    result = await call_tool("prepare_session_batch", {
        "intent": "MODIFY",
        "query": "Fix the login button color",
        "gate_level": "high"
    })

    assert result["success"] == True
    assert result["session_id"] is not None
    assert result["phase"] == "EXPLORATION"
    assert result["branch"]["created"] == True

async def test_prepare_session_batch_stale_branches():
    """Stale branches exist - should return error"""
    # Setup: create stale branch
    ...

    result = await call_tool("prepare_session_batch", {...})

    assert result["success"] == False
    assert result["error"] == "stale_branches_detected"

async def test_prepare_session_batch_skip_branch():
    """skip_branch=true (--quick mode)"""
    result = await call_tool("prepare_session_batch", {
        "intent": "MODIFY",
        "query": "Quick fix",
        "skip_branch": True
    })

    assert result["success"] == True
    assert result["phase"] == "READY"
    assert result["branch"]["created"] == False
```

### 2. 統合テスト（AVITO側）

```bash
# Test 1: Normal flow
/code Fix the modal background color

# Expected: prepare_session_batch is called once
# Duration: ~19.5 seconds (vs 25.5 seconds before)

# Test 2: With --quick
/code -q Change button color

# Expected: prepare_session_batch with skip_branch=true
# Duration: ~8 seconds (sync+query_frame only)

# Test 3: With stale branches
# (Create stale branch manually)
/code Fix bug

# Expected: Error returned, cleanup_stale_branches recommended
```

---

## パフォーマンス比較

### Before (現在)

| Step | 所要時間 | 累積 |
|------|----------|------|
| start_session | 0.1秒 | 0.1秒 |
| (LLM往復) | 2-3秒 | 2-3秒 |
| sync_index | 11.5秒 | 13.5-14.5秒 |
| (LLM往復) | 2-3秒 | 15.5-17.5秒 |
| set_query_frame | 3秒 | 18.5-20.5秒 |
| (LLM往復) | 2-3秒 | 20.5-23.5秒 |
| begin_phase_gate | 5秒 | 25.5-28.5秒 |

### After (改善後)

| Step | 所要時間 | 累積 |
|------|----------|------|
| prepare_session_batch | | |
| - start_session | 0.1秒 | 0.1秒 |
| - sync_index | 11.5秒 | 11.6秒 |
| - set_query_frame (API) | 3秒 | 14.6秒 |
| - begin_phase_gate | 5秒 | 19.6秒 |
| (LLM往復×1のみ) | 0秒 | 19.6秒 |

**削減: 5.9-8.9秒（21-31%削減）**

---

## 実装の優先度と工数

| タスク | 工数 | 優先度 |
|--------|------|--------|
| code_intel_server.py 実装 | 2-3時間 | 🔥 高 |
| _extract_query_frame_server_side 実装 | 1時間 | 🔥 高 |
| ツール定義追加 | 30分 | 🔥 高 |
| code.md 更新 | 30分 | ⚡ 中 |
| ユニットテスト | 1時間 | ⚡ 中 |
| 統合テスト（AVITO側） | 30分 | ⚡ 中 |
| ドキュメント更新 | 30分 | 💡 低 |
| **合計** | **5.5-6.5時間** | - |

---

## 次のステップ

1. ✅ 設計レビュー（このドキュメント）
2. 🔥 code_intel_server.py 実装
3. 🔥 code.md 更新
4. ⚡ テスト実施
5. 📊 パフォーマンス測定

---

## 将来の拡張

### DOCUMENT_RESEARCH のバッチ統合

現在は `doc_research_enabled` パラメータがあるが、実装は未完了。

将来的には：
```python
if doc_research_enabled:
    # Spawn sub-agent and wait for result
    doc_research_result = await spawn_document_research_agent(...)
    essential_context["mandatory_rules"] = doc_research_result
```

**ただし、これを追加すると30-40秒増加するため、優先度は低い。**

---

## まとめ

- **目的**: 準備フェーズを1回のツール呼び出しに統合
- **削減時間**: 5.9-8.9秒（21-31%削減）
- **実装工数**: 5.5-6.5時間
- **リスク**: 低（既存フローは維持、新ツールは追加）
- **後方互換性**: 完全（古いステップも動作可能）

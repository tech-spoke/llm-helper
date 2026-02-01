# フェーズ契約（①オーケストレーター→②LLM→③LLMサブミット）

この文書は、Complete Flow の各フェーズで「①オーケストレーターが②LLMに渡す指示」と
「③LLMが submit_phase で返す payload の構造」を明文化したもの。

**ソース・オブ・トゥルース**
- 生成: `tools/session.py` の `get_phase_response()` が、`PHASE_INSTRUCTIONS` と `EXPECTED_PAYLOADS` を元に
  `instruction` と `expected_payload` を返す。
- 実装参照: `tools/session.py`, `code_intel_server.py`

> 注意: ここに書いてある内容が実装とズレた場合は、実装（`get_phase_response`）が正。
> この文書は「ブラックボックス化を避けるための補助資料」。

---

## 指示フォーマット（①→②）
オーケストレーターは次の形で LLM に指示を出す。

```json
{
  "phase": "<PHASE>",
  "step": <STEP>,
  "instruction": "<PHASE_INSTRUCTIONS[phase]>",
  "expected_payload": { "<EXPECTED_PAYLOADS[phase]>": "..." },
  "call": "submit_phase"
}
```

- `instruction`: 実行すべき作業の指示
- `expected_payload`: submit_phase に送る payload 構造（擬似スキーマ）
- `call`: 実行すべきツール名（常に `submit_phase`）

---

## 主要ツール（参照）
このプロジェクトで利用する基盤ツールの役割（フェーズ共通の前提）。

- ripgrep: 高速テキスト検索（`search_text` / `search_files`）
- ctags: シンボル定義/参照（`find_definitions` / `find_references` / `get_symbols`）
- tree-sitter: 構文解析・構造抽出（`analyze_structure` / `get_function_at_line`）
- AST Chunker: sync_index 時の AST ベース分割（ChromaDB への投入単位）
- ChromaDB Manage: map/forest の意味検索（`semantic_search` / `fetch_chunk_detail`）

---

## フェーズ別の指示（①→②）

### Step 2: BRANCH_INTERVENTION
```json
{
  "phase": "BRANCH_INTERVENTION",
  "step": 2,
  "instruction": "Stale ブランチが検出されました。delete/merge/continue の意味を説明して選択肢を提示し、選択結果を submit_phase で送信してください。",
  "expected_payload": {
    "choice": "str: 'delete' | 'merge' | 'continue'",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


### Step 3: DOCUMENT_RESEARCH
```json
{
  "phase": "DOCUMENT_RESEARCH",
  "step": 3,
  "instruction": "設計文書を調査してください。完了後 submit_phase で送信してください。",
  "expected_payload": {
    "documents_reviewed": "list[str]",
    "tools_used": "list[str]",
    "summary": "str"
  },
 "call": "submit_phase"
}
```
利用ツール:
- `search_files`（任意: docs の一覧・絞り込み）
- `search_text`（任意: キーワード探索）
- `submit_phase`（必須）

付帯リソース（本来の設計）:
- `.code-intel/context.yml` の `project_rules` はメインエージェントが読む
- `.code-intel/context.yml` の `doc_research` はメイン/サブエージェントが従う
- `.code-intel/context.yml` の `document_search` はサブエージェントの探索指針
- サブエージェントは「守るべきルールの箇所」と「読むべき文書箇所」を返し、メインが該当箇所を読む
- `.code-intel/doc_research/`（設計文書調査のプロンプト）
- 旧フローの Step 2.5 に対応

### Step 4: QUERY_FRAME
```json
{
  "phase": "QUERY_FRAME",
  "step": 4,
  "instruction": "ユーザーの指示から構造化スロットを抽出し submit_phase で送信してください。",
  "expected_payload": {
    "action_type": "str",
    "target_symbols": "list[str]",
    "scope": "str",
    "constraints": "str",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


### Step 5: EXPLORATION
```json
{
  "phase": "EXPLORATION",
  "step": 5,
  "instruction": "code-intel ツールを最低2種類使って探索し、完了後 submit_phase で送信してください。",
  "expected_payload": {
    "explored_files": "list[str]",
    "findings": "list[str]",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `search_text`
- `search_files`
- `analyze_structure`
- `get_function_at_line`
- `find_definitions`
- `find_references`
- `get_symbols`
- `submit_phase`（必須）


### Step 6: Q1
```json
{
  "phase": "Q1",
  "step": 6,
  "instruction": "SEMANTIC フェーズが必要か評価してください。submit_phase で結果を送信してください。",
  "expected_payload": {
    "needs_more_information": "bool",
    "reason": "str",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


### Step 7: SEMANTIC
```json
{
  "phase": "SEMANTIC",
  "step": 7,
  "instruction": "semantic_search で追加情報を収集してください。完了後 submit_phase で送信してください。",
  "expected_payload": {
    "search_query": "str",
    "search_results": "list[str]",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `semantic_search`
- `fetch_chunk_detail`（必要に応じて）
- `submit_phase`（必須）


### Step 8: Q2
```json
{
  "phase": "Q2",
  "step": 8,
  "instruction": "VERIFICATION フェーズが必要か評価してください。submit_phase で結果を送信してください。",
  "expected_payload": {
    "has_unverified_hypotheses": "bool",
    "reason": "str",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


### Step 9: VERIFICATION
```json
{
  "phase": "VERIFICATION",
  "step": 9,
  "instruction": "仮説検証を実施してください。完了後 submit_phase で送信してください。",
  "expected_payload": {
    "hypotheses_verified": "list[{hypothesis, result, evidence}]",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


### Step 10: Q3
```json
{
  "phase": "Q3",
  "step": 10,
  "instruction": "IMPACT_ANALYSIS フェーズが必要か評価してください。submit_phase で結果を送信してください。",
  "expected_payload": {
    "needs_impact_analysis": "bool",
    "reason": "str",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


### Step 11: IMPACT_ANALYSIS
```json
{
  "phase": "IMPACT_ANALYSIS",
  "step": 11,
  "instruction": "analyze_impact を実行して影響分析を実施し、完了後 submit_phase で送信してください。",
  "expected_payload": {
    "impact_summary": "dict",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `analyze_impact`
- `submit_phase`（必須）


### Step 12: READY (Planning)
```json
{
  "phase": "READY",
  "step": 12,
  "instruction": "探索結果に基づきタスクを分割し、タスクリストを submit_phase で送信してください。.code-intel/task_planning.md があれば先に確認して従ってください。",
  "expected_payload": {
    "tasks": "list[{id, description, status, failure_count?, revert_reason?}]",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）

付帯リソース（本来の設計）:
- `.code-intel/task_planning.md`（タスク分割の指針）

### Step 13: READY (Implementation)
```json
{
  "phase": "READY",
  "step": 13,
  "instruction": "変更対象ファイルは事前に check_write_target を実行し、次のタスクを実装して submit_phase で報告してください。",
  "expected_payload": {
    "task_id": "str",
    "summary": "str",
    "tools_used": "list[str]"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `check_write_target`（必須）
- `submit_phase`（必須）


### Step 14: READY (Completion)
```json
{
  "phase": "READY",
  "step": 14,
  "instruction": "全タスクが完了しました。summary を添えて submit_phase で送信してください。",
  "expected_payload": {
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


### Step 15: POST_IMPL_VERIFY
```json
{
  "phase": "POST_IMPL_VERIFY",
  "step": 15,
  "instruction": "実装結果を検証してください。検証結果を submit_phase で送信してください。",
  "expected_payload": {
    "verifier_used": "str",
    "passed": "bool",
    "failed_tasks": "list[str] (if passed=false)",
    "details": "str",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）

付帯リソース（本来の設計）:
- `.code-intel/verifiers/*.md`（検証手順の提示）
- 旧フローの Step 8.5 に対応

### Step 16: VERIFY_INTERVENTION
```json
{
  "phase": "VERIFY_INTERVENTION",
  "step": 16,
  "instruction": "介入プロンプトを読み、指示に従ってください。実行結果を submit_phase で送信してください。",
  "expected_payload": {
    "prompt_used": "str",
    "action_taken": "str",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）

付帯リソース（本来の設計）:
- `.code-intel/interventions/*.md`（検証ループ打開の介入）

### Step 17: PRE_COMMIT
```json
{
  "phase": "PRE_COMMIT",
  "step": 17,
  "instruction": "review_changes で差分を取得してレビューし、コミットメッセージとともに submit_phase で送信してください。",
  "expected_payload": {
    "review_prompt_used": "str",
    "reviewed_files": "list[str]",
    "commit_message": "str",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `review_changes`（必須）
- `submit_phase`（必須）

付帯リソース（本来の設計）:
- `.code-intel/review_prompts/garbage_detection.md`（ゴミ検出・差分レビュー）

### Step 18: QUALITY_REVIEW
```json
{
  "phase": "QUALITY_REVIEW",
  "step": 18,
  "instruction": ".code-intel/review_prompts/quality_review.md のチェックリストに従って品質レビューを実施し、結果を submit_phase で送信してください。",
  "expected_payload": {
    "quality_prompt_used": "str",
    "quality_score": "str",
    "issues": "list[str]",
    "tools_used": "list[str]",
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）

付帯リソース（本来の設計）:
- `.code-intel/review_prompts/quality_review.md`（最終品質チェック）
- 旧フローの Step 9.5 に対応

### Step 19: MERGE
```json
{
  "phase": "MERGE",
  "step": 19,
  "instruction": "summary を添えて submit_phase で送信し、マージを実行してください。",
  "expected_payload": {
    "summary": "str"
  },
  "call": "submit_phase"
}
```
利用ツール:
- `submit_phase`（必須）


---

## READY サブステップ補足
- READY は内部的に `READY_PLAN` / `READY_IMPL` / `READY_COMPLETE` の 3 サブステップに分割される。
- `get_phase_response("READY", extra={"ready_substep": "READY_PLAN"})` のようにサブステップが指定される。

---

## 運用メモ
- この契約は LLM クライアント依存ではない（Claude/Codex 共通）。
- 実際の `instruction` と `expected_payload` は、**各フェーズ開始時のサーバー返却**を必ず参照する。
- もし docs の記述とサーバーの返却がズレていたら、docs 側を更新する。

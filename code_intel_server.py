#!/usr/bin/env python3
"""
Code Intelligence MCP Server

Provides Cursor-like code intelligence capabilities using open source tools:
- Repomix: Pack entire repositories for LLM consumption
- ripgrep: Fast text search
- tree-sitter: Code structure analysis
- ctags: Symbol definitions and references

v3.6: QueryFrame-based natural language handling
- QueryFrame: 自然文を構造化（target_feature, trigger_condition, etc.）
- QueryDecomposer: LLMが抽出 → サーバーが検証
- risk_level: 動的成果条件（HIGH/MEDIUM/LOW）
- slot_source: FACT/HYPOTHESIS の区別
- NL→シンボル整合性検証

v3.5: Outcome Log for improvement cycle
- record_outcome: Records session outcomes (success/failure)
- DecisionLog に session_id を追加
- /outcome スキルで人間トリガーの記録

v3.4: 抜け穴を塞ぐ
- 成果物の相互整合性チェック（量だけでなく意味的整合性）
- SEMANTIC 突入理由を missing_requirements に紐付け
- READY での Write 対象を探索済みファイルに制限

v3.3: LLM に判断をさせない設計
- confidence はサーバー側で算出（LLM の自己申告を廃止）
- evidence は構造化必須
- devrag_reason は Enum のみ許可

v3.2: Phase-gated execution to ensure proper tool usage order.
"""

import asyncio
import json
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from tools.repomix_tool import pack_repository
from tools.ripgrep_tool import search_text, search_files
from tools.treesitter_tool import analyze_structure, get_function_at_line
from tools.ctags_tool import find_definitions, find_references, get_symbols
from tools.router import Router, QuestionCategory, UnifiedResult, DecisionLog, FallbackDecision
from tools.session import (
    SessionManager, SessionState, Phase,
    ExplorationResult, SemanticResult, VerificationResult, DevragReason,
    VerificationEvidence, VerifiedHypothesis,
    Hypothesis,  # v3.5
    IntentReclassificationRequired,
    InvalidSemanticReason, WriteTargetBlocked,  # v3.4
)
from tools.outcome_log import (  # v3.5
    OutcomeLog, OutcomeAnalysis, record_outcome,
    get_outcomes_for_session, get_failure_stats,
)
from tools.query_frame import (  # v3.6
    QueryFrame, QueryDecomposer, SlotSource, SlotEvidence,
    validate_slot, assess_risk_level, generate_investigation_guidance,
)


# Create MCP server, router, and session manager
router = Router()
server = Server("code-intel")
session_manager = SessionManager()


async def execute_query(
    question: str,
    path: str = ".",
    symbol: str | None = None,
    file_path: str | None = None,
    show_plan: bool = True,
    intent: str | None = None,  # v3.2: Accept intent from caller (LLM)
) -> dict:
    """
    Execute an intelligent query using the Router.

    This function:
    1. Uses intent from caller (v3.2) or defaults to INVESTIGATE
    2. Checks if bootstrap (repo_pack) is needed
    3. Creates an execution plan
    4. Runs tools in order (composite - all categories combined)
    5. Integrates results
    6. Falls back to devrag if needed

    Args:
        intent: One of "IMPLEMENT", "MODIFY", "INVESTIGATE", "QUESTION".
                Passed from the calling agent (e.g., /code skill).
    """
    # Build context
    context = {"path": path, "question": question}
    if symbol:
        context["symbol"] = symbol
    if file_path:
        context["file_path"] = file_path

    # Create execution plan (v3.2: pass intent from caller)
    plan = router.create_plan(question, context, intent=intent)

    output = {
        "question": question,
        "categories": [c.name for c in plan.categories],
        "reasoning": plan.reasoning,
        "needs_bootstrap": plan.needs_bootstrap,
        "intent_confidence": plan.intent_confidence,  # v3
        "force_devrag": plan.force_devrag,            # v3
    }

    if show_plan:
        output["plan"] = [
            {
                "tool": step.tool,
                "purpose": step.purpose,
                "priority": step.priority,
                "params": step.params,
            }
            for step in plan.steps
        ]

    # Execute each step
    all_results: list[UnifiedResult] = []
    step_outputs = []

    # Step 0: Bootstrap if needed (repo_pack as pre-processing)
    if plan.needs_bootstrap:
        bootstrap_context = router.bootstrap.get_context(path)
        if bootstrap_context is None:
            # Execute repo_pack for this path
            bootstrap_result = await pack_repository(
                path=path,
                output_format="markdown",
            )
            if "error" not in bootstrap_result:
                router.bootstrap.set_context(path, bootstrap_result)
                step_outputs.append({
                    "tool": "repo_pack",
                    "phase": "bootstrap",
                    "raw_result": {
                        "status": "cached",
                        "file_count": bootstrap_result.get("file_count", 0),
                        "note": "Repository context cached for session",
                    },
                })
                # Normalize bootstrap results
                normalized = router.integrator.normalize("repo_pack", bootstrap_result)
                all_results.extend(normalized)
            else:
                step_outputs.append({
                    "tool": "repo_pack",
                    "phase": "bootstrap",
                    "raw_result": bootstrap_result,
                })
        else:
            output["bootstrap_cached"] = True

    # Execute planned steps
    for step in plan.steps:
        step_result = await execute_tool_step(step.tool, step.params, context)
        step_outputs.append({
            "tool": step.tool,
            "phase": "query",
            "priority": step.priority,
            "raw_result": step_result,
        })

        # Normalize results
        normalized = router.integrator.normalize(step.tool, step_result)
        all_results.extend(normalized)

    # Check if we need devrag fallback (v3.1: get full decision details)
    fallback_decision = router.get_fallback_decision(
        results=all_results,
        categories=plan.categories,
        path=path,
        intent_confidence=plan.intent_confidence,
    )
    devrag_used = any(s.tool == "devrag_search" for s in plan.steps)

    # v3.1: Update decision log with fallback info
    if plan.decision_log:
        plan.decision_log.fallback_triggered = fallback_decision.should_fallback and not devrag_used
        plan.decision_log.fallback_reason = fallback_decision.reason
        plan.decision_log.fallback_threshold = fallback_decision.threshold
        plan.decision_log.code_results_count = fallback_decision.code_results_count

    if fallback_decision.should_fallback and not devrag_used:
        output["fallback_triggered"] = True
        output["fallback_reason"] = fallback_decision.reason  # v3.1: From decision

        # If bootstrap wasn't done yet, do it now before devrag
        if not router.bootstrap.is_initialized(path):
            bootstrap_result = await pack_repository(path=path, output_format="markdown")
            if "error" not in bootstrap_result:
                router.bootstrap.set_context(path, bootstrap_result)
                step_outputs.append({
                    "tool": "repo_pack",
                    "phase": "fallback_bootstrap",
                    "raw_result": {"status": "cached_for_devrag"},
                })

        # v3: Use enhanced query with repo context
        enhanced_query = router.bootstrap.get_enhanced_query(path, question)
        devrag_result = await execute_devrag_search(enhanced_query, path)
        if devrag_result:
            step_outputs.append({
                "tool": "devrag_search",
                "phase": "fallback",
                "enhanced_query": enhanced_query,  # v3: Show the enhanced query
                "raw_result": devrag_result,
            })
            normalized = router.integrator.normalize("devrag_search", devrag_result)
            all_results.extend(normalized)

    # Merge and deduplicate results
    merged_results = router.integrator.merge(all_results)

    # Format output
    output["results"] = [
        {
            "file_path": r.file_path,
            "symbol_name": r.symbol_name,
            "start_line": r.start_line,
            "end_line": r.end_line,
            "content_snippet": r.content_snippet,
            "source_tool": r.source_tool,
            "confidence": r.confidence,
        }
        for r in merged_results
    ]
    output["total_results"] = len(merged_results)
    output["step_outputs"] = step_outputs

    # v3.1: Include decision log for observability
    if plan.decision_log:
        output["decision_log"] = plan.decision_log.to_dict()

    # v3.1: Include cache status
    output["cache_status"] = router.bootstrap.get_cache_status(path)

    return output


async def execute_tool_step(tool: str, params: dict, context: dict) -> dict:
    """Execute a single tool step."""
    path = params.get("path", context.get("path", "."))
    symbol = params.get("symbol", context.get("symbol"))

    if tool == "search_text":
        pattern = symbol or params.get("pattern", "")
        if pattern:
            return await search_text(pattern=pattern, path=path)
        return {"error": "No pattern specified"}

    elif tool == "find_definitions":
        if symbol:
            return await find_definitions(symbol=symbol, path=path)
        return {"error": "No symbol specified"}

    elif tool == "find_references":
        if symbol:
            return await find_references(symbol=symbol, path=path)
        return {"error": "No symbol specified"}

    elif tool == "analyze_structure":
        target_file = params.get("file_path", context.get("file_path"))
        if target_file:
            return await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: analyze_structure(file_path=target_file)
            )
        return {"error": "No file_path specified"}

    elif tool == "get_symbols":
        return await get_symbols(path=path)

    elif tool == "devrag_search":
        # v3: Use enhanced query if bootstrap is available
        question = context.get("question", "")
        enhanced_query = router.bootstrap.get_enhanced_query(path, question)
        return await execute_devrag_search(enhanced_query, path)

    return {"error": f"Unknown tool: {tool}"}


async def execute_devrag_search(query: str, path: str) -> dict:
    """
    Execute devrag semantic search.

    Note: This requires devrag to be running as a separate MCP server.
    We call it via subprocess since it's a separate process.
    """
    try:
        # Try to call devrag CLI directly
        process = await asyncio.create_subprocess_exec(
            "devrag", "search", query,
            "--path", path,
            "--format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            return json.loads(stdout.decode())
        else:
            return {
                "error": f"devrag search failed: {stderr.decode()}",
                "note": "devrag may not be configured for this project"
            }

    except FileNotFoundError:
        return {
            "error": "devrag not found",
            "note": "Install devrag or configure it in .mcp.json"
        }
    except Exception as e:
        return {"error": f"devrag search failed: {str(e)}"}


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available code intelligence tools."""
    return [
        Tool(
            name="repo_pack",
            description="Pack an entire repository into LLM-friendly format using Repomix. "
                        "Useful for providing full codebase context to LLMs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the repository to pack",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "xml", "plain"],
                        "default": "markdown",
                        "description": "Output format",
                    },
                    "include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Glob patterns to include (e.g., ['*.py', 'src/**/*'])",
                    },
                    "exclude": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Glob patterns to exclude (e.g., ['node_modules/**', '*.test.js'])",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="search_text",
            description="Search for text patterns in files using ripgrep. "
                        "Supports regex patterns and file type filtering.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (regex by default)",
                    },
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Path to search in",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "File type filter (e.g., 'py', 'js', 'ts')",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether search is case sensitive",
                    },
                    "context_lines": {
                        "type": "integer",
                        "default": 0,
                        "description": "Number of context lines before/after match",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 100,
                        "description": "Maximum number of results",
                    },
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="search_files",
            description="Search for files matching a glob pattern.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern for file names (e.g., '*.py', 'test_*.js')",
                    },
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Path to search in",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "File type filter",
                    },
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="analyze_structure",
            description="Analyze code structure using tree-sitter. "
                        "Extracts functions, classes, imports, and other structural elements.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to source file",
                    },
                    "code": {
                        "type": "string",
                        "description": "Source code string (alternative to file_path)",
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language (auto-detected from extension if not provided)",
                    },
                },
            },
        ),
        Tool(
            name="find_definitions",
            description="Find symbol definitions using Universal Ctags. "
                        "Locates where functions, classes, variables, etc. are defined.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name to search for",
                    },
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Path to search in",
                    },
                    "language": {
                        "type": "string",
                        "description": "Filter by language (e.g., 'Python', 'JavaScript')",
                    },
                    "exact_match": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to match symbol name exactly",
                    },
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="find_references",
            description="Find symbol references. "
                        "Locates where a symbol is used (excluding its definition).",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name to search for",
                    },
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Path to search in",
                    },
                    "language": {
                        "type": "string",
                        "description": "File type filter (e.g., 'py', 'js')",
                    },
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="get_symbols",
            description="Get all symbols in a file or directory. "
                        "Returns functions, classes, variables, and other symbols.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to analyze",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Filter by symbol kind (e.g., 'function', 'class', 'variable')",
                    },
                    "language": {
                        "type": "string",
                        "description": "Filter by language",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="get_function_at_line",
            description="Get the function containing a specific line number.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to source file",
                    },
                    "line_number": {
                        "type": "integer",
                        "description": "Line number (1-indexed)",
                    },
                },
                "required": ["file_path", "line_number"],
            },
        ),
        Tool(
            name="query",
            description="Intelligent code query with automatic tool selection. "
                        "Creates an execution plan based on intent, "
                        "runs appropriate tools, and integrates results. "
                        "Use this for complex questions about code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural language question about the code",
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["IMPLEMENT", "MODIFY", "INVESTIGATE", "QUESTION"],
                        "description": "Intent type: IMPLEMENT (new code), MODIFY (change existing), "
                                       "INVESTIGATE (understand code), QUESTION (general question). "
                                       "Determines tool selection strategy.",
                    },
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Path to search in",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Target symbol name (if applicable)",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Specific file to analyze (if applicable)",
                    },
                    "show_plan": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to show the execution plan",
                    },
                },
                "required": ["question"],
            },
        ),
        # v3.2: Session management tools for phase-gated execution
        # v3.6: Updated with QueryFrame support
        Tool(
            name="start_session",
            description="Start a new code implementation session with phase-gated execution. "
                        "v3.6: Returns extraction prompt for QueryFrame. "
                        "After calling this, extract QueryFrame using the prompt, then call set_query_frame.",
            inputSchema={
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": ["IMPLEMENT", "MODIFY", "INVESTIGATE", "QUESTION"],
                        "description": "The intent type for this session",
                    },
                    "query": {
                        "type": "string",
                        "description": "The user's original request",
                    },
                },
                "required": ["intent", "query"],
            },
        ),
        # v3.6: Set QueryFrame from LLM extraction
        Tool(
            name="set_query_frame",
            description="v3.6: Set the QueryFrame for the current session. "
                        "LLM extracts slots using the prompt from start_session, "
                        "server validates and creates QueryFrame. "
                        "Each slot must have 'value' and 'quote' (original text from query).",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_feature": {
                        "type": ["object", "null"],
                        "properties": {
                            "value": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "description": "Target feature/module (e.g., 'login function')",
                    },
                    "trigger_condition": {
                        "type": ["object", "null"],
                        "properties": {
                            "value": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "description": "Trigger condition (e.g., 'when XXX is input')",
                    },
                    "observed_issue": {
                        "type": ["object", "null"],
                        "properties": {
                            "value": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "description": "Observed issue (e.g., 'error occurs')",
                    },
                    "desired_action": {
                        "type": ["object", "null"],
                        "properties": {
                            "value": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "description": "Desired action (e.g., 'fix XXX')",
                    },
                },
            },
        ),
        Tool(
            name="get_session_status",
            description="Get the current session status including phase and allowed tools.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="submit_understanding",
            description="Submit exploration results to complete Phase 1 (EXPLORATION). "
                        "v3.3: Confidence is calculated by SERVER, not from LLM input. "
                        "Server evaluates results (symbols, entry_points, tools_used) "
                        "and determines if SEMANTIC (devrag) is needed or can proceed to READY.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbols_identified": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of key symbols found (classes, functions, etc.)",
                    },
                    "entry_points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of entry points identified",
                    },
                    "existing_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of existing patterns found (e.g., 'Service + Repository')",
                    },
                    "files_analyzed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of files that were analyzed",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes about the exploration",
                    },
                },
                "required": ["symbols_identified", "entry_points", "files_analyzed"],
            },
        ),
        Tool(
            name="submit_semantic",
            description="Submit devrag results to complete Phase 2 (SEMANTIC). "
                        "Required after using devrag. Must include hypotheses and reason. "
                        "v3.5: hypotheses now include confidence for improvement analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hypotheses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": "Hypothesis text",
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "default": "medium",
                                    "description": "Confidence level of this hypothesis",
                                },
                            },
                            "required": ["text"],
                        },
                        "description": "List of hypotheses with confidence levels",
                    },
                    "devrag_reason": {
                        "type": "string",
                        "enum": [
                            "no_definition_found",
                            "no_reference_found",
                            "no_similar_implementation",
                            "architecture_unknown",
                            "context_fragmented",
                        ],
                        "description": "Why semantic search was needed",
                    },
                    "search_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Queries used for devrag search",
                    },
                },
                "required": ["hypotheses", "devrag_reason"],
            },
        ),
        Tool(
            name="submit_verification",
            description="Submit verification results to complete Phase 3 (VERIFICATION). "
                        "v3.3: Evidence must be STRUCTURED with tool, target, and result. "
                        "This prevents 'fake' verification claims.",
            inputSchema={
                "type": "object",
                "properties": {
                    "verified": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "hypothesis": {
                                    "type": "string",
                                    "description": "The hypothesis being verified",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["confirmed", "rejected"],
                                },
                                "evidence": {
                                    "type": "object",
                                    "properties": {
                                        "tool": {
                                            "type": "string",
                                            "enum": [
                                                "find_definitions", "find_references",
                                                "search_text", "analyze_structure", "query",
                                            ],
                                            "description": "Tool used for verification",
                                        },
                                        "target": {
                                            "type": "string",
                                            "description": "Symbol/file/pattern verified",
                                        },
                                        "result": {
                                            "type": "string",
                                            "description": "Summary of tool result",
                                        },
                                        "files": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "Related file paths",
                                        },
                                    },
                                    "required": ["tool", "target", "result"],
                                },
                            },
                            "required": ["hypothesis", "status", "evidence"],
                        },
                        "description": "List of verified hypotheses with structured evidence",
                    },
                },
                "required": ["verified"],
            },
        ),
        # v3.4: Write target validation
        Tool(
            name="check_write_target",
            description="v3.4: Check if a file can be written to in READY phase. "
                        "Files must have been explored (in files_analyzed or verification evidence). "
                        "Prevents writing to unexplored code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to write",
                    },
                    "allow_new_files": {
                        "type": "boolean",
                        "default": True,
                        "description": "Allow creating new files in explored directories",
                    },
                },
                "required": ["file_path"],
            },
        ),
        # v3.5: Outcome logging for improvement cycle
        Tool(
            name="record_outcome",
            description="v3.5: Record session outcome (success/failure/partial) for improvement analysis. "
                        "Called by /outcome skill when human recognizes success or failure. "
                        "Observer only: records, does not intervene or change behavior.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to record outcome for",
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["success", "failure", "partial"],
                        "description": "Outcome type",
                    },
                    "analysis": {
                        "type": "object",
                        "properties": {
                            "root_cause": {
                                "type": "string",
                                "description": "What went wrong / what succeeded",
                            },
                            "failure_point": {
                                "type": ["string", "null"],
                                "enum": ["EXPLORATION", "SEMANTIC", "VERIFICATION", "READY", None],
                                "description": "Phase where failure occurred",
                            },
                            "related_symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Symbols related to outcome",
                            },
                            "related_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Files related to outcome",
                            },
                            "user_feedback_summary": {
                                "type": "string",
                                "description": "Summary of user's feedback",
                            },
                        },
                        "required": ["root_cause"],
                    },
                    "trigger_message": {
                        "type": "string",
                        "description": "The user message that triggered /outcome",
                    },
                },
                "required": ["session_id", "outcome", "analysis"],
            },
        ),
        Tool(
            name="get_outcome_stats",
            description="v3.5: Get statistics about session outcomes for improvement analysis. "
                        "Shows breakdown by intent, phase, devrag usage, and confidence.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # v3.7: LLM委譲による関連性判定
        Tool(
            name="validate_symbol_relevance",
            description="v3.7: Validate relevance between natural language term and code symbols. "
                        "Returns a validation prompt for LLM to determine relevance with code_evidence. "
                        "LLM must explain WHY symbols are related using code evidence (method names, comments, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_feature": {
                        "type": "string",
                        "description": "Natural language term for the target feature (e.g., 'ログイン機能')",
                    },
                    "symbols_identified": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of code symbols found during exploration",
                    },
                },
                "required": ["target_feature", "symbols_identified"],
            },
        ),
    ]


def check_phase_access(tool_name: str) -> dict | None:
    """
    v3.2: Check if tool is allowed in current session phase.

    Returns None if allowed, or error dict if blocked.
    """
    session = session_manager.get_active_session()

    # No active session - allow all tools (backward compatibility)
    if session is None:
        return None

    # Check if tool is allowed
    if session.is_tool_allowed(tool_name):
        return None

    # Tool is blocked
    return {
        "error": "phase_blocked",
        "message": session.get_blocked_reason(tool_name),
        "current_phase": session.phase.name,
        "allowed_tools": session.get_allowed_tools(),
        "hint": "Use get_session_status to see current phase requirements.",
    }


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute a code intelligence tool."""

    result = None

    # v3.2: Session management tools (no phase check needed)
    # v3.6: Updated with QueryFrame support
    if name == "start_session":
        intent = arguments["intent"]
        query = arguments["query"]
        session = session_manager.create_session(intent=intent, query=query)

        # v3.6: Get extraction prompt for QueryFrame
        extraction_prompt = QueryDecomposer.get_extraction_prompt(query)

        result = {
            "success": True,
            "session_id": session.session_id,
            "intent": session.intent,
            "current_phase": session.phase.name,
            "allowed_tools": session.get_allowed_tools(),
            "message": f"Session started. Phase: {session.phase.name}",
            # v3.6: QueryFrame extraction
            "query_frame": {
                "status": "pending",
                "extraction_prompt": extraction_prompt,
                "next_step": "Extract slots from query using the prompt, then call set_query_frame.",
            },
        }
        if session.phase == Phase.EXPLORATION:
            result["exploration_hint"] = (
                "After setting QueryFrame, use code-intel tools to fill missing slots. "
                "Then call submit_understanding."
            )
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "set_query_frame":
        # v3.6: Set QueryFrame from LLM extraction
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session. Use start_session first."}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        raw_query = session.query

        # Validate and extract each slot
        frame = QueryFrame(raw_query=raw_query)
        validation_errors = []
        validated_slots = []

        slot_names = ["target_feature", "trigger_condition", "observed_issue", "desired_action"]
        for slot_name in slot_names:
            slot_data = arguments.get(slot_name)
            if slot_data is not None:
                # validate_slot returns (value, quote) on success, (None, None) on failure
                value, validated_quote = validate_slot(slot_name, slot_data, raw_query)
                if value is None:
                    # Validation failed - quote not found or semantically inconsistent
                    provided_quote = slot_data.get("quote", "")
                    validation_errors.append({
                        "slot": slot_name,
                        "error": f"quote '{provided_quote}' not found in query or semantically inconsistent"
                    })
                else:
                    setattr(frame, slot_name, value)
                    frame.slot_quotes[slot_name] = validated_quote or ""
                    frame.slot_source[slot_name] = SlotSource.FACT  # From query = FACT
                    validated_slots.append(slot_name)

        if validation_errors:
            result = {
                "success": False,
                "error": "validation_failed",
                "validation_errors": validation_errors,
                "message": "Some slot validations failed. Check quotes match original query.",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # Set frame on session
        session.query_frame = frame

        # Assess risk level
        risk_level = assess_risk_level(frame, session.intent)
        session.risk_level = risk_level

        # Get missing slots and investigation guidance
        missing_slots = frame.get_missing_slots()
        guidance = generate_investigation_guidance(missing_slots)

        result = {
            "success": True,
            "query_frame": {
                "raw_query": raw_query,
                "target_feature": frame.target_feature,
                "trigger_condition": frame.trigger_condition,
                "observed_issue": frame.observed_issue,
                "desired_action": frame.desired_action,
                "validated_slots": validated_slots,
                "slot_sources": {k: v.value for k, v in frame.slot_source.items()},
            },
            "risk_level": risk_level,
            "missing_slots": missing_slots,
            "investigation_guidance": guidance,
            "message": f"QueryFrame set. Risk level: {risk_level}. Missing slots: {missing_slots}",
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "get_session_status":
        session = session_manager.get_active_session()
        if session is None:
            result = {
                "error": "no_active_session",
                "message": "No active session. Use start_session first.",
            }
        else:
            result = session.get_status()
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "submit_understanding":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            # v3.3: confidence は入力から削除。サーバー側で算出する。
            exploration = ExplorationResult(
                symbols_identified=arguments.get("symbols_identified", []),
                entry_points=arguments.get("entry_points", []),
                existing_patterns=arguments.get("existing_patterns", []),
                files_analyzed=arguments.get("files_analyzed", []),
                tools_used=[tc["tool"] for tc in session.tool_calls],
                notes=arguments.get("notes", ""),
            )
            result = session.submit_exploration(exploration)
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "submit_semantic":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            # v3.3: devrag_reason を文字列から DevragReason Enum に変換
            reason_str = arguments.get("devrag_reason")
            devrag_reason = None
            if reason_str:
                try:
                    devrag_reason = DevragReason(reason_str)
                except ValueError:
                    result = {
                        "error": "invalid_devrag_reason",
                        "message": f"Invalid devrag_reason: '{reason_str}'",
                        "valid_reasons": [r.value for r in DevragReason],
                    }
                    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # v3.5: hypotheses を Hypothesis オブジェクトに変換
            hypotheses_raw = arguments.get("hypotheses", [])
            hypotheses = []
            for h in hypotheses_raw:
                if isinstance(h, str):
                    # 後方互換性: 文字列の場合は medium confidence
                    hypotheses.append(Hypothesis(text=h, confidence="medium"))
                elif isinstance(h, dict):
                    hypotheses.append(Hypothesis(
                        text=h.get("text", ""),
                        confidence=h.get("confidence", "medium"),
                    ))

            semantic = SemanticResult(
                hypotheses=hypotheses,
                devrag_reason=devrag_reason,
                search_queries=arguments.get("search_queries", []),
            )
            result = session.submit_semantic(semantic)
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "submit_verification":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            # v3.3: 構造化された evidence を持つ VerifiedHypothesis に変換
            verified_list = []
            for v in arguments.get("verified", []):
                evidence_data = v.get("evidence", {})
                evidence = VerificationEvidence(
                    tool=evidence_data.get("tool", ""),
                    target=evidence_data.get("target", ""),
                    result=evidence_data.get("result", ""),
                    files=evidence_data.get("files", []),
                )
                verified_hypothesis = VerifiedHypothesis(
                    hypothesis=v.get("hypothesis", ""),
                    status=v.get("status", "rejected"),
                    evidence=evidence,
                )
                verified_list.append(verified_hypothesis)

            verification = VerificationResult(
                verified=verified_list,
                all_confirmed=all(vh.status == "confirmed" for vh in verified_list),
            )
            result = session.submit_verification(verification)
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "check_write_target":
        # v3.4: Check if file can be written
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            result = session.check_write_target(
                file_path=arguments["file_path"],
                allow_new_files=arguments.get("allow_new_files", True),
            )
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "record_outcome":
        # v3.5: Record session outcome
        session_id = arguments["session_id"]
        session = session_manager.get_session(session_id)

        # Get session context
        phase_at_outcome = "UNKNOWN"
        intent = "UNKNOWN"
        devrag_used = False
        confidence_was = ""

        if session:
            phase_at_outcome = session.phase.name
            intent = session.intent
            devrag_used = session.semantic is not None
            if session.exploration:
                confidence_was = session.exploration._evaluated_confidence

        # Build analysis
        analysis_data = arguments.get("analysis", {})
        analysis = OutcomeAnalysis(
            root_cause=analysis_data.get("root_cause", ""),
            failure_point=analysis_data.get("failure_point"),
            related_symbols=analysis_data.get("related_symbols", []),
            related_files=analysis_data.get("related_files", []),
            user_feedback_summary=analysis_data.get("user_feedback_summary", ""),
        )

        # Create outcome log
        outcome_log = OutcomeLog(
            session_id=session_id,
            outcome=arguments["outcome"],
            phase_at_outcome=phase_at_outcome,
            intent=intent,
            devrag_used=devrag_used,
            confidence_was=confidence_was,
            analysis=analysis,
            trigger_message=arguments.get("trigger_message", ""),
        )

        result = record_outcome(outcome_log)

        # v3.7: Cache successful pairs
        if arguments["outcome"] == "success" and session and session.query_frame:
            try:
                from tools.learned_pairs import cache_successful_pair
                qf = session.query_frame
                if qf.target_feature and qf.mapped_symbols:
                    cached_count = 0
                    for sym in qf.mapped_symbols:
                        cache_successful_pair(
                            nl_term=qf.target_feature,
                            symbol=sym.name,
                            similarity=sym.confidence,
                            code_evidence=sym.evidence.result_summary if sym.evidence else None,
                            session_id=session_id,
                        )
                        cached_count += 1
                    result["cached_pairs"] = cached_count
                    result["cache_note"] = f"{cached_count} pairs cached for future reference"
            except Exception as e:
                result["cache_error"] = str(e)

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "get_outcome_stats":
        # v3.5: Get outcome statistics
        result = get_failure_stats()
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "validate_symbol_relevance":
        # v3.7: LLM委譲 + Embedding検証のハイブリッド
        target_feature = arguments["target_feature"]
        symbols = arguments["symbols_identified"]

        # Build validation prompt for LLM
        symbols_list = "\n".join(f"- {s}" for s in symbols)
        validation_prompt = f"""以下のシンボル群から、対象機能「{target_feature}」に関連するものを選んでください。

シンボル一覧:
{symbols_list}

回答形式（JSON）:
{{
  "relevant_symbols": ["関連するシンボル名"],
  "reasoning": "選定理由",
  "code_evidence": "コード上の根拠（メソッド名、コメント、命名規則など）"
}}

※ code_evidence は必須。根拠なしの判定は無効。
※ 関連するシンボルがない場合は relevant_symbols を空配列に。"""

        result = {
            "validation_prompt": validation_prompt,
            "target_feature": target_feature,
            "symbols_count": len(symbols),
            "action_required": "LLMがこのプロンプトに回答し、set_query_frame で mapped_symbols を更新してください。",
            "response_schema": {
                "relevant_symbols": "array of string",
                "reasoning": "string (required)",
                "code_evidence": "string (required)",
            },
        }

        # v3.7: Check learned pairs cache first
        try:
            from tools.learned_pairs import find_cached_matches
            cached_matches = find_cached_matches(target_feature, symbols)
            if cached_matches:
                result["cached_matches"] = cached_matches
                result["cache_note"] = (
                    "cached_matches は過去に成功したペア。優先的に採用してください。"
                )
        except Exception as e:
            result["cache_status"] = f"unavailable: {str(e)}"

        # v3.7: Embedding-based suggestions (if available)
        try:
            from tools.embedding import get_embedding_validator, is_embedding_available
            if is_embedding_available():
                validator = get_embedding_validator()
                suggestions = validator.find_related_symbols(target_feature, symbols, top_k=5)
                result["embedding_suggestions"] = suggestions
                result["embedding_note"] = (
                    "embedding_suggestions はサーバーがベクトル類似度で算出した候補。"
                    "LLM判定の参考にしてください。similarity > 0.6 は高信頼。"
                )
        except Exception as e:
            result["embedding_status"] = f"unavailable: {str(e)}"

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    # v3.2: Check phase access for other tools
    phase_error = check_phase_access(name)
    if phase_error:
        return [TextContent(type="text", text=json.dumps(phase_error, indent=2, ensure_ascii=False))]

    # Record tool call in session
    session = session_manager.get_active_session()

    if name == "repo_pack":
        result = await pack_repository(
            path=arguments["path"],
            output_format=arguments.get("format", "markdown"),
            include_patterns=arguments.get("include"),
            exclude_patterns=arguments.get("exclude"),
        )

    elif name == "search_text":
        result = await search_text(
            pattern=arguments["pattern"],
            path=arguments.get("path", "."),
            file_type=arguments.get("file_type"),
            case_sensitive=arguments.get("case_sensitive", True),
            context_lines=arguments.get("context_lines", 0),
            max_results=arguments.get("max_results", 100),
        )

    elif name == "search_files":
        result = await search_files(
            pattern=arguments["pattern"],
            path=arguments.get("path", "."),
            file_type=arguments.get("file_type"),
        )

    elif name == "analyze_structure":
        # This is a sync function, run in executor
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: analyze_structure(
                file_path=arguments.get("file_path"),
                code=arguments.get("code"),
                language=arguments.get("language"),
            )
        )

    elif name == "find_definitions":
        result = await find_definitions(
            symbol=arguments["symbol"],
            path=arguments.get("path", "."),
            language=arguments.get("language"),
            exact_match=arguments.get("exact_match", False),
        )

    elif name == "find_references":
        result = await find_references(
            symbol=arguments["symbol"],
            path=arguments.get("path", "."),
            language=arguments.get("language"),
        )

    elif name == "get_symbols":
        result = await get_symbols(
            path=arguments["path"],
            kind=arguments.get("kind"),
            language=arguments.get("language"),
        )

    elif name == "get_function_at_line":
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_function_at_line(
                file_path=arguments["file_path"],
                line_number=arguments["line_number"],
            )
        )

    elif name == "query":
        result = await execute_query(
            question=arguments["question"],
            path=arguments.get("path", "."),
            symbol=arguments.get("symbol"),
            file_path=arguments.get("file_path"),
            show_plan=arguments.get("show_plan", True),
            intent=arguments.get("intent"),  # v3.2: Pass intent from caller
        )

    else:
        result = {"error": f"Unknown tool: {name}"}

    # v3.2: Record tool call in session
    # v3.5: Add result_detail for improvement analysis
    if session is not None and result is not None:
        result_summary = ""
        result_detail = {}  # v3.5

        if isinstance(result, dict):
            if "error" in result:
                result_summary = f"error: {result['error']}"
                result_detail = {"status": "error", "error_type": result.get("error", "unknown")}
            elif "matches" in result:
                count = len(result["matches"])
                result_summary = f"{count} matches"
                result_detail = {
                    "status": "found" if count > 0 else "no_match",
                    "hit_count": count,
                }
            elif "definitions" in result:
                count = len(result["definitions"])
                result_summary = f"{count} definitions"
                result_detail = {
                    "status": "found" if count > 0 else "no_match",
                    "hit_count": count,
                }
            elif "references" in result:
                count = len(result["references"])
                result_summary = f"{count} references"
                result_detail = {
                    "status": "found" if count > 0 else "no_match",
                    "hit_count": count,
                }
            elif "symbols" in result:
                count = len(result["symbols"])
                result_summary = f"{count} symbols"
                result_detail = {
                    "status": "found" if count > 0 else "no_match",
                    "hit_count": count,
                }
            else:
                result_summary = "completed"
                result_detail = {"status": "completed"}

        session.record_tool_call(name, arguments, result_summary, result_detail)

    # Format result as JSON
    return [TextContent(
        type="text",
        text=json.dumps(result, indent=2, ensure_ascii=False),
    )]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())

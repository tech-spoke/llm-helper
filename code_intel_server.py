#!/usr/bin/env python3
"""
Code Intelligence MCP Server v1.3

Provides Cursor-like code intelligence capabilities using open source tools:
- ripgrep: Fast text search
- tree-sitter: Code structure analysis
- ctags: Symbol definitions and references
- ChromaDB: Semantic search (Forest/Map architecture)

Key Features:
- Phase-gated execution: EXPLORATION → SEMANTIC → VERIFICATION → READY
- Server-side confidence calculation (no LLM self-reporting)
- QueryFrame for structured natural language processing
- Forest/Map architecture for semantic search
- Improvement cycle with DecisionLog + OutcomeLog

v1.1 Additions:
- Essential context provision (design docs + project rules)
- Impact analysis before READY phase
- Markup context relaxation
"""

import asyncio
import json
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from tools.ripgrep_tool import search_text, search_files
from tools.treesitter_tool import analyze_structure, get_function_at_line
from tools.ctags_tool import find_definitions, find_references, get_symbols
from tools.router import Router, QuestionCategory, UnifiedResult, DecisionLog, FallbackDecision
from tools.session import (
    SessionManager, SessionState, Phase,
    ExplorationResult, SemanticResult, VerificationResult, SemanticReason,
    VerificationEvidence, VerifiedHypothesis,
    Hypothesis,
    IntentReclassificationRequired,
    InvalidSemanticReason, WriteTargetBlocked,
)
from tools.outcome_log import (
    OutcomeLog, OutcomeAnalysis, record_outcome,
    get_outcomes_for_session, get_failure_stats,
    record_decision, get_decision_for_session,
    get_session_analysis, get_improvement_insights,
)
from tools.query_frame import (
    QueryFrame, QueryDecomposer, SlotSource, SlotEvidence,
    validate_slot, generate_investigation_guidance,
)
from tools.context_provider import ContextProvider, get_summary_prompts
from tools.impact_analyzer import analyze_impact
from tools.chromadb_manager import (
    ChromaDBManager, SearchResult, SearchHit,
    CHROMADB_AVAILABLE,
)
from tools.branch_manager import BranchManager


# Create MCP server, router, and session manager
router = Router()
server = Server("code-intel")
session_manager = SessionManager()

# ChromaDB manager cache (per project)
_chromadb_managers: dict[str, ChromaDBManager] = {}

# v1.7: Ctags cache managers (per project)
_ctags_cache_managers: dict[str, "CtagsCacheManager"] = {}

# v1.2.1: Branch manager cache (per session) - OverlayFS removed, git branch only
_branch_managers: dict[str, BranchManager] = {}


def _get_or_recreate_branch_manager(session, repo_path: str) -> BranchManager | None:
    """
    Get branch manager from cache, or recreate it from session state.

    This handles server restart recovery where the in-memory cache is lost
    but session state is persisted.
    """
    import subprocess

    # Try cache first
    branch_manager = _branch_managers.get(session.session_id)
    if branch_manager is not None:
        return branch_manager

    # Recreate from session state
    if not session.task_branch_name:
        return None

    branch_manager = BranchManager(repo_path)
    branch_manager._active_session = session.session_id
    branch_manager._branch_name = session.task_branch_name

    # Determine base branch
    try:
        result_proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        current = result_proc.stdout.strip()

        if current.startswith("llm_task_"):
            for base_candidate in ["main", "master", "develop"]:
                try:
                    mb_result = subprocess.run(
                        ["git", "merge-base", base_candidate, "HEAD"],
                        cwd=repo_path,
                        capture_output=True,
                        text=True,
                    )
                    if mb_result.returncode == 0:
                        branch_manager._base_branch = base_candidate
                        break
                except Exception:
                    pass
            if not branch_manager._base_branch:
                branch_manager._base_branch = "main"
        else:
            branch_manager._base_branch = current
    except Exception:
        branch_manager._base_branch = "main"

    _branch_managers[session.session_id] = branch_manager
    return branch_manager


def get_chromadb_manager(project_root: str = ".") -> ChromaDBManager:
    """Get or create ChromaDBManager for a project."""
    if not CHROMADB_AVAILABLE:
        raise RuntimeError("chromadb is not installed. Install with: pip install chromadb")

    project_path = Path(project_root).resolve()
    key = str(project_path)

    if key not in _chromadb_managers:
        _chromadb_managers[key] = ChromaDBManager(project_path)

    return _chromadb_managers[key]


def get_ctags_cache_manager(project_root: str = ".") -> "CtagsCacheManager":
    """Get or create CtagsCacheManager for a project."""
    from tools.ctags_cache import CtagsCacheManager

    project_path = Path(project_root).resolve()
    key = str(project_path)

    if key not in _ctags_cache_managers:
        _ctags_cache_managers[key] = CtagsCacheManager(project_path)

    return _ctags_cache_managers[key]


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

    v3.7: Updated to use QueryFrame-based routing.
    This function creates a simple QueryFrame from the question and executes
    the planned tools.

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

    # v3.7: Create QueryFrame from question
    query_frame = QueryFrame(raw_query=question)
    if symbol:
        query_frame.target_feature = symbol

    # Resolve intent
    resolved_intent = intent or "INVESTIGATE"

    # Create execution plan (v3.7: pass QueryFrame and intent)
    plan = router.create_plan(query_frame, resolved_intent, context)

    output = {
        "question": question,
        "intent": plan.intent.name,
        "reasoning": plan.reasoning,
        "needs_bootstrap": plan.needs_bootstrap,
        "risk_level": plan.risk_level,
        "missing_slots": plan.missing_slots,
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

    # Execute planned steps
    for step in plan.steps:
        step_result = await execute_tool_step(step.tool, step.params, context)

        # v1.8: 出力サイズ削減のため、raw_resultの代わりにsummaryのみ返す
        result_summary = "no results"
        if isinstance(step_result, dict):
            if "matches" in step_result:
                result_summary = f"{len(step_result['matches'])} matches"
            elif "results" in step_result:
                result_summary = f"{len(step_result['results'])} results"
            elif "definitions" in step_result:
                result_summary = f"{len(step_result['definitions'])} definitions"
            elif "references" in step_result:
                result_summary = f"{len(step_result['references'])} references"
            elif "symbols" in step_result:
                result_summary = f"{len(step_result['symbols'])} symbols"

        step_outputs.append({
            "tool": step.tool,
            "phase": "query",
            "priority": step.priority,
            "summary": result_summary,
        })

        # Collect results (simple normalization)
        if isinstance(step_result, dict) and "matches" in step_result:
            for match in step_result.get("matches", []):
                all_results.append(UnifiedResult(
                    file_path=match.get("file", ""),
                    symbol_name=match.get("symbol"),
                    start_line=match.get("line", 0),
                    end_line=match.get("end_line"),
                    content_snippet=match.get("content", "")[:200],
                    source_tool=step.tool,
                    confidence=1.0,
                ))

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
        for r in all_results
    ]
    output["total_results"] = len(all_results)
    output["step_outputs"] = step_outputs

    # v3.7: Include decision log for observability
    # DISABLED: Decision log disabled for performance (v3.10 feature)
    # if plan.decision_log:
    #     decision_dict = plan.decision_log.to_dict()
    #
    #     # Add session_id if there's an active session
    #     active_session = session_manager.get_active_session()
    #     if active_session:
    #         decision_dict["session_id"] = active_session.session_id
    #
    #     # Persist decision log
    #     record_decision(decision_dict)
    #
    #     output["decision_log"] = decision_dict

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

    elif tool == "semantic_search":
        # Use ChromaDB for semantic search
        question = context.get("question", "")
        if CHROMADB_AVAILABLE:
            try:
                manager = get_chromadb_manager(path)
                result = manager.search(question)
                return {
                    "source": result.source,
                    "results": [h.to_dict() for h in result.hits],
                    "skip_forest": result.skip_forest,
                    "confidence": result.confidence,
                    "engine": "chromadb",
                }
            except Exception as e:
                return {"error": f"Semantic search failed: {str(e)}"}
        else:
            return {
                "error": "chromadb_not_available",
                "message": "chromadb is not installed. Install with: pip install chromadb",
            }

    return {"error": f"Unknown tool: {tool}"}


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available code intelligence tools."""
    return [
        Tool(
            name="search_text",
            description="Search for text patterns in files using ripgrep. "
                        "Supports regex patterns and file type filtering. "
                        "Can search multiple patterns in parallel (max 5).",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 5
                            }
                        ],
                        "description": "Single pattern (string) or multiple patterns (array of strings, max 5 for parallel search)",
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
        Tool(
            name="fetch_chunk_detail",
            description="v1.8: Fetch detailed content of a specific ChromaDB chunk by ID. "
                        "Use this when semantic_search returns chunk IDs and you need full content. "
                        "Enables gradual retrieval to avoid large outputs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chunk_id": {
                        "type": "string",
                        "description": "ChromaDB chunk ID (from semantic_search results)",
                    },
                    "path": {
                        "type": "string",
                        "default": ".",
                        "description": "Project root path",
                    },
                },
                "required": ["chunk_id"],
            },
        ),
        # Session management tools for phase-gated execution
        Tool(
            name="start_session",
            description="Start a new code implementation session with phase-gated execution. "
                        "v1.6: Preparation phase only. Branch creation moved to begin_phase_gate. "
                        "After calling this, call begin_phase_gate to start phase gates. "
                        "v1.10: gate_level='full' forces all phases, 'auto' checks necessity before each.",
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
                    "repo_path": {
                        "type": "string",
                        "description": "Project root path for agreements and learned_pairs (default: '.')",
                        "default": ".",
                    },
                    "gate_level": {
                        "type": "string",
                        "enum": ["full", "auto"],
                        "description": "v1.10: Gate level for phase checks. 'full' forces all phases, 'auto' checks necessity before each phase.",
                        "default": "auto",
                    },
                    "skip_quality": {
                        "type": "boolean",
                        "description": "v1.5: Skip QUALITY_REVIEW phase (--no-quality flag)",
                        "default": False,
                    },
                    "skip_implementation": {
                        "type": "boolean",
                        "description": "v1.8: Skip implementation phase (--only-explore flag). Exploration will run but implementation will be skipped.",
                        "default": False,
                    },
                },
                "required": ["intent", "query"],
            },
        ),
        # v1.6: Phase gate start (separated from start_session)
        Tool(
            name="begin_phase_gate",
            description="v1.6: Start phase gate and create task branch. Call after start_session. "
                        "If stale branches exist, returns error with recovery options (user must decide). "
                        "Use skip_branch=true for --quick mode. "
                        "Use resume_current=true to continue on current branch (for 'Continue as-is' option).",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from start_session",
                    },
                    "skip_branch": {
                        "type": "boolean",
                        "description": "Skip branch creation (for --quick mode). Transitions directly to READY.",
                        "default": False,
                    },
                    "resume_current": {
                        "type": "boolean",
                        "description": "Continue on current branch without creating new one (for 'Continue as-is' option). "
                                       "If on llm_task_* branch, continues there. Otherwise creates new branch and ignores stale branches.",
                        "default": False,
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="set_query_frame",
            description="Set the QueryFrame for the current session. "
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
        # v1.10 note: submit_understanding removed (replaced by check_phase_necessity)
        # LLM should use check_phase_necessity after exploration to determine next phase
        Tool(
            name="submit_semantic",
            description="Submit semantic search results to complete Phase 2 (SEMANTIC). "
                        "Required after using semantic_search. Must include hypotheses and reason.",
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
                    "semantic_reason": {
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
                        "description": "Queries used for semantic search",
                    },
                },
                "required": ["hypotheses", "semantic_reason"],
            },
        ),
        Tool(
            name="check_phase_necessity",
            description="[v1.10] Check if a phase is necessary before executing it. "
                        "Used for Q1 (SEMANTIC), Q2 (VERIFICATION), Q3 (IMPACT_ANALYSIS) checks. "
                        "LLM decides necessity based on actual code inspection results. "
                        "gate_level='full' forces execution, 'auto' respects assessment.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phase": {
                        "type": "string",
                        "enum": ["SEMANTIC", "VERIFICATION", "IMPACT_ANALYSIS"],
                        "description": "Phase to check necessity for",
                    },
                    "assessment": {
                        "type": ["object", "string"],
                        "description": "Necessity assessment for the phase (object or JSON string)",
                        "anyOf": [
                            {
                                "description": "Q1: SEMANTIC necessity check",
                                "properties": {
                                    "needs_more_information": {
                                        "type": "boolean",
                                        "description": "true: need additional information, false: information sufficient",
                                    },
                                    "needs_more_information_reason": {
                                        "type": "string",
                                        "description": "Reason for needing (or not needing) more information",
                                    },
                                },
                                "required": ["needs_more_information", "needs_more_information_reason"],
                            },
                            {
                                "description": "Q2: VERIFICATION necessity check",
                                "properties": {
                                    "has_unverified_hypotheses": {
                                        "type": "boolean",
                                        "description": "true: have unverified hypotheses, false: no hypotheses or already verified",
                                    },
                                    "has_unverified_hypotheses_reason": {
                                        "type": "string",
                                        "description": "Reason for having (or not having) unverified hypotheses",
                                    },
                                },
                                "required": ["has_unverified_hypotheses", "has_unverified_hypotheses_reason"],
                            },
                            {
                                "description": "Q3: IMPACT_ANALYSIS necessity check",
                                "properties": {
                                    "needs_impact_analysis": {
                                        "type": "boolean",
                                        "description": "true: need impact analysis, false: impact already confirmed",
                                    },
                                    "needs_impact_analysis_reason": {
                                        "type": "string",
                                        "description": "Reason for needing (or not needing) impact analysis",
                                    },
                                },
                                "required": ["needs_impact_analysis", "needs_impact_analysis_reason"],
                            },
                        ],
                    },
                },
                "required": ["phase", "assessment"],
            },
        ),
        Tool(
            name="submit_verification",
            description="Submit verification results to complete Phase 3 (VERIFICATION). "
                        "Evidence must be STRUCTURED with tool, target, and result. "
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
        Tool(
            name="check_write_target",
            description="Check if a file can be written to in READY phase. "
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
        Tool(
            name="add_explored_files",
            description="Add files/directories to explored list in READY phase. "
                        "Use when check_write_target blocks a write to an unexplored location. "
                        "Lightweight recovery without reverting to EXPLORATION phase.",
            inputSchema={
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths or directory paths to add to explored list",
                    },
                },
                "required": ["files"],
            },
        ),
        Tool(
            name="revert_to_exploration",
            description="Revert from any phase back to EXPLORATION phase. "
                        "Use when additional exploration is needed after reaching READY phase. "
                        "Previous exploration results are kept by default for incremental exploration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "keep_results": {
                        "type": "boolean",
                        "default": True,
                        "description": "Keep existing exploration results (default: true)",
                    },
                },
            },
        ),
        # v1.2: PRE_COMMIT phase tools for garbage detection
        Tool(
            name="submit_for_review",
            description="Transition from READY to PRE_COMMIT phase for garbage detection. "
                        "Call this after implementation is complete to review changes before commit. "
                        "Requires task branch to be enabled.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="review_changes",
            description="Get all changes captured in the task branch for garbage review. "
                        "Returns list of changed files with diffs for LLM to review.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="finalize_changes",
            description="Apply reviewed changes and commit to task branch. "
                        "Call after review_changes with decisions about which files to keep/discard.",
            inputSchema={
                "type": "object",
                "properties": {
                    "reviewed_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "decision": {"type": "string", "enum": ["keep", "discard"]},
                                "reason": {"type": "string"},
                            },
                            "required": ["path", "decision"],
                        },
                        "description": "List of file decisions. Discard requires reason.",
                    },
                    "commit_message": {
                        "type": "string",
                        "description": "Commit message for the changes",
                    },
                },
                "required": ["reviewed_files"],
            },
        ),
        # v1.5: Quality Review
        Tool(
            name="submit_quality_review",
            description="v1.5: Report quality review results. "
                        "Call after reviewing changes in QUALITY_REVIEW phase. "
                        "If issues found, reverts to READY phase for fixes. "
                        "If no issues, allows proceed to merge_to_base.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issues_found": {
                        "type": ["boolean", "string"],
                        "description": "Whether any quality issues were found (true/false or 'true'/'false')",
                    },
                    "issues": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of issues found (required if issues_found=true). Each item should be 'description in file:line' format.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes or comments",
                    },
                },
                "required": ["issues_found"],
            },
        ),
        Tool(
            name="merge_to_base",
            description="Merge task branch back to the base branch (where session started). "
                        "Automatically uses the branch that was active when start_session was called. "
                        "Call after finalize_changes (or submit_quality_review if enabled) to complete the workflow.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="cleanup_stale_branches",
            description="Clean up stale task branches from interrupted runs. "
                        "Use when task branches remain after session interruption. "
                        "action='delete' removes branches, action='merge' merges to base then removes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Repository path to clean up (default: current directory)",
                        "default": ".",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["delete", "merge"],
                        "description": "Action to take: 'delete' removes branches, 'merge' merges to base branch then removes",
                        "default": "delete",
                    },
                },
            },
        ),
        Tool(
            name="record_outcome",
            description="Record session outcome (success/failure/partial) for improvement analysis. "
                        "Also called automatically by /code when failure is detected. "
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
                    "phase_at_outcome": {
                        "type": "string",
                        "enum": ["EXPLORATION", "SEMANTIC", "VERIFICATION", "READY"],
                        "description": "Phase at outcome (optional, auto-detected from session if available)",
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["IMPLEMENT", "MODIFY", "INVESTIGATE", "QUESTION"],
                        "description": "Intent type (optional, auto-detected from session if available)",
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
            description="Get statistics about session outcomes for improvement analysis. "
                        "Shows breakdown by intent, phase, semantic search usage, and confidence.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # v1.4: Intervention System Tools
        Tool(
            name="record_verification_failure",
            description="Record a verification failure for intervention system tracking. "
                        "Call this when POST_IMPLEMENTATION_VERIFICATION fails. "
                        "After 3 failures, intervention is triggered with guidance to select appropriate intervention prompt.",
            inputSchema={
                "type": "object",
                "properties": {
                    "error_message": {
                        "type": "string",
                        "description": "What differed from expectation (e.g., 'logo not displayed, color still #ff0000')",
                    },
                    "problem_location": {
                        "type": "string",
                        "description": "Where the problem was found (e.g., 'inside header element, .btn-primary class')",
                    },
                    "observed_values": {
                        "type": "string",
                        "description": "Specific values observed (e.g., 'actual color is #333333, element not found')",
                    },
                },
                "required": ["error_message", "problem_location", "observed_values"],
            },
        ),
        Tool(
            name="record_intervention_used",
            description="Record that an intervention prompt was used. "
                        "Call this after reading and following an intervention prompt from .code-intel/interventions/.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt_name": {
                        "type": "string",
                        "description": "Name of intervention prompt used (e.g., 'step_back', 'user_escalation')",
                    },
                },
                "required": ["prompt_name"],
            },
        ),
        Tool(
            name="get_intervention_status",
            description="Get current intervention system status. "
                        "Shows verification failure count, intervention count, and whether escalation is required.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="validate_symbol_relevance",
            description="Validate relevance between natural language term and code symbols. "
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
        Tool(
            name="confirm_symbol_relevance",
            description="Confirm symbol relevance after LLM validation. "
                        "Updates mapped_symbols confidence based on embedding similarity and LLM's code_evidence. "
                        "Call this after validate_symbol_relevance with LLM's response.",
            inputSchema={
                "type": "object",
                "properties": {
                    "relevant_symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Symbols that LLM confirmed as relevant",
                    },
                    "code_evidence": {
                        "type": "string",
                        "description": "Code evidence explaining why symbols are related (required)",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "LLM's reasoning for the selection",
                    },
                },
                "required": ["relevant_symbols", "code_evidence"],
            },
        ),
        Tool(
            name="sync_index",
            description="Sync source code to ChromaDB index. "
                        "Uses AST-based chunking with fingerprint-based incremental sync. "
                        "Run this after code changes or at session start.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Project root path (default: current session's repo_path)",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Force full re-index (ignore fingerprints)",
                    },
                    "sync_map": {
                        "type": "boolean",
                        "default": True,
                        "description": "Also sync agreements to map collection",
                    },
                },
            },
        ),
        Tool(
            name="semantic_search",
            description="Semantic search using ChromaDB. "
                        "Searches map (agreements) first, then forest (code) if needed. "
                        "Short-circuits if map has high-confidence match (≥0.7).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "path": {
                        "type": "string",
                        "description": "Project root path (default: current session's repo_path)",
                    },
                    "target_feature": {
                        "type": "string",
                        "description": "Target feature from QueryFrame (optional, improves search)",
                    },
                    "collection": {
                        "type": "string",
                        "enum": ["auto", "map", "forest"],
                        "default": "auto",
                        "description": "Collection to search: auto (short-circuit), map only, forest only",
                    },
                    "n_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results",
                    },
                },
                "required": ["query"],
            },
        ),
        # v1.1: Impact analysis tool
        Tool(
            name="analyze_impact",
            description="Analyze impact of code changes before READY phase. "
                        "Detects direct references (callers, type hints) and naming convention matches "
                        "(tests, factories, seeders). Applies markup relaxation for style-only files. "
                        "LLM must verify must_verify files and declare verification results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to analyze for impact",
                    },
                    "change_description": {
                        "type": "string",
                        "description": "Description of the change being made (for inference hints)",
                    },
                },
                "required": ["target_files"],
            },
        ),
        # v1.1: Update context tool (save LLM-generated summaries)
        Tool(
            name="update_context",
            description="Save LLM-generated summaries to context.yml. "
                        "Call this after sync_index returns context_update_required with documents to summarize. "
                        "Pass the generated summaries for each document.",
            inputSchema={
                "type": "object",
                "properties": {
                    "design_doc_summaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "Document path"},
                                "file": {"type": "string", "description": "Filename"},
                                "summary": {"type": "string", "description": "Generated summary"},
                            },
                            "required": ["path", "summary"],
                        },
                        "description": "Summaries for design documents",
                    },
                    "project_rules_summary": {
                        "type": "string",
                        "description": "Generated summary for project rules (DO/DON'T format)",
                    },
                },
            },
        ),
        # v1.1: Submit impact analysis results
        Tool(
            name="submit_impact_analysis",
            description="Submit impact analysis verification results to complete IMPACT_ANALYSIS phase. "
                        "Validates that all must_verify files have responses with status and reason. "
                        "Must be called after analyze_impact before proceeding to READY phase.",
            inputSchema={
                "type": "object",
                "properties": {
                    "verified_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {
                                    "type": "string",
                                    "description": "File path that was verified",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["will_modify", "no_change_needed", "not_affected"],
                                    "description": "Verification status for this file",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Reason for status (required when status != will_modify)",
                                },
                            },
                            "required": ["file", "status"],
                        },
                        "description": "List of verified files with status and reason",
                    },
                    "inferred_from_rules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional files inferred from project_rules naming conventions",
                    },
                },
                "required": ["verified_files"],
            },
        ),
        # v1.10 note: submit_verification_and_impact removed (v1.9 integration reverted)
        # Use separate submit_verification and submit_impact_analysis instead
    ]


def _validate_phase_assessment(phase: str, assessment: dict) -> tuple[bool, str]:
    """
    Validate phase necessity assessment.

    Args:
        phase: "SEMANTIC", "VERIFICATION", or "IMPACT_ANALYSIS"
        assessment: Assessment dictionary

    Returns:
        (is_valid, error_message)
    """
    if phase == "SEMANTIC":
        if "needs_more_information" not in assessment:
            return False, "needs_more_information field is required for SEMANTIC assessment"
        if "needs_more_information_reason" not in assessment:
            return False, "needs_more_information_reason field is required for SEMANTIC assessment"
        if not isinstance(assessment["needs_more_information"], bool):
            return False, "needs_more_information must be a boolean"
        if len(assessment["needs_more_information_reason"]) < 10:
            return False, "needs_more_information_reason must be at least 10 characters"

    elif phase == "VERIFICATION":
        if "has_unverified_hypotheses" not in assessment:
            return False, "has_unverified_hypotheses field is required for VERIFICATION assessment"
        if "has_unverified_hypotheses_reason" not in assessment:
            return False, "has_unverified_hypotheses_reason field is required for VERIFICATION assessment"
        if not isinstance(assessment["has_unverified_hypotheses"], bool):
            return False, "has_unverified_hypotheses must be a boolean"
        if len(assessment["has_unverified_hypotheses_reason"]) < 10:
            return False, "has_unverified_hypotheses_reason must be at least 10 characters"

    elif phase == "IMPACT_ANALYSIS":
        if "needs_impact_analysis" not in assessment:
            return False, "needs_impact_analysis field is required for IMPACT_ANALYSIS assessment"
        if "needs_impact_analysis_reason" not in assessment:
            return False, "needs_impact_analysis_reason field is required for IMPACT_ANALYSIS assessment"
        if not isinstance(assessment["needs_impact_analysis"], bool):
            return False, "needs_impact_analysis must be a boolean"
        if len(assessment["needs_impact_analysis_reason"]) < 10:
            return False, "needs_impact_analysis_reason must be at least 10 characters"

    return True, ""


def _get_next_instruction(phase: str, phase_required: bool, next_phase: str) -> str:
    """
    Generate instruction for the next step after phase necessity check.

    Args:
        phase: The phase that was checked
        phase_required: Whether the phase is required
        next_phase: The next phase or checkpoint

    Returns:
        Instruction string
    """
    if phase_required:
        if phase == "SEMANTIC":
            return "Execute SEMANTIC phase: Use semantic_search to gather additional information, then call submit_semantic."
        elif phase == "VERIFICATION":
            return "Execute VERIFICATION phase: Verify hypotheses using code-intel tools, then call submit_verification."
        elif phase == "IMPACT_ANALYSIS":
            return "Execute IMPACT_ANALYSIS phase: Analyze impact range using analyze_impact, then call submit_impact_analysis."
    else:
        if next_phase == "Q2_CHECK":
            return "SEMANTIC skipped. Now check VERIFICATION necessity using check_phase_necessity(phase='VERIFICATION', assessment={...})."
        elif next_phase == "Q3_CHECK":
            return "VERIFICATION skipped. Now check IMPACT_ANALYSIS necessity using check_phase_necessity(phase='IMPACT_ANALYSIS', assessment={...})."
        elif next_phase == "READY":
            return "IMPACT_ANALYSIS skipped. Proceed to READY phase for implementation."

    return "Proceed to next step."


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

    # v1.8: Record tool call start for performance tracking
    session = session_manager.get_active_session()
    if session is not None and name not in ["start_session", "get_session_status"]:
        session.record_tool_call_start(name, arguments)

    result = None

    # Session management tools (no phase check needed)
    if name == "start_session":
        intent = arguments["intent"]
        query = arguments["query"]
        repo_path = arguments.get("repo_path", ".")
        gate_level = arguments.get("gate_level", "auto")  # v1.10: default changed from "high" to "auto"
        skip_quality = arguments.get("skip_quality", False)
        skip_implementation = arguments.get("skip_implementation", False)
        print(f"[DEBUG start_session] skip_implementation argument = {skip_implementation}, type = {type(skip_implementation)}")

        # v1.6: enable_overlay/enable_branch parameter removed (branch creation moved to begin_phase_gate)
        # These parameters are no longer used

        session = session_manager.create_session(
            intent=intent,
            query=query,
            repo_path=repo_path,
            gate_level=gate_level,
        )

        # v1.5: Set quality review enabled/disabled
        session.quality_review_enabled = not skip_quality

        # v1.8: Set skip_implementation flag
        session.skip_implementation = skip_implementation

        # Get extraction prompt for QueryFrame
        extraction_prompt = QueryDecomposer.get_extraction_prompt(query)

        # v1.6: Phase is now None (unset) until begin_phase_gate is called
        # v1.10: gate_level="none" removed (use "full" or "auto" instead)
        if gate_level not in ("full", "auto"):
            phase_message = f"Session started with invalid gate_level={gate_level}. Valid values: 'full' or 'auto'."
            next_step_message = "Update gate_level to 'full' (execute all phases) or 'auto' (check before each)."
        else:
            phase_message = f"Session started. v1.6: Call begin_phase_gate to start phase gates."
            next_step_message = "Extract slots from query using the prompt, call set_query_frame, then call begin_phase_gate."

        result = {
            "success": True,
            "session_id": session.session_id,
            "intent": session.intent,
            "current_phase": session.phase.name,
            "allowed_tools": session.get_allowed_tools(),
            "repo_path": session.repo_path,
            "gate_level": gate_level,
            "message": phase_message,
            # v3.6: QueryFrame extraction
            "query_frame": {
                "status": "pending",
                "extraction_prompt": extraction_prompt,
                "next_step": next_step_message,
            },
            # v1.6: Next step indicator
            # v1.10: gate_level is now "full" or "auto" only
            "next_step": "Call begin_phase_gate to start phase gates" if gate_level in ("full", "auto") else None,
        }

        # v1.1: Essential context provision (design docs + project rules)
        # v1.3: Also includes doc_research configuration
        try:
            context_provider = ContextProvider(repo_path)
            essential_context = context_provider.load_context()
            if essential_context:
                result["essential_context"] = essential_context.to_dict()
                result["essential_context"]["note"] = (
                    "Design docs and project rules loaded from context.yml. "
                    "Use these to understand project conventions before implementation."
                )
            else:
                # v1.3: Even without context.yml, try to get doc_research config
                doc_research_config = context_provider.get_doc_research_config()
                if doc_research_config:
                    result["essential_context"] = {
                        "doc_research": {
                            "enabled": doc_research_config.enabled,
                            "docs_path": doc_research_config.docs_path,
                            "default_prompts": doc_research_config.default_prompts,
                        },
                        "note": "Documentation paths auto-detected. Use for DOCUMENT_RESEARCH phase.",
                    }
        except Exception as e:
            result["essential_context"] = {"error": str(e)}

        # v3.9: ChromaDB status and auto-sync
        if CHROMADB_AVAILABLE:
            try:
                manager = get_chromadb_manager(repo_path)
                chromadb_info = {
                    "available": True,
                    "stats": manager.get_stats(),
                    "needs_sync": manager.needs_sync(),
                }

                # Auto-sync if needed and configured
                if manager.config.get("sync_on_start", True) and manager.needs_sync():
                    sync_result = manager.sync_forest()
                    chromadb_info["auto_sync"] = sync_result.to_dict()

                result["chromadb"] = chromadb_info

                # v1.1: Check for essential docs that need summarization
                try:
                    context_provider = ContextProvider(repo_path)

                    # If context.yml doesn't exist, generate initial structure
                    if not context_provider.context_file.exists():
                        initial_config = context_provider.generate_initial_context()
                        if initial_config:
                            context_provider.save_context(initial_config)
                            result["context_initialized"] = {
                                "message": "Created initial context.yml with detected sources",
                                "sources": initial_config,
                            }

                    doc_changes = context_provider.check_docs_changed()
                    if doc_changes:
                        prompts = get_summary_prompts()
                        docs_to_summarize = []

                        for change in doc_changes:
                            change_path = Path(repo_path) / change["path"]
                            if change_path.exists():
                                try:
                                    if change["type"] == "essential_doc":
                                        docs_to_summarize.append({
                                            "type": "design_doc",
                                            "path": change["path"],
                                            "file": change_path.name,
                                        })
                                    elif change["type"] == "project_rules":
                                        docs_to_summarize.append({
                                            "type": "project_rules",
                                            "path": change["path"],
                                        })
                                except Exception:
                                    pass

                        if docs_to_summarize:
                            result["context_update_required"] = {
                                "documents": docs_to_summarize,
                                "prompts": prompts,
                                "instruction": (
                                    "Read each document using the Read tool, generate a summary using the appropriate prompt, "
                                    "then call update_context tool with the generated summaries."
                                ),
                            }
                except Exception:
                    pass  # Non-critical, don't fail session start
                result["v39_features"] = {
                    "semantic_search": "Use semantic_search for map/forest vector search",
                    "sync_index": "Use sync_index to manually trigger re-indexing",
                    "short_circuit": "High-confidence map hits (≥0.7) skip forest search",
                }
            except Exception as e:
                result["chromadb"] = {"available": True, "error": str(e)}
        else:
            result["chromadb"] = {
                "available": False,
                "note": "Install chromadb for v3.9 features: pip install chromadb",
            }

        if session.phase == Phase.EXPLORATION:
            result["exploration_hint"] = (
                "v3.9: Use semantic_search to find past agreements (map) and relevant code (forest). "
                "If no hit, use code-intel tools to fill missing slots. "
                "Then call submit_exploration and check_phase_necessity to determine next steps."
            )
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "begin_phase_gate":
        # v1.6: Start phase gate (separated from start_session)
        session_id = arguments["session_id"]
        skip_branch = arguments.get("skip_branch", False)
        resume_current = arguments.get("resume_current", False)

        session = session_manager.get_session(session_id)
        if session is None:
            result = {
                "success": False,
                "error": "session_not_found",
                "message": f"Session {session_id} not found. Call start_session first.",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        repo_path = session.repo_path

        # Check for stale branches (unless resume_current=true)
        if not resume_current:
            stale_info = await BranchManager.list_stale_branches(repo_path)
            stale_branches = stale_info.get("stale_branches", [])
            is_on_task_branch = stale_info.get("is_on_task_branch", False)

            # If not on task branch and stale branches exist, block and require user decision
            if not is_on_task_branch and stale_branches:
                result = {
                    "success": False,
                    "error": "stale_branches_detected",
                    "stale_branches": {
                        "branches": stale_branches,
                        "message": "Previous task branches exist. User action required.",
                    },
                    "recovery_options": {
                        "delete": "Run cleanup_stale_sessions, then retry begin_phase_gate",
                        "merge": "Run merge_to_base for each branch, then retry begin_phase_gate",
                        "continue": "Call begin_phase_gate(resume_current=true) to leave stale branches and continue",
                    },
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # Handle skip_branch
        if skip_branch:
            session.task_branch_enabled = False

            # v1.8: Differentiate between --quick mode and exploration-only mode
            if session.skip_implementation:
                # Exploration-only mode (INVESTIGATE/QUESTION): start with EXPLORATION
                session.transition_to_phase(Phase.EXPLORATION, reason="skip_branch (exploration-only)")
                result = {
                    "success": True,
                    "phase": "EXPLORATION",
                    "branch": {
                        "created": False,
                        "reason": "skip_branch=true (exploration-only)",
                    },
                }
            else:
                # --quick mode: skip directly to READY
                session.transition_to_phase(Phase.READY, reason="skip_branch (quick mode)")
                result = {
                    "success": True,
                    "phase": "READY",
                    "branch": {
                        "created": False,
                        "reason": "skip_branch=true (quick mode)",
                    },
                }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # Handle resume_current
        if resume_current:
            current_info = await BranchManager.is_task_branch_checked_out(repo_path)

            if current_info.get("is_task_branch"):
                # Already on a task branch - continue there
                # v1.10: Always start from EXPLORATION (gate_level=none removed)
                session.transition_to_phase(Phase.EXPLORATION, reason="resume_current")
                session.task_branch_enabled = True
                session.task_branch_name = current_info["current_branch"]

                result = {
                    "success": True,
                    "phase": session.phase.name,
                    "branch": {
                        "created": False,
                        "resumed": True,
                        "name": current_info["current_branch"],
                        "reason": "resume_current=true (continuing on current task branch)",
                    },
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
            else:
                # Not on task branch - create new one, but note stale branches are ignored
                stale_info = await BranchManager.list_stale_branches(repo_path)
                ignored_branches = [b["name"] for b in stale_info.get("stale_branches", [])]

                # Fall through to create new branch, but include stale_branches_ignored in response
                pass

        # Create new branch (normal flow or resume_current on non-task branch)
        try:
            branch_manager = BranchManager(repo_path)
            setup_result = await branch_manager.setup_session(session.session_id)

            if setup_result.success:
                # v1.10: Always start from EXPLORATION (gate_level=none removed)
                session.transition_to_phase(Phase.EXPLORATION, reason="begin_phase_gate")
                session.task_branch_enabled = True
                session.task_branch_name = setup_result.branch_name

                # Cache branch manager for this session
                _branch_managers[session.session_id] = branch_manager

                result = {
                    "success": True,
                    "phase": session.phase.name,
                    "branch": {
                        "created": True,
                        "name": setup_result.branch_name,
                        "base_branch": setup_result.base_branch,
                    },
                }

                # If resume_current was used but we created new branch, note ignored branches
                if resume_current:
                    stale_info = await BranchManager.list_stale_branches(repo_path)
                    # Re-fetch since we just created a new branch
                    ignored = [b["name"] for b in stale_info.get("stale_branches", [])
                               if b["name"] != setup_result.branch_name]
                    if ignored:
                        result["branch"]["stale_branches_ignored"] = ignored

                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
            else:
                result = {
                    "success": False,
                    "error": "branch_setup_failed",
                    "message": setup_result.error,
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        except Exception as e:
            result = {
                "success": False,
                "error": "branch_setup_exception",
                "message": str(e),
            }
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

        # v1.10: Assess risk level (inlined from assess_risk_level())
        # Determine risk based on QueryFrame completeness
        if frame.desired_action and not frame.observed_issue:
            risk_level = "HIGH"
        elif session.intent == "MODIFY" and not frame.target_feature:
            risk_level = "HIGH"
        elif session.intent == "IMPLEMENT" and not any([
            frame.target_feature,
            frame.trigger_condition,
            frame.observed_issue,
            frame.desired_action,
        ]):
            risk_level = "HIGH"
        elif frame.observed_issue and len(frame.observed_issue) < 10:
            risk_level = "MEDIUM"
        elif frame.get_hypothesis_slots():
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

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

    # v1.10 note: submit_understanding handler removed (replaced by check_phase_necessity)
    # LLM should call check_phase_necessity after EXPLORATION to determine if SEMANTIC is needed

    elif name == "submit_semantic":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            # semantic_reason を文字列から SemanticReason Enum に変換
            reason_str = arguments.get("semantic_reason")
            semantic_reason = None
            if reason_str:
                try:
                    semantic_reason = SemanticReason(reason_str)
                except ValueError:
                    result = {
                        "error": "invalid_semantic_reason",
                        "message": f"Invalid semantic_reason: '{reason_str}'",
                        "valid_reasons": [r.value for r in SemanticReason],
                    }
                    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # hypotheses を Hypothesis オブジェクトに変換
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
                semantic_reason=semantic_reason,
                search_queries=arguments.get("search_queries", []),
            )
            result = session.submit_semantic(semantic)
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "check_phase_necessity":
        # v1.10: Check phase necessity before execution
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            phase = arguments["phase"]
            assessment = arguments["assessment"]

            # Handle string-serialized assessment (MCP client workaround)
            if isinstance(assessment, str):
                try:
                    assessment = json.loads(assessment)
                except json.JSONDecodeError as e:
                    result = {"error": "invalid_assessment", "message": f"Failed to parse assessment JSON: {e}"}
                    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # Validate assessment fields based on phase
            valid, error_msg = _validate_phase_assessment(phase, assessment)
            if not valid:
                result = {"error": "invalid_assessment", "message": error_msg}
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # gate_level="full" forces execution of all phases
            if session.gate_level == "full":
                session.phase = Phase[phase]
                session.phase_assessments[phase] = assessment
                result = {
                    "success": True,
                    "phase_required": True,
                    "next_phase": phase,
                    "reason": "gate_level=full: All phases are executed regardless of assessment",
                    "assessment": assessment,
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # gate_level="auto": Use assessment to determine
            phase_required = False
            next_phase = None

            if phase == "SEMANTIC":
                needs_more_info = assessment.get("needs_more_information", False)
                if needs_more_info:
                    phase_required = True
                    next_phase = "SEMANTIC"
                    session.phase = Phase.SEMANTIC
                else:
                    # Skip SEMANTIC, proceed to Q2 check
                    next_phase = "Q2_CHECK"

            elif phase == "VERIFICATION":
                has_unverified = assessment.get("has_unverified_hypotheses", False)
                if has_unverified:
                    phase_required = True
                    next_phase = "VERIFICATION"
                    session.phase = Phase.VERIFICATION
                else:
                    # Skip VERIFICATION, proceed to Q3 check
                    next_phase = "Q3_CHECK"

            elif phase == "IMPACT_ANALYSIS":
                needs_impact = assessment.get("needs_impact_analysis", False)
                if needs_impact:
                    phase_required = True
                    next_phase = "IMPACT_ANALYSIS"
                    session.phase = Phase.IMPACT_ANALYSIS
                else:
                    # Skip IMPACT_ANALYSIS, proceed to READY
                    next_phase = "READY"
                    session.phase = Phase.READY

            # Record assessment
            session.phase_assessments[phase] = assessment

            result = {
                "success": True,
                "phase_required": phase_required,
                "next_phase": next_phase,
                "assessment": assessment,
                "instruction": _get_next_instruction(phase, phase_required, next_phase),
            }

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

    elif name == "add_explored_files":
        # v3.10: Add files to explored list in READY phase
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            result = session.add_explored_files(
                files=arguments["files"],
            )
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "revert_to_exploration":
        # v3.10: Revert to EXPLORATION phase
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            result = session.revert_to_exploration(
                keep_results=arguments.get("keep_results", True),
            )
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    # v1.2: PRE_COMMIT phase tools
    elif name == "submit_for_review":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            result = session.submit_for_review()
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "review_changes":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        if session.phase != Phase.PRE_COMMIT:
            result = {
                "error": "phase_blocked",
                "current_phase": session.phase.name,
                "message": f"review_changes only allowed in PRE_COMMIT phase, current: {session.phase.name}",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        if not session.task_branch_enabled:
            result = {
                "error": "task_branch_not_enabled",
                "message": "Task branch not enabled. Cannot review changes.",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # Get or recreate branch manager for this session
        repo_path = session.repo_path or "."
        branch_manager = _get_or_recreate_branch_manager(session, repo_path)
        if branch_manager is None:
            result = {
                "error": "branch_manager_not_found",
                "message": "Branch manager not found and cannot be recreated (no task_branch_name in session).",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        changes = await branch_manager.get_changes()

        result = {
            "success": True,
            "session_id": session.session_id,
            "total_changes": changes.total_files,
            "changes": [
                {
                    "path": c.path,
                    "change_type": c.change_type,
                    "is_binary": c.is_binary,
                    "diff": c.diff,
                }
                for c in changes.changes
            ],
            "review_prompt": "Read .code-intel/review_prompts/garbage_detection.md and follow its instructions to review each change.",
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "finalize_changes":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        if session.phase != Phase.PRE_COMMIT:
            result = {
                "error": "phase_blocked",
                "current_phase": session.phase.name,
                "message": f"finalize_changes only allowed in PRE_COMMIT phase, current: {session.phase.name}",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        if not session.task_branch_enabled:
            result = {
                "error": "task_branch_not_enabled",
                "message": "Task branch not enabled. Cannot finalize changes.",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # Get or recreate branch manager for this session
        repo_path = session.repo_path or "."
        branch_manager = _get_or_recreate_branch_manager(session, repo_path)
        if branch_manager is None:
            result = {
                "error": "branch_manager_not_found",
                "message": "Branch manager not found and cannot be recreated (no task_branch_name in session).",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # Process reviewed files
        reviewed_files = arguments.get("reviewed_files", [])
        commit_message = arguments.get("commit_message")

        # Submit review to session
        review_result = session.submit_pre_commit_review(
            reviewed_files=reviewed_files,
            review_notes=commit_message or "",
        )

        if not review_result.get("success"):
            return [TextContent(type="text", text=json.dumps(review_result, indent=2, ensure_ascii=False))]

        # v1.8: Apply changes using branch manager
        # If quality_review_enabled, prepare commit but don't execute (for quality check first)
        # If quality_review_disabled, execute commit immediately
        execute_commit_now = not session.quality_review_enabled

        finalize_result = await branch_manager.finalize(
            keep_files=review_result.get("kept_files"),
            discard_files=review_result.get("discarded_files"),
            commit_message=commit_message,
            execute_commit=execute_commit_now,  # v1.8: Skip commit if quality review is enabled
        )

        if finalize_result.success:
            # v1.8: Store commit preparation state
            if finalize_result.prepared:
                session.commit_prepared = True
                session.prepared_commit_message = commit_message
                session.prepared_kept_files = finalize_result.kept_files
                session.prepared_discarded_files = finalize_result.discarded_files

            # v1.5: Transition to QUALITY_REVIEW phase (if enabled)
            if session.quality_review_enabled:
                # Check if quality_review.md exists
                repo_path = session.repo_path or "."
                quality_review_path = Path(repo_path) / ".code-intel" / "review_prompts" / "quality_review.md"

                if not quality_review_path.exists():
                    # Skip QUALITY_REVIEW if prompt file is missing
                    session.quality_review_completed = True
                    result = {
                        "success": True,
                        "commit_hash": finalize_result.commit_hash,
                        "kept_files": finalize_result.kept_files,
                        "discarded_files": finalize_result.discarded_files,
                        "branch": session.task_branch_name,
                        "skipped": True,
                        "warning": f"quality_review.md not found at {quality_review_path}",
                        "message": "Quality review skipped. Proceeding to merge.",
                        "next_action": "Call merge_to_base to complete",
                    }
                else:
                    session.transition_to_phase(Phase.QUALITY_REVIEW, reason="finalize_changes")
                    result = {
                        "success": True,
                        "commit_hash": None if finalize_result.prepared else finalize_result.commit_hash,  # v1.8: No commit hash yet if prepared
                        "prepared": finalize_result.prepared,  # v1.8
                        "kept_files": finalize_result.kept_files,
                        "discarded_files": finalize_result.discarded_files,
                        "branch": session.task_branch_name,
                        "phase": "QUALITY_REVIEW",
                        "message": f"Changes prepared. Now in QUALITY_REVIEW phase. Commit will be executed after quality check passes." if finalize_result.prepared else f"Changes finalized. Committed to {session.task_branch_name}. Now in QUALITY_REVIEW phase.",
                        "next_step": "Read .code-intel/review_prompts/quality_review.md and follow instructions. Call submit_quality_review when done.",
                    }
            else:
                # Quality review disabled (--no-quality)
                result = {
                    "success": True,
                    "commit_hash": finalize_result.commit_hash,
                    "kept_files": finalize_result.kept_files,
                    "discarded_files": finalize_result.discarded_files,
                    "branch": session.task_branch_name,
                    "message": f"Changes finalized. Committed to {session.task_branch_name}. Quality review skipped.",
                    "next_step": "Call merge_to_base to merge back to original branch.",  # DISABLED: record_outcome
                }
        else:
            result = {
                "success": False,
                "error": finalize_result.error,
                "kept_files": finalize_result.kept_files,
                "discarded_files": finalize_result.discarded_files,
            }

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "submit_quality_review":
        # v1.5: Quality Review phase handler
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        if session.phase != Phase.QUALITY_REVIEW:
            result = {
                "error": "phase_blocked",
                "current_phase": session.phase.name,
                "message": f"submit_quality_review only allowed in QUALITY_REVIEW phase, current: {session.phase.name}",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        issues_found_raw = arguments.get("issues_found", False)
        # Handle string "true"/"false" from MCP clients
        if isinstance(issues_found_raw, str):
            issues_found = issues_found_raw.lower() == "true"
        else:
            issues_found = bool(issues_found_raw)
        issues = arguments.get("issues", [])
        notes = arguments.get("notes")

        if issues_found:
            # Validate issues list
            if not issues:
                result = {
                    "error": "issues_required",
                    "message": "issues_found=true requires non-empty issues list",
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # Check max revert count
            session.quality_revert_count += 1
            if session.quality_revert_count > session.quality_review_max_revert:
                # Force completion - max reverts exceeded
                session.quality_review_completed = True  # Allow merge_to_base
                result = {
                    "success": True,
                    "issues_found": True,
                    "forced_completion": True,
                    "issues": issues,
                    "revert_count": session.quality_revert_count,
                    "message": f"Max revert count ({session.quality_review_max_revert}) exceeded. Forcing completion.",
                    "warning": "Quality issues may remain unresolved.",
                    "next_action": "Call merge_to_base to complete",
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # v1.8: Clear commit preparation state when reverting
            session.commit_prepared = False
            session.prepared_commit_message = None
            session.prepared_kept_files = []
            session.prepared_discarded_files = []

            # Revert to READY phase for fixes
            session.transition_to_phase(Phase.READY, reason="quality_review_issues_found")
            result = {
                "success": True,
                "issues_found": True,
                "issues": issues,
                "revert_count": session.quality_revert_count,
                "next_action": "Fix the issues in READY phase, then re-run verification",
                "phase": "READY",
                "message": "Reverted to READY phase. Prepared commit discarded. Fix issues and proceed through POST_IMPL_VERIFY → PRE_COMMIT → QUALITY_REVIEW.",
            }
        else:
            # v1.8: No issues - execute prepared commit if it exists
            commit_hash = None
            if session.commit_prepared:
                # Execute the prepared commit
                repo_path = session.repo_path or "."
                branch_manager = _get_or_recreate_branch_manager(session, repo_path)
                if branch_manager:
                    commit_result = await branch_manager.execute_prepared_commit(
                        commit_message=session.prepared_commit_message or "Quality review passed"
                    )
                    if commit_result.success:
                        commit_hash = commit_result.commit_hash
                        session.commit_prepared = False  # Clear preparation state
                    else:
                        result = {
                            "success": False,
                            "error": "commit_execution_failed",
                            "message": f"Failed to execute prepared commit: {commit_result.error}",
                        }
                        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

            # No issues - mark quality review as completed and ready for merge
            session.quality_review_completed = True
            result = {
                "success": True,
                "issues_found": False,
                "notes": notes,
                "commit_hash": commit_hash,  # v1.8: Include commit hash if executed
                "message": f"Quality review passed. Commit executed: {commit_hash}. Ready for merge." if commit_hash else "Quality review passed. Ready for merge.",
                "next_action": "Call merge_to_base to complete",
            }

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "merge_to_base":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        if not session.task_branch_enabled:
            result = {
                "error": "task_branch_not_enabled",
                "message": "Task branch not enabled. Cannot merge.",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # v1.5: Check if QUALITY_REVIEW is required but not completed
        if session.quality_review_enabled and session.phase == Phase.QUALITY_REVIEW and not session.quality_review_completed:
            result = {
                "error": "quality_review_required",
                "current_phase": session.phase.name,
                "message": "QUALITY_REVIEW phase not completed. Call submit_quality_review first.",
                "next_action": "Read .code-intel/review_prompts/quality_review.md and call submit_quality_review",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # Get or recreate branch manager for this session
        repo_path = session.repo_path or "."
        branch_manager = _get_or_recreate_branch_manager(session, repo_path)
        if branch_manager is None:
            result = {
                "error": "branch_manager_not_found",
                "message": "Branch manager not found and cannot be recreated (no task_branch_name in session).",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        merge_result = await branch_manager.merge_to_base()

        if merge_result["success"]:
            # Cleanup branch manager cache after successful merge
            if session.session_id in _branch_managers:
                del _branch_managers[session.session_id]

            result = {
                "success": True,
                "merged": True,
                "branch_deleted": merge_result.get("branch_deleted", False),
                "from_branch": merge_result.get("from_branch"),
                "to_branch": merge_result.get("to_branch"),
                "message": f"Successfully merged {merge_result.get('from_branch')} to {merge_result.get('to_branch')}." +
                          (" Branch deleted." if merge_result.get("branch_deleted") else ""),
                # DISABLED: "next_step": "Call record_outcome to record the result.",
            }
        else:
            result = {
                "success": False,
                "merged": False,
                "branch_deleted": False,
                "from_branch": merge_result.get("from_branch"),
                "to_branch": merge_result.get("to_branch"),
                "error": merge_result.get("error"),
            }

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "cleanup_stale_branches":
        # v1.2.1: Clean up stale task branches from interrupted runs
        # v1.10: Added action parameter (delete/merge)
        repo_path = arguments.get("repo_path", ".")
        action = arguments.get("action", "delete")

        cleanup_result = await BranchManager.cleanup_stale_sessions(repo_path, action=action)

        deleted_count = len(cleanup_result.get("deleted_branches", []))
        merged_count = len(cleanup_result.get("merged_branches", []))
        checked_out_to = cleanup_result.get("checked_out_to")

        if action == "merge":
            message = f"Merged {merged_count} branches, deleted {deleted_count} stale branches."
        else:
            message = f"Cleaned up {deleted_count} stale branches."
        if checked_out_to:
            message = f"Checked out to '{checked_out_to}'. " + message

        result = {
            "success": True,
            "action": action,
            "deleted_branches": cleanup_result.get("deleted_branches", []),
            "errors": cleanup_result.get("errors", []),
            "message": message,
        }

        if cleanup_result.get("merged_branches"):
            result["merged_branches"] = cleanup_result["merged_branches"]
        if checked_out_to:
            result["checked_out_to"] = checked_out_to

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    # =========================================================================
    # v1.4: Intervention System Handlers
    # =========================================================================

    elif name == "record_verification_failure":
        # Record verification failure for intervention tracking
        session = session_manager.get_active_session()
        if not session:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": "No active session. Start a session first.",
                }, ensure_ascii=False),
            )]

        failure_info = {
            "phase": "POST_IMPLEMENTATION_VERIFICATION",
            "error_message": arguments["error_message"],
            "problem_location": arguments["problem_location"],
            "observed_values": arguments["observed_values"],
            "attempt_number": session.verification_failure_count + 1,
        }

        result = session.record_verification_failure(failure_info)
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "record_intervention_used":
        # Record that an intervention prompt was used
        session = session_manager.get_active_session()
        if not session:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": "No active session. Start a session first.",
                }, ensure_ascii=False),
            )]

        prompt_name = arguments["prompt_name"]
        result = session.record_intervention_used(prompt_name)

        # Reset verification failure count after intervention
        session.reset_verification_failures()
        result["verification_failures_reset"] = True

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "get_intervention_status":
        # Get current intervention system status
        session = session_manager.get_active_session()
        if not session:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": "No active session. Start a session first.",
                }, ensure_ascii=False),
            )]

        result = session.get_intervention_status()
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "record_outcome":
        # v3.5: Record session outcome
        # v3.10: Support explicit phase/intent for auto-failure detection
        session_id = arguments["session_id"]
        session = session_manager.get_session(session_id)

        # Get session context (prefer session data, fallback to arguments)
        phase_at_outcome = "UNKNOWN"
        intent = "UNKNOWN"
        semantic_used = False
        confidence_was = ""

        if session:
            phase_at_outcome = session.phase.name
            intent = session.intent
            semantic_used = session.semantic is not None
            if session.exploration:
                confidence_was = session.exploration._evaluated_confidence

        # Allow explicit override (for cases where session is not in memory)
        if "phase_at_outcome" in arguments:
            phase_at_outcome = arguments["phase_at_outcome"]
        if "intent" in arguments:
            intent = arguments["intent"]

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
            semantic_used=semantic_used,
            confidence_was=confidence_was,
            analysis=analysis,
            trigger_message=arguments.get("trigger_message", ""),
        )

        result = record_outcome(outcome_log)

        # Cache successful pairs + Generate agreements
        if arguments["outcome"] == "success" and session and session.query_frame:
            try:
                from tools.learned_pairs import cache_successful_pair
                from tools.agreements import AgreementData, get_agreements_manager

                qf = session.query_frame
                repo_path = session.repo_path

                if qf.target_feature and qf.mapped_symbols:
                    cached_count = 0
                    agreement_files = []

                    for sym in qf.mapped_symbols:
                        # 1. learned_pairs.json に追加
                        cache_successful_pair(
                            nl_term=qf.target_feature,
                            symbol=sym.name,
                            similarity=sym.confidence,
                            code_evidence=sym.evidence.result_summary if sym.evidence else None,
                            session_id=session_id,
                            project_root=repo_path,
                        )
                        cached_count += 1

                        # agreements/ に Markdown を生成
                        agreement_data = AgreementData(
                            nl_term=qf.target_feature,
                            symbol=sym.name,
                            similarity=sym.confidence,
                            code_evidence=sym.evidence.result_summary if sym.evidence else None,
                            session_id=session_id,
                            intent=session.intent,
                            related_files=analysis.related_files if analysis else [],
                            query_frame_summary={
                                "target_feature": qf.target_feature,
                                "trigger_condition": qf.trigger_condition,
                                "observed_issue": qf.observed_issue,
                                "desired_action": qf.desired_action,
                            },
                        )

                        manager = get_agreements_manager(repo_path)
                        agreement_file = manager.save_agreement(agreement_data)
                        agreement_files.append(str(agreement_file.name))

                    result["cached_pairs"] = cached_count
                    result["agreement_files"] = agreement_files
                    result["cache_note"] = f"{cached_count} pairs cached, {len(agreement_files)} agreement(s) generated"

                    # ChromaDB map に追加
                    if CHROMADB_AVAILABLE:
                        try:
                            chromadb_manager = get_chromadb_manager(repo_path)
                            symbols_list = [sym.name for sym in qf.mapped_symbols]
                            code_evidence = "; ".join(
                                sym.evidence.result_summary
                                for sym in qf.mapped_symbols
                                if sym.evidence and sym.evidence.result_summary
                            ) or "Success confirmed by user"

                            chromadb_manager.add_agreement(
                                nl_term=qf.target_feature,
                                symbols=symbols_list,
                                code_evidence=code_evidence,
                                session_id=session_id,
                                similarity=max(sym.confidence for sym in qf.mapped_symbols) if qf.mapped_symbols else 0.0,
                            )
                            result["chromadb_map"] = "agreement added"
                        except Exception as chromadb_err:
                            result["chromadb_error"] = str(chromadb_err)

            except Exception as e:
                result["cache_error"] = str(e)

        # v1.6: Auto-delete branch on failure
        if arguments["outcome"] == "failure":
            try:
                # Determine repo_path
                repo_path = "."
                if session:
                    repo_path = session.repo_path

                # Find and delete the branch for this session
                stale_info = await BranchManager.list_stale_branches(repo_path)
                target_branch = None

                for branch in stale_info.get("stale_branches", []):
                    if branch.get("session_id") == session_id:
                        target_branch = branch["name"]
                        break

                if target_branch:
                    # Check if we're currently on this branch
                    current_branch = stale_info.get("current_branch", "")
                    if current_branch == target_branch:
                        # Need to checkout base branch first
                        parsed = BranchManager.parse_task_branch(target_branch)
                        base_branch = parsed.get("base_branch") if parsed else "main"
                        if base_branch:
                            checkout_proc = await asyncio.create_subprocess_exec(
                                "git", "checkout", base_branch,
                                cwd=repo_path,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await checkout_proc.communicate()

                    # Delete the branch
                    delete_result = await BranchManager.delete_branch(repo_path, target_branch, force=True)

                    result["branch_cleanup"] = {
                        "attempted": True,
                        "deleted": delete_result.get("deleted"),
                        "message": "Failed session branch deleted automatically." if delete_result.get("success")
                                   else f"Branch deletion failed: {delete_result.get('error')}",
                    }
                else:
                    result["branch_cleanup"] = {
                        "attempted": True,
                        "deleted": None,
                        "message": "No branch found for session (may have been deleted already).",
                    }
            except Exception as e:
                result["branch_cleanup"] = {
                    "attempted": True,
                    "deleted": None,
                    "error": str(e),
                    "message": "Branch cleanup failed with exception.",
                }

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "get_outcome_stats":
        result = get_failure_stats()
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "validate_symbol_relevance":
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
            "action_required": "LLMがこのプロンプトに回答し、confirm_symbol_relevance で検証結果を確定してください。",
            "response_schema": {
                "relevant_symbols": "array of string",
                "reasoning": "string (required)",
                "code_evidence": "string (required)",
            },
        }

        # Check learned pairs cache first
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

        # Embedding-based suggestions (if available)
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

    elif name == "confirm_symbol_relevance":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        elif not session.query_frame:
            result = {"error": "no_query_frame", "message": "QueryFrame not set. Call set_query_frame first."}
        else:
            relevant_symbols = arguments.get("relevant_symbols", [])
            code_evidence = arguments.get("code_evidence", "")
            reasoning = arguments.get("reasoning", "")

            qf = session.query_frame
            updated_count = 0
            not_found = []

            # Embedding で類似度を取得
            embedding_scores = {}
            try:
                from tools.embedding import get_embedding_validator, is_embedding_available
                if is_embedding_available() and qf.target_feature:
                    validator = get_embedding_validator()
                    suggestions = validator.find_related_symbols(
                        qf.target_feature, relevant_symbols, top_k=len(relevant_symbols)
                    )
                    embedding_scores = {s["symbol"]: s["similarity"] for s in suggestions}
            except Exception:
                pass

            # mapped_symbols の confidence を更新
            for symbol in relevant_symbols:
                # 既存のシンボルを探す
                existing = [s for s in qf.mapped_symbols if s.name == symbol]
                if existing:
                    # Embedding スコアがあればそれを使用、なければ 0.7（LLM確認済み）
                    new_confidence = embedding_scores.get(symbol, 0.7)
                    existing[0].confidence = new_confidence
                    existing[0].source = SlotSource.FACT
                    if code_evidence:
                        existing[0].evidence = SlotEvidence(
                            tool="confirm_symbol_relevance",
                            params={"reasoning": reasoning},
                            result_summary=code_evidence,
                        )
                    updated_count += 1
                else:
                    # submit_understanding で追加されていないシンボル
                    not_found.append(symbol)

            result = {
                "success": True,
                "updated_count": updated_count,
                "mapped_symbols": [
                    {"name": s.name, "confidence": s.confidence, "source": s.source.value}
                    for s in qf.mapped_symbols
                ],
                "message": f"{updated_count} symbols confirmed with code_evidence.",
            }

            if not_found:
                result["not_found"] = not_found
                result["hint"] = "These symbols were not in mapped_symbols. Call submit_exploration first to add them."

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "sync_index":
        session = session_manager.get_active_session()
        path = arguments.get("path") or (session.repo_path if session else ".")
        force = arguments.get("force", False)
        sync_map = arguments.get("sync_map", True)

        if not CHROMADB_AVAILABLE:
            result = {
                "error": "chromadb_not_available",
                "message": "chromadb is not installed. Install with: pip install chromadb",
            }
        else:
            try:
                manager = get_chromadb_manager(path)

                # Sync forest (source code)
                forest_result = manager.sync_forest(force=force)

                result = {
                    "success": True,
                    "forest_sync": forest_result.to_dict(),
                    "stats": manager.get_stats(),
                }

                # Sync map (agreements) if requested
                if sync_map:
                    map_result = manager.sync_map()
                    result["map_sync"] = map_result.to_dict()

                # v1.1: Check for essential docs changes and provide content for LLM summary generation
                try:
                    context_provider = ContextProvider(path)

                    # If context.yml doesn't exist, generate initial structure
                    if not context_provider.context_file.exists():
                        initial_config = context_provider.generate_initial_context()
                        if initial_config:
                            context_provider.save_context(initial_config)
                            result["context_initialized"] = {
                                "message": "Created initial context.yml with detected sources",
                                "sources": initial_config,
                            }

                    doc_changes = context_provider.check_docs_changed()
                    if doc_changes:
                        prompts = get_summary_prompts()
                        docs_to_summarize = []

                        for change in doc_changes:
                            change_path = Path(path) / change["path"]
                            if change_path.exists():
                                try:
                                    if change["type"] == "essential_doc":
                                        docs_to_summarize.append({
                                            "type": "design_doc",
                                            "path": change["path"],
                                            "file": change_path.name,
                                        })
                                    elif change["type"] == "project_rules":
                                        docs_to_summarize.append({
                                            "type": "project_rules",
                                            "path": change["path"],
                                        })
                                except Exception:
                                    pass

                        if docs_to_summarize:
                            result["context_update_required"] = {
                                "documents": docs_to_summarize,
                                "prompts": prompts,
                                "instruction": (
                                    "Read each document using the Read tool, generate a summary using the appropriate prompt, "
                                    "then call update_context tool with the generated summaries."
                                ),
                            }
                except Exception:
                    pass  # Non-critical, don't fail sync

            except Exception as e:
                result = {"error": str(e)}

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "semantic_search":
        session = session_manager.get_active_session()
        query = arguments["query"]
        path = arguments.get("path") or (session.repo_path if session else ".")
        target_feature = arguments.get("target_feature") or (
            session.query_frame.target_feature if session and session.query_frame else None
        )
        collection = arguments.get("collection", "auto")
        n_results = arguments.get("n_results", 10)

        if not CHROMADB_AVAILABLE:
            result = {
                "error": "chromadb_not_available",
                "message": "chromadb is not installed. Install with: pip install chromadb",
            }
        else:
            try:
                manager = get_chromadb_manager(path)
                search_result = manager.search(
                    query=query,
                    target_feature=target_feature,
                    collection=collection,
                    n_results=n_results,
                )

                result = {
                    "success": True,
                    **search_result.to_dict(),
                    "query": query,
                    "target_feature": target_feature,
                }

            except Exception as e:
                result = {"error": str(e)}

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "analyze_impact":
        # v1.1: Impact analysis before READY phase
        session = session_manager.get_active_session()
        path = session.repo_path if session else "."

        target_files = arguments["target_files"]
        change_description = arguments.get("change_description", "")

        try:
            result = await analyze_impact(
                target_files=target_files,
                change_description=change_description,
                repo_path=path,
            )

            # Add session context if available
            if session:
                result["session_id"] = session.session_id
                result["current_phase"] = session.phase.name

                # v1.1: Store impact analysis context in session for validation
                confirmation = result.get("confirmation_required", {})
                impact = result.get("impact_analysis", {})
                session.set_impact_analysis_context(
                    target_files=target_files,
                    must_verify=confirmation.get("must_verify", []),
                    should_verify=confirmation.get("should_verify", []),
                    mode=impact.get("mode", "standard"),
                )

                # Add essential_context hint if project_rules exists
                if session.query_frame:
                    result["query_frame"] = {
                        "target_feature": session.query_frame.target_feature,
                    }

            # Add guidance for LLM
            result["next_steps"] = {
                "action": "submit_impact_analysis",
                "instructions": (
                    "1. Review must_verify files and check if changes affect them\n"
                    "2. Review should_verify files (tests, factories, seeders)\n"
                    "3. Use project_rules to infer additional related files\n"
                    "4. Call submit_impact_analysis with verified_files and inferred_from_rules"
                ),
            }

        except Exception as e:
            result = {"error": str(e)}

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "update_context":
        # v1.1: Save LLM-generated summaries to context.yml
        session = session_manager.get_active_session()
        path = session.repo_path if session else "."

        design_doc_summaries = arguments.get("design_doc_summaries", [])
        project_rules_summary = arguments.get("project_rules_summary", "")

        try:
            from tools.context_provider import DocSummary
            import hashlib

            context_provider = ContextProvider(path)

            # Convert to DocSummary objects and calculate content_hash
            summaries = []
            for s in design_doc_summaries:
                doc_path = Path(path) / s["path"]
                content_hash = ""
                if doc_path.exists():
                    content_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()[:16]

                summaries.append(DocSummary(
                    file=s.get("file", doc_path.name),
                    path=s["path"],
                    summary=s["summary"],
                    content_hash=content_hash,
                ))

            # Calculate hash for project_rules source
            config = context_provider.get_context_config() or {}
            project_rules = config.get("project_rules", {})
            if project_rules_summary and project_rules.get("source"):
                rules_path = Path(path) / project_rules["source"]
                if rules_path.exists():
                    project_rules["content_hash"] = hashlib.sha256(rules_path.read_bytes()).hexdigest()[:16]
                    config["project_rules"] = project_rules
                    context_provider.save_context(config)

            # Update summaries (preserves extra_notes)
            context_provider.update_summaries(summaries, project_rules_summary)

            result = {
                "success": True,
                "updated": {
                    "design_docs": len(summaries),
                    "project_rules": bool(project_rules_summary),
                },
                "message": "Context summaries updated in .code-intel/context.yml",
            }

        except Exception as e:
            result = {"error": str(e)}

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "submit_impact_analysis":
        # v1.1: Submit impact analysis results to proceed to READY phase
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session."}
        else:
            verified_files = arguments.get("verified_files", [])
            inferred_from_rules = arguments.get("inferred_from_rules", [])

            result = session.submit_impact_analysis(
                verified_files=verified_files,
                inferred_from_rules=inferred_from_rules,
            )

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    # v1.10 note: submit_verification_and_impact handler removed (v1.9 integration reverted)
    # Use separate submit_verification and submit_impact_analysis instead

    # Check phase access for other tools
    phase_error = check_phase_access(name)
    if phase_error:
        return [TextContent(type="text", text=json.dumps(phase_error, indent=2, ensure_ascii=False))]

    # Record tool call in session
    session = session_manager.get_active_session()

    if name == "search_text":
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
        # Get active session for caching
        session = session_manager.get_active_session()

        result = await find_definitions(
            symbol=arguments["symbol"],
            path=arguments.get("path", "."),
            language=arguments.get("language"),
            exact_match=arguments.get("exact_match", False),
            session=session,
        )

        # Add cache stats to result
        if session:
            result["cache_stats"] = session.cache_stats.copy()

    elif name == "find_references":
        # Get active session for caching
        session = session_manager.get_active_session()

        result = await find_references(
            symbol=arguments["symbol"],
            path=arguments.get("path", "."),
            language=arguments.get("language"),
            session=session,
        )

        # Add cache stats to result
        if session:
            result["cache_stats"] = session.cache_stats.copy()

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

    elif name == "fetch_chunk_detail":
        # v1.8: 段階的取得 - ChromaDBから個別チャンクを取得
        chunk_id = arguments.get("chunk_id")
        project_root = arguments.get("path", ".")

        if not chunk_id:
            result = {"error": "chunk_id is required"}
        else:
            try:
                manager = get_chromadb_manager(project_root)
                # ChromaDBからチャンクを取得
                chunk_result = manager.forest_collection.get(
                    ids=[chunk_id],
                    include=["documents", "metadatas"]
                )

                if chunk_result and chunk_result["documents"]:
                    result = {
                        "success": True,
                        "chunk_id": chunk_id,
                        "content": chunk_result["documents"][0],
                        "metadata": chunk_result["metadatas"][0] if chunk_result["metadatas"] else {},
                    }
                else:
                    result = {"error": f"Chunk not found: {chunk_id}"}
            except Exception as e:
                result = {"error": f"Failed to fetch chunk: {str(e)}"}

    else:
        result = {"error": f"Unknown tool: {name}"}

    # Record tool call in session
    if session is not None and result is not None:
        result_summary = ""
        result_detail = {}

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

        # v1.8: Record tool call end with timing
        session.record_tool_call_end(result_summary, result_detail)

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

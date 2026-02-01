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
- Checkpoint persistence for session recovery (v1.12)

v1.1 Additions:
- Essential context provision (design docs + project rules)
- Impact analysis before READY phase
- Markup context relaxation
"""

import asyncio
import json
from datetime import datetime
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
    TaskModel, get_phase_response,
    PHASE_STEP_MAP, EXPECTED_PAYLOADS, PHASE_INSTRUCTIONS,
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


# v1.12: Response size limit (256KB)
MAX_RESPONSE_BYTES = 262_144


def _truncate_response(content: str, max_bytes: int = MAX_RESPONSE_BYTES) -> tuple[str, bool]:
    """Truncate response if it exceeds max_bytes. Returns (content, was_truncated)."""
    content_bytes = content.encode("utf-8")
    if len(content_bytes) <= max_bytes:
        return content, False

    # Try to detect total_matches from the original content
    total_matches = None
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "matches" in parsed:
            matches = parsed["matches"]
            if isinstance(matches, list):
                total_matches = len(matches)
    except (json.JSONDecodeError, TypeError):
        pass

    # Truncate to max_bytes
    truncated_bytes = content_bytes[:max_bytes]
    # Decode safely (may cut a multi-byte char)
    truncated = truncated_bytes.decode("utf-8", errors="ignore")

    # Build warning
    warning = {"_truncation_warning": {"truncated": True, "message": "レスポンスが 256KB を超えたため切り詰めました。パターンを絞り込んでください。"}}
    if total_matches is not None:
        warning["_truncation_warning"]["total_matches"] = total_matches
    warning_str = "\n" + json.dumps(warning, ensure_ascii=False)

    return truncated + warning_str, True


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


def _phase_response(session: SessionState | None, phase_name: str, step: int | None = None, extra: dict | None = None) -> dict:
    repo_path = session.repo_path if session is not None else "."
    return get_phase_response(phase_name, step=step, extra=extra, repo_path=repo_path)


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


async def _create_branch_for_ready(session) -> dict:
    """
    v1.11: Create task branch when transitioning to READY phase.

    This function is called when:
    - check_phase_necessity skips IMPACT_ANALYSIS (Q3=NO) → READY
    - submit_impact_analysis completes → READY

    Returns: {"success": bool, "branch": {...}} or {"error": str, "message": str}
    """
    # Skip if already has branch or skip_implementation mode
    if session.task_branch_enabled and session.task_branch_name:
        return {
            "success": True,
            "branch": {
                "created": False,
                "name": session.task_branch_name,
                "reason": "Already on task branch",
            },
        }

    if session.skip_implementation:
        return {
            "success": True,
            "branch": {
                "created": False,
                "reason": "skip_implementation=true (exploration-only)",
            },
        }

    repo_path = session.repo_path

    if session.branch_policy == "continue":
        if not session.task_branch_name:
            try:
                branch_status = await BranchManager.is_task_branch_checked_out(repo_path)
                if branch_status.get("is_task_branch"):
                    session.task_branch_enabled = True
                    session.task_branch_name = branch_status.get("current_branch")
            except Exception:
                pass
        return {
            "success": True,
            "branch": {
                "created": False,
                "name": session.task_branch_name,
                "reason": "branch_policy_continue",
            },
        }

    try:
        branch_manager = BranchManager(repo_path)
        setup_result = await branch_manager.setup_session(session.session_id)

        if setup_result.success:
            session.task_branch_enabled = True
            session.task_branch_name = setup_result.branch_name

            # Cache branch manager for this session
            _branch_managers[session.session_id] = branch_manager

            return {
                "success": True,
                "branch": {
                    "created": True,
                    "name": setup_result.branch_name,
                    "base_branch": setup_result.base_branch,
                },
            }
        else:
            return {
                "success": False,
                "error": "branch_setup_failed",
                "message": setup_result.error,
            }

    except Exception as e:
        return {
            "success": False,
            "error": "branch_setup_exception",
            "message": str(e),
        }


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
            description="Start a new code implementation session. "
                        "v1.11: After calling this, follow server instruction + expected_payload. "
                        "All phases advance via submit_phase. Use get_session_status to recover.",
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
                        "description": "Project root path (default: '.')",
                        "default": ".",
                    },
                    "gate_level": {
                        "type": "string",
                        "enum": ["full", "auto"],
                        "description": "Gate level for phase checks. 'full' forces all phases, 'auto' checks necessity.",
                        "default": "auto",
                    },
                    "flags": {
                        "type": "object",
                        "description": "v1.11: Session flags parsed from command options.",
                        "properties": {
                            "no_verify": {"type": "boolean", "default": False, "description": "--no-verify"},
                            "no_quality": {"type": "boolean", "default": False, "description": "--no-quality"},
                            "fast": {"type": "boolean", "default": False, "description": "--fast"},
                            "quick": {"type": "boolean", "default": False, "description": "--quick"},
                            "no_doc": {"type": "boolean", "default": False, "description": "--no-doc"},
                            "no_intervention": {"type": "boolean", "default": False, "description": "--no-intervention / -ni"},
                            "only_explore": {"type": "boolean", "default": False, "description": "--only-explore"},
                        },
                    },
                },
                "required": ["intent", "query"],
            },
        ),
        Tool(
            name="get_session_status",
            description="Get the current session status including phase, instruction, and expected_payload. "
                        "Use this to recover after context compaction.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Path to the target repository (used for checkpoint restoration when no active session exists)",
                    },
                },
            },
        ),
        # v1.11: Unified submit_phase tool (replaces all submit_* tools)
        Tool(
            name="submit_phase",
            description="v1.11: Unified phase submission. The ONLY tool for advancing through phases. "
                        "Server determines current phase from session state and validates payload accordingly. "
                        "Response always includes instruction + expected_payload for next step.",
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "description": "Phase-specific payload. Format varies by current phase - see expected_payload in previous response.",
                    },
                },
                "required": ["data"],
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



# =============================================================================
# v1.11: submit_phase dispatch
# =============================================================================

READY_PAYLOAD_STEP_MAP = {
    "READY_PLAN": 12,
    "READY_IMPL": 13,
    "READY_COMPLETE": 14,
}

_IGNORED_TOOL_CALLS = {"submit_phase"}
_REQUIRED_TOOLS_BY_PAYLOAD: dict[str, set[str]] = {
    "SEMANTIC": {"semantic_search"},
    "IMPACT_ANALYSIS": {"analyze_impact"},
    "READY_IMPL": {"check_write_target"},
    "PRE_COMMIT": {"review_changes"},
}


def _tools_used_in_phase(session: SessionState, phase_name: str) -> set[str]:
    phase_start = None
    for entry in reversed(session.phase_history):
        if entry.get("phase") == phase_name and entry.get("started_at"):
            phase_start = entry.get("started_at")
            break

    def _within_phase(call: dict) -> bool:
        if call.get("phase") != phase_name:
            return False
        if not phase_start:
            return True
        ts = call.get("started_at") or call.get("timestamp")
        if not ts:
            return True
        try:
            return datetime.fromisoformat(ts) >= datetime.fromisoformat(phase_start)
        except Exception:
            return True

    return {
        call.get("tool")
        for call in session.tool_calls
        if _within_phase(call) and call.get("tool") not in _IGNORED_TOOL_CALLS
    }


def _validate_tool_usage(session: SessionState, data: dict, payload_key: str) -> dict | None:
    phase_response = _phase_response_for_payload_key(session, payload_key)
    expected = phase_response.get("expected_payload", EXPECTED_PAYLOADS.get(payload_key, {}))
    if "tools_used" not in expected:
        return None

    actual_tools = _tools_used_in_phase(session, session.phase.name)
    required = _REQUIRED_TOOLS_BY_PAYLOAD.get(payload_key, set())
    reported = set(data.get("tools_used", [])) if isinstance(data.get("tools_used"), list) else set()

    if payload_key == "EXPLORATION" and len(actual_tools) < 2:
        return {
            "error": "payload_mismatch",
            "current_phase": session.phase.name,
            "step": READY_PAYLOAD_STEP_MAP.get(payload_key, PHASE_STEP_MAP.get(payload_key, 0)),
            "message": "EXPLORATION requires at least 2 distinct tools before submit_phase.",
            **phase_response,
        }

    missing = required - actual_tools
    if missing:
        missing_list = ", ".join(sorted(missing))
        return {
            "error": "payload_mismatch",
            "current_phase": session.phase.name,
            "step": READY_PAYLOAD_STEP_MAP.get(payload_key, PHASE_STEP_MAP.get(payload_key, 0)),
            "message": f"Required tools not used: {missing_list}.",
            **phase_response,
        }

    if required and not required.issubset(reported):
        missing_reported = ", ".join(sorted(required - reported))
        return {
            "error": "payload_mismatch",
            "current_phase": session.phase.name,
            "step": READY_PAYLOAD_STEP_MAP.get(payload_key, PHASE_STEP_MAP.get(payload_key, 0)),
            "message": f"tools_used must include required tools: {missing_reported}.",
            **phase_response,
        }

    return None


def _resolve_payload_key_for_submit(session: SessionState, data: dict) -> str:
    if session.phase == Phase.READY:
        if "tasks" in data:
            return "READY_PLAN"
        if "task_id" in data:
            return "READY_IMPL"
        return "READY_COMPLETE"
    return session.phase.name


def _phase_response_for_payload_key(session: SessionState, payload_key: str) -> dict:
    if session.phase == Phase.READY:
        step = READY_PAYLOAD_STEP_MAP.get(payload_key, 12)
        return _phase_response(session, "READY", step=step, extra={"ready_substep": payload_key})
    return _phase_response(session, payload_key)


def _validate_common_payload(session: SessionState, data: dict, payload_key: str) -> dict | None:
    phase_response = _phase_response_for_payload_key(session, payload_key)
    expected = phase_response.get("expected_payload", EXPECTED_PAYLOADS.get(payload_key, {}))

    if "summary" in expected:
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return {
                "error": "payload_mismatch",
                "current_phase": session.phase.name,
                "step": READY_PAYLOAD_STEP_MAP.get(payload_key, PHASE_STEP_MAP.get(payload_key, 0)),
                "message": "summary is required for this phase.",
                **phase_response,
            }

    if "tools_used" in expected:
        tools_used = data.get("tools_used")
        if not isinstance(tools_used, list):
            return {
                "error": "payload_mismatch",
                "current_phase": session.phase.name,
                "step": READY_PAYLOAD_STEP_MAP.get(payload_key, PHASE_STEP_MAP.get(payload_key, 0)),
                "message": "tools_used must be a list (empty list allowed).",
                **phase_response,
            }

    return None


async def _handle_submit_phase(session: SessionState, data: dict) -> dict:
    """
    v1.11: Unified phase submission handler.

    Dispatches based on session.phase. Returns self-contained response
    with instruction + expected_payload for next step.
    """
    phase = session.phase

    # --- Step 2: BRANCH_INTERVENTION ---
    if phase == Phase.BRANCH_INTERVENTION:
        return await _submit_branch_intervention(session, data)

    # --- Step 3: DOCUMENT_RESEARCH ---
    elif phase == Phase.DOCUMENT_RESEARCH:
        return _submit_document_research(session, data)

    # --- Step 4: QUERY_FRAME ---
    elif phase == Phase.QUERY_FRAME:
        return await _submit_query_frame(session, data)

    # --- Step 5: EXPLORATION ---
    elif phase == Phase.EXPLORATION:
        return _submit_exploration_v11(session, data)

    # --- Step 6: Q1 ---
    elif phase == Phase.Q1:
        return await _submit_q1(session, data)

    # --- Step 7: SEMANTIC ---
    elif phase == Phase.SEMANTIC:
        return _submit_semantic_v11(session, data)

    # --- Step 8: Q2 ---
    elif phase == Phase.Q2:
        return await _submit_q2(session, data)

    # --- Step 9: VERIFICATION ---
    elif phase == Phase.VERIFICATION:
        return _submit_verification_v11(session, data)

    # --- Step 10: Q3 ---
    elif phase == Phase.Q3:
        return await _submit_q3(session, data)

    # --- Step 11: IMPACT_ANALYSIS ---
    elif phase == Phase.IMPACT_ANALYSIS:
        return _submit_impact_analysis_v11(session, data)

    # --- Steps 12-14: READY (plan/implement/complete) ---
    elif phase == Phase.READY:
        return _submit_ready(session, data)

    # --- Step 15: POST_IMPL_VERIFY ---
    elif phase == Phase.POST_IMPL_VERIFY:
        return _submit_post_impl_verify(session, data)

    # --- Step 16: VERIFY_INTERVENTION ---
    elif phase == Phase.VERIFY_INTERVENTION:
        return _submit_verify_intervention(session, data)

    # --- Step 17: PRE_COMMIT ---
    elif phase == Phase.PRE_COMMIT:
        return await _submit_pre_commit(session, data)

    # --- Step 18: QUALITY_REVIEW ---
    elif phase == Phase.QUALITY_REVIEW:
        return await _submit_quality_review_v11(session, data)

    # --- Step 19: MERGE ---
    elif phase == Phase.MERGE:
        return await _submit_merge(session, data)

    else:
        return {
            "error": "unknown_phase",
            "current_phase": phase.name,
            "message": f"No handler for phase {phase.name}.",
        }


async def _submit_branch_intervention(session: SessionState, data: dict) -> dict:
    """Step 2: Handle stale branch intervention choice."""
    choice = data.get("choice")
    if choice not in ("delete", "merge", "continue"):
        return {
            "error": "payload_mismatch",
            "current_phase": "BRANCH_INTERVENTION",
            "step": 2,
            "message": "choice must be 'delete', 'merge', or 'continue'.",
            **_phase_response(session, "BRANCH_INTERVENTION"),
        }

    repo_path = session.repo_path or "."

    if choice == "delete":
        cleanup_result = await BranchManager.cleanup_stale_sessions(repo_path, action="delete")
    elif choice == "merge":
        cleanup_result = await BranchManager.cleanup_stale_sessions(repo_path, action="merge")
    else:
        cleanup_result = {"action": "continue"}
        session.branch_policy = "continue"
        try:
            branch_status = await BranchManager.is_task_branch_checked_out(repo_path)
            if branch_status.get("is_task_branch"):
                session.task_branch_enabled = True
                session.task_branch_name = branch_status.get("current_branch")
        except Exception:
            pass

    if choice in ("delete", "merge"):
        session.branch_policy = None

    # Determine next phase
    next_phase = Phase.DOCUMENT_RESEARCH
    if session.no_doc:
        next_phase = Phase.QUERY_FRAME

    session.transition_to_phase(next_phase, reason=f"branch_intervention_{choice}")

    return {
        "success": True,
        "choice": choice,
        "cleanup_result": cleanup_result if choice != "continue" else None,
        **_phase_response(session, next_phase.name),
    }


def _submit_document_research(session: SessionState, data: dict) -> dict:
    """Step 3: Handle document research results."""
    documents_reviewed = data.get("documents_reviewed", [])

    session.phase_history.append({
        "phase": "DOCUMENT_RESEARCH",
        "documents_reviewed": documents_reviewed,
        "timestamp": datetime.now().isoformat(),
    })

    session.transition_to_phase(Phase.QUERY_FRAME, reason="document_research_complete")

    return {
        "success": True,
        "documents_reviewed": len(documents_reviewed),
        **_phase_response(session, "QUERY_FRAME"),
    }


async def _submit_query_frame(session: SessionState, data: dict) -> dict:
    """Step 4: Handle query frame extraction."""
    from tools.query_frame import QueryFrame, QueryDecomposer, validate_slot, SlotSource, generate_investigation_guidance

    raw_query = session.query
    frame = QueryFrame(raw_query=raw_query)

    # Extract slots from data
    slot_names = ["target_feature", "trigger_condition", "observed_issue", "desired_action"]
    for slot_name in slot_names:
        slot_data = data.get(slot_name)
        if slot_data is not None:
            if isinstance(slot_data, dict):
                value, validated_quote = validate_slot(slot_name, slot_data, raw_query)
                if value is not None:
                    setattr(frame, slot_name, value)
                    frame.slot_quotes[slot_name] = validated_quote or ""
                    frame.slot_source[slot_name] = SlotSource.FACT
            elif isinstance(slot_data, str):
                setattr(frame, slot_name, slot_data)

    session.query_frame = frame

    # Also accept action_type, target_symbols, scope, constraints (v1.11 new format)
    if "action_type" in data:
        frame.desired_action = data.get("action_type")

    # Determine next phase
    if session.fast_mode or session.quick_mode:
        # Skip exploration → READY
        next_phase = Phase.READY
        session.transition_to_phase(next_phase, reason="query_frame_complete_skip_exploration")
        response = _phase_response(session, "READY", extra={"ready_substep": "READY_PLAN"})
        # --fast: create branch for READY (--quick: no branch)
        if session.fast_mode:
            branch_result = await _create_branch_for_ready(session)
            if branch_result.get("branch"):
                response["branch"] = branch_result["branch"]
    else:
        next_phase = Phase.EXPLORATION
        session.transition_to_phase(next_phase, reason="query_frame_complete")
        response = _phase_response(session, "EXPLORATION")

    return {
        "success": True,
        "query_frame": {
            "target_feature": frame.target_feature,
            "trigger_condition": frame.trigger_condition,
            "observed_issue": frame.observed_issue,
            "desired_action": frame.desired_action,
        },
        **response,
    }


def _submit_exploration_v11(session: SessionState, data: dict) -> dict:
    """Step 5: Handle exploration results via submit_phase."""
    explored_files = data.get("explored_files", [])
    findings = data.get("findings", [])

    # Also support legacy field names for compatibility
    symbols_identified = data.get("symbols_identified", [])
    entry_points = data.get("entry_points", [])
    existing_patterns = data.get("existing_patterns", [])
    files_analyzed = data.get("files_analyzed", explored_files)

    # Auto-populate tools_used from current phase history
    tools_used = list(_tools_used_in_phase(session, session.phase.name))

    exploration = ExplorationResult(
        symbols_identified=symbols_identified,
        entry_points=entry_points,
        existing_patterns=existing_patterns,
        files_analyzed=files_analyzed,
        tools_used=tools_used,
        notes="; ".join(findings) if findings else "",
    )

    session.exploration = exploration

    session.phase_history.append({
        "phase": "EXPLORATION",
        "explored_files": explored_files,
        "findings": findings,
        "timestamp": datetime.now().isoformat(),
    })

    # Transition to Q1
    session.transition_to_phase(Phase.Q1, reason="exploration_complete")

    return {
        "success": True,
        "explored_files_count": len(files_analyzed),
        **_phase_response(session, "Q1"),
    }


async def _submit_q1(session: SessionState, data: dict) -> dict:
    """Step 6: Q1 - SEMANTIC necessity evaluation."""
    needs_more = data.get("needs_more_information", False)
    reason = data.get("reason", "")

    session.phase_assessments["Q1"] = {
        "needs_more_information": needs_more,
        "reason": reason,
    }

    # gate_level="full" forces execution
    if session.gate_level == "full":
        needs_more = True

    if needs_more:
        session.transition_to_phase(Phase.SEMANTIC, reason="q1_needs_semantic")
        return {
            "success": True,
            "decision": "SEMANTIC required",
            **_phase_response(session, "SEMANTIC"),
        }
    else:
        # Skip SEMANTIC → Q2
        session.transition_to_phase(Phase.Q2, reason="q1_skip_semantic")
        return {
            "success": True,
            "decision": "SEMANTIC skipped",
            **_phase_response(session, "Q2"),
        }


def _submit_semantic_v11(session: SessionState, data: dict) -> dict:
    """Step 7: Handle semantic search results."""
    search_results = data.get("search_results", [])

    session.phase_history.append({
        "phase": "SEMANTIC",
        "search_results_count": len(search_results),
        "timestamp": datetime.now().isoformat(),
    })

    # Transition to Q2
    session.transition_to_phase(Phase.Q2, reason="semantic_complete")

    return {
        "success": True,
        "search_results_count": len(search_results),
        **_phase_response(session, "Q2"),
    }


async def _submit_q2(session: SessionState, data: dict) -> dict:
    """Step 8: Q2 - VERIFICATION necessity evaluation."""
    has_unverified = data.get("has_unverified_hypotheses", False)
    reason = data.get("reason", "")

    session.phase_assessments["Q2"] = {
        "has_unverified_hypotheses": has_unverified,
        "reason": reason,
    }

    if session.gate_level == "full":
        has_unverified = True

    if has_unverified:
        session.transition_to_phase(Phase.VERIFICATION, reason="q2_needs_verification")
        return {
            "success": True,
            "decision": "VERIFICATION required",
            **_phase_response(session, "VERIFICATION"),
        }
    else:
        session.transition_to_phase(Phase.Q3, reason="q2_skip_verification")
        return {
            "success": True,
            "decision": "VERIFICATION skipped",
            **_phase_response(session, "Q3"),
        }


def _submit_verification_v11(session: SessionState, data: dict) -> dict:
    """Step 9: Handle verification results."""
    hypotheses_verified = data.get("hypotheses_verified", [])

    session.phase_history.append({
        "phase": "VERIFICATION",
        "verified_count": len(hypotheses_verified),
        "timestamp": datetime.now().isoformat(),
    })

    # Transition to Q3
    session.transition_to_phase(Phase.Q3, reason="verification_complete")

    return {
        "success": True,
        "verified_count": len(hypotheses_verified),
        **_phase_response(session, "Q3"),
    }


async def _submit_q3(session: SessionState, data: dict) -> dict:
    """Step 10: Q3 - IMPACT_ANALYSIS necessity evaluation."""
    needs_impact = data.get("needs_impact_analysis", False)
    reason = data.get("reason", "")

    session.phase_assessments["Q3"] = {
        "needs_impact_analysis": needs_impact,
        "reason": reason,
    }

    if session.gate_level == "full":
        needs_impact = True

    if needs_impact:
        session.transition_to_phase(Phase.IMPACT_ANALYSIS, reason="q3_needs_impact")
        return {
            "success": True,
            "decision": "IMPACT_ANALYSIS required",
            **_phase_response(session, "IMPACT_ANALYSIS"),
        }
    else:
        # Skip IMPACT_ANALYSIS
        # For INVESTIGATE/QUESTION → SESSION_COMPLETE
        if session.intent in ("INVESTIGATE", "QUESTION") or session.skip_implementation:
            return {
                "success": True,
                "decision": "SESSION_COMPLETE (investigation only)",
                "session_complete": True,
                "message": "探索完了。セッションを終了します。",
            }

        # For IMPLEMENT/MODIFY → READY
        session.transition_to_phase(Phase.READY, reason="q3_skip_impact_to_ready")

        # Create branch for READY
        branch_result = await _create_branch_for_ready(session)

        response = _phase_response(session, "READY", extra={"ready_substep": "READY_PLAN"})
        if branch_result.get("branch"):
            response["branch"] = branch_result["branch"]

        return {
            "success": True,
            "decision": "IMPACT_ANALYSIS skipped → READY",
            **response,
        }


def _submit_impact_analysis_v11(session: SessionState, data: dict) -> dict:
    """Step 11: Handle impact analysis results."""
    impact_summary = data.get("impact_summary", {})

    session.phase_history.append({
        "phase": "IMPACT_ANALYSIS",
        "impact_summary": impact_summary,
        "timestamp": datetime.now().isoformat(),
    })

    # For INVESTIGATE/QUESTION → SESSION_COMPLETE
    if session.intent in ("INVESTIGATE", "QUESTION") or session.skip_implementation:
        return {
            "success": True,
            "session_complete": True,
            "message": "探索完了。セッションを終了します。",
        }

    # For IMPLEMENT/MODIFY → READY
    session.transition_to_phase(Phase.READY, reason="impact_analysis_complete")

    return {
        "success": True,
        **_phase_response(session, "READY", extra={"ready_substep": "READY_PLAN"}),
    }


def _submit_ready(session: SessionState, data: dict) -> dict:
    """Steps 12-14: READY phase dispatches to sub-steps."""

    # --- Step 12: Task Plan (has "tasks" key) ---
    if "tasks" in data:
        result = session.register_tasks(data["tasks"])
        if "error" in result:
            return {
                **result,
                **_phase_response(session, "READY", step=12, extra={"ready_substep": "READY_PLAN"}),
            }

        # Read task_planning.md if exists
        task_planning_path = Path(session.repo_path or ".") / ".code-intel" / "task_planning.md"
        planning_guide = None
        if task_planning_path.exists():
            try:
                planning_guide = task_planning_path.read_text(encoding="utf-8")[:2000]
            except Exception:
                pass

        response = {
            **result,
            **_phase_response(session, "READY", step=13, extra={"ready_substep": "READY_IMPL"}),
        }
        if planning_guide:
            response["planning_guide"] = planning_guide
        return response

    # --- Step 13: Task Completion (has "task_id" key) ---
    if "task_id" in data:
        task_id = data["task_id"]
        summary = data.get("summary", "")
        return session.complete_task(task_id, summary)

    # --- Step 14: Implementation Complete (empty data or explicit) ---
    check = session.check_all_tasks_complete()
    if "error" in check:
        return {
            **check,
            **_phase_response(session, "READY", step=14, extra={"ready_substep": "READY_COMPLETE"}),
        }

    # All tasks complete → determine next phase
    if session.no_verify and session.quick_mode:
        return {
            "success": True,
            "session_complete": True,
            "message": "全タスク完了。セッション終了（--no-verify + --quick）。",
        }
    elif session.no_verify:
        session.transition_to_phase(Phase.PRE_COMMIT, reason="ready_complete_no_verify")
        return {
            "success": True,
            **_phase_response(session, "PRE_COMMIT"),
        }
    else:
        session.transition_to_phase(Phase.POST_IMPL_VERIFY, reason="ready_complete")
        return {
            "success": True,
            **_phase_response(session, "POST_IMPL_VERIFY"),
        }


def _submit_post_impl_verify(session: SessionState, data: dict) -> dict:
    """Step 15: POST_IMPL_VERIFY."""
    passed = data.get("passed", False)
    details = data.get("details", "")
    failed_tasks = data.get("failed_tasks", [])

    if passed:
        if session.quick_mode:
            return {
                "success": True,
                "session_complete": True,
                "message": "検証通過。セッション終了（--quick）。",
            }
        session.transition_to_phase(Phase.PRE_COMMIT, reason="post_impl_verify_passed")
        return {
            "success": True,
            "verified": True,
            **_phase_response(session, "PRE_COMMIT"),
        }
    else:
        # Increment failure_count for failed tasks
        for task_id in failed_tasks:
            for t in session.tasks:
                if t.id == task_id:
                    t.failure_count += 1
                    break

        # Check if any task has failure_count >= 3
        high_failure = any(t.failure_count >= 3 for t in session.tasks)

        if high_failure:
            # Check if intervention is allowed
            if session.no_intervention or session.quick_mode:
                # Skip intervention → revert to READY
                session.transition_to_phase(Phase.READY, reason="post_impl_verify_failed_skip_intervention")
                session.ready_substep = "plan"
                return {
                    "success": True,
                    "revert_to": "READY",
                    "reason": "Verification failed (intervention skipped).",
                    "existing_tasks": [t.to_dict() for t in session.tasks],
                    **_phase_response(session, "READY", step=12, extra={"ready_substep": "READY_PLAN"}),
                }
            else:
                # → VERIFY_INTERVENTION
                session.transition_to_phase(Phase.VERIFY_INTERVENTION, reason="post_impl_verify_3_failures")
                return {
                    "success": True,
                    **_phase_response(session, "VERIFY_INTERVENTION"),
                    "failure_details": details,
                    "failed_tasks": failed_tasks,
                    "high_failure_tasks": [t.to_dict() for t in session.tasks if t.failure_count >= 3],
                }
        else:
            # Revert to READY for retry
            session.transition_to_phase(Phase.READY, reason="post_impl_verify_failed")
            session.ready_substep = "plan"
            return {
                "success": True,
                "revert_to": "READY",
                "reason": f"Verification failed: {details}",
                "existing_tasks": [t.to_dict() for t in session.tasks],
                **_phase_response(session, "READY", step=12, extra={"ready_substep": "READY_PLAN"}),
            }


def _submit_verify_intervention(session: SessionState, data: dict) -> dict:
    """Step 16: VERIFY_INTERVENTION."""
    prompt_used = data.get("prompt_used", "")
    action_taken = data.get("action_taken", "")

    session.intervention_count += 1

    # Reset failure counts for all tasks
    for t in session.tasks:
        t.failure_count = 0

    session.phase_history.append({
        "phase": "VERIFY_INTERVENTION",
        "prompt_used": prompt_used,
        "action_taken": action_taken,
        "intervention_count": session.intervention_count,
        "timestamp": datetime.now().isoformat(),
    })

    # Check if user escalation is needed
    if session.intervention_count >= 2:
        response = _phase_response(session, "VERIFY_INTERVENTION")
        response["instruction"] = (
            ".code-intel/user_escalation.md の手順に従いユーザーへヘルプ要請を行ってください。"
            "使用した手順書の名前/パスを明記して submit_phase で送信してください。"
        )
        return {
            "success": True,
            "user_escalation": True,
            "intervention_count": session.intervention_count,
            "message": f"介入を {session.intervention_count} 回実行しました。ユーザーに相談してください。",
            **response,
        }

    # Revert to READY
    session.transition_to_phase(Phase.READY, reason="verify_intervention_complete")
    session.ready_substep = "plan"

    return {
        "success": True,
        "intervention_count": session.intervention_count,
        "failure_counts_reset": True,
        "existing_tasks": [t.to_dict() for t in session.tasks],
        **_phase_response(session, "READY", step=12, extra={"ready_substep": "READY_PLAN"}),
    }


async def _submit_pre_commit(session: SessionState, data: dict) -> dict:
    """Step 17: PRE_COMMIT."""
    reviewed_files = data.get("reviewed_files", [])
    commit_message = data.get("commit_message", "")

    if not commit_message:
        return {
            "error": "missing_commit_message",
            "message": "commit_message is required.",
            **_phase_response(session, "PRE_COMMIT"),
        }

    # Get branch manager
    repo_path = session.repo_path or "."
    branch_manager = _get_or_recreate_branch_manager(session, repo_path)
    if branch_manager is None:
        return {
            "error": "branch_manager_not_found",
            "message": "Branch manager not found.",
        }

    # Submit review to session
    review_result = session.submit_pre_commit_review(
        reviewed_files=[{"path": f, "decision": "keep"} for f in reviewed_files] if isinstance(reviewed_files, list) and reviewed_files and isinstance(reviewed_files[0], str) else reviewed_files,
        review_notes=commit_message,
    )

    if not review_result.get("success"):
        return review_result

    # Determine whether to execute commit now
    execute_commit_now = not session.quality_review_enabled

    finalize_result = await branch_manager.finalize(
        keep_files=review_result.get("kept_files"),
        discard_files=review_result.get("discarded_files"),
        commit_message=commit_message,
        execute_commit=execute_commit_now,
    )

    if not finalize_result.success:
        return {
            "error": "finalize_failed",
            "message": finalize_result.error,
        }

    # Store preparation state
    if finalize_result.prepared:
        session.commit_prepared = True
        session.prepared_commit_message = commit_message
        session.prepared_kept_files = finalize_result.kept_files
        session.prepared_discarded_files = finalize_result.discarded_files

    # Determine next phase
    # --fast and --quick skip QUALITY_REVIEW per phase matrix
    skip_quality = session.fast_mode or session.quick_mode or not session.quality_review_enabled
    if not skip_quality:
        # Check if quality_review.md exists
        quality_review_path = Path(repo_path) / ".code-intel" / "review_prompts" / "quality_review.md"
        if quality_review_path.exists():
            session.transition_to_phase(Phase.QUALITY_REVIEW, reason="pre_commit_to_quality")
            return {
                "success": True,
                "commit_hash": None if finalize_result.prepared else finalize_result.commit_hash,
                "prepared": finalize_result.prepared,
                **_phase_response(session, "QUALITY_REVIEW"),
            }
        # else: skip quality review

    # Skipped quality review → mark as completed so MERGE doesn't block
    session.quality_review_completed = True
    # No quality review → MERGE
    session.transition_to_phase(Phase.MERGE, reason="pre_commit_to_merge")
    return {
        "success": True,
        "commit_hash": finalize_result.commit_hash,
        **_phase_response(session, "MERGE"),
    }


async def _submit_quality_review_v11(session: SessionState, data: dict) -> dict:
    """Step 18: QUALITY_REVIEW."""
    quality_score = data.get("quality_score", "")
    issues = data.get("issues", [])

    has_issues = bool(issues)

    if has_issues:
        session.quality_revert_count += 1

        # Check forced completion
        if session.quality_revert_count >= session.quality_review_max_revert:
            session.quality_review_completed = True
            session.transition_to_phase(Phase.MERGE, reason="quality_forced_completion")
            return {
                "success": True,
                "forced_completion": True,
                "warning": "品質問題が未解決のまま完了",
                "quality_revert_count": session.quality_revert_count,
                **_phase_response(session, "MERGE"),
            }

        # Clear commit preparation
        session.commit_prepared = False
        session.prepared_commit_message = None
        session.prepared_kept_files = []
        session.prepared_discarded_files = []

        # Revert to READY
        session.transition_to_phase(Phase.READY, reason="quality_review_issues")
        session.ready_substep = "plan"

        return {
            "success": True,
            "issues_found": True,
            "issues": issues,
            "quality_revert_count": session.quality_revert_count,
            "existing_tasks": [t.to_dict() for t in session.tasks],
            **_phase_response(session, "READY", step=12, extra={"ready_substep": "READY_PLAN"}),
        }
    else:
        # Execute prepared commit if exists
        commit_hash = None
        if session.commit_prepared:
            repo_path = session.repo_path or "."
            branch_manager = _get_or_recreate_branch_manager(session, repo_path)
            if branch_manager:
                commit_result = await branch_manager.execute_prepared_commit(
                    commit_message=session.prepared_commit_message or "Quality review passed"
                )
                if commit_result.success:
                    commit_hash = commit_result.commit_hash
                    session.commit_prepared = False

        session.quality_review_completed = True
        session.transition_to_phase(Phase.MERGE, reason="quality_review_passed")

        return {
            "success": True,
            "issues_found": False,
            "commit_hash": commit_hash,
            **_phase_response(session, "MERGE"),
        }


async def _submit_merge(session: SessionState, data: dict) -> dict:
    """Step 19: MERGE."""
    if not session.task_branch_enabled:
        return {
            "success": True,
            "session_complete": True,
            "message": "No task branch. Session complete.",
        }

    # Check quality review
    if session.quality_review_enabled and not session.quality_review_completed:
        return {
            "error": "quality_review_required",
            "message": "QUALITY_REVIEW not completed.",
            **_phase_response(session, "QUALITY_REVIEW"),
        }

    repo_path = session.repo_path or "."
    branch_manager = _get_or_recreate_branch_manager(session, repo_path)
    if branch_manager is None:
        return {
            "error": "branch_manager_not_found",
            "message": "Branch manager not found.",
        }

    merge_result = await branch_manager.merge_to_base()

    if merge_result["success"]:
        # Cleanup
        if session.session_id in _branch_managers:
            del _branch_managers[session.session_id]

        return {
            "success": True,
            "session_complete": True,
            "merged": True,
            "from_branch": merge_result.get("from_branch"),
            "to_branch": merge_result.get("to_branch"),
            "message": f"Merged {merge_result.get('from_branch')} → {merge_result.get('to_branch')}. Session complete.",
        }
    else:
        return {
            "success": False,
            "error": merge_result.get("error"),
            "message": "Merge failed.",
        }


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
    """Execute a code intelligence tool (v1.12: with response truncation)."""
    result = await _call_tool_impl(name, arguments)

    # v1.12: Apply response size limit to all tool responses
    if result and len(result) > 0:
        content = result[0].text
        truncated_content, was_truncated = _truncate_response(content)
        if was_truncated:
            result = [TextContent(type="text", text=truncated_content)]

    return result


async def _call_tool_impl(name: str, arguments: dict) -> list[TextContent]:
    """Execute a code intelligence tool (implementation)."""

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
        gate_level = arguments.get("gate_level", "auto")

        # v1.11: Parse flags
        flags = arguments.get("flags", {})
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except json.JSONDecodeError:
                flags = {}

        # Legacy compatibility
        skip_quality = arguments.get("skip_quality", flags.get("no_quality", False))
        skip_implementation = arguments.get("skip_implementation", flags.get("only_explore", False))

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

        # v1.11: Set session flags
        session.no_verify = flags.get("no_verify", False)
        session.no_quality = skip_quality
        session.fast_mode = flags.get("fast", False)
        session.quick_mode = flags.get("quick", False)
        session.no_doc = flags.get("no_doc", False)
        session.no_intervention = flags.get("no_intervention", False)

        # v1.11: Determine initial phase based on flags
        # start_session → check for stale branches → DOCUMENT_RESEARCH or QUERY_FRAME
        # For now, set initial phase to BRANCH_INTERVENTION (server will check stale branches)
        # or skip to DOCUMENT_RESEARCH/QUERY_FRAME if --fast/--quick
        # v1.11: All modes check stale branches first (Step 2), then proceed:
        #   stale found → BRANCH_INTERVENTION
        #   no stale + --no-doc → QUERY_FRAME
        #   no stale → DOCUMENT_RESEARCH
        # Exploration skip (--fast/--quick) is handled in QUERY_FRAME → READY
        stale_info = await BranchManager.list_stale_branches(repo_path)
        stale_branches = stale_info.get("stale_branches", [])
        if stale_branches:
            session.transition_to_phase(Phase.BRANCH_INTERVENTION, reason="start_session_stale_found")
        elif session.no_doc:
            session.transition_to_phase(Phase.QUERY_FRAME, reason="start_session_skip_doc")
        else:
            session.transition_to_phase(Phase.DOCUMENT_RESEARCH, reason="start_session_no_stale")

        # Get extraction prompt for QueryFrame
        extraction_prompt = QueryDecomposer.get_extraction_prompt(query)

        # v1.11: Self-contained response
        first_phase = session.phase.name
        first_response = _phase_response(session, first_phase)

        result = {
            "success": True,
            "session_id": session.session_id,
            "intent": session.intent,
            "gate_level": gate_level,
            "flags": {
                "no_verify": session.no_verify,
                "no_quality": session.no_quality,
                "fast": session.fast_mode,
                "quick": session.quick_mode,
                "no_doc": session.no_doc,
                "no_intervention": session.no_intervention,
            },
            # v1.11: Self-contained phase response
            **first_response,
            # v3.6: QueryFrame extraction prompt (for Step 4)
            "query_frame_extraction_prompt": extraction_prompt,
        }

        # v1.11: Include stale branch info for BRANCH_INTERVENTION
        if stale_branches:
            result["stale_branches"] = stale_branches

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
                "Then call submit_phase to proceed."
            )

        # v1.12: Check for existing checkpoints and notify recovery_available
        try:
            checkpoints = session_manager.list_checkpoints(repo_path)
            # Filter out the current session's checkpoint (it was just created)
            old_checkpoints = [cp for cp in checkpoints if cp["session_id"] != session.session_id]
            if old_checkpoints:
                latest = old_checkpoints[-1]
                result["recovery_available"] = True
                result["checkpoint_info"] = latest
        except Exception:
            pass

        # v1.12: Save initial checkpoint
        session_manager.save_checkpoint(session.session_id, repo_path)

        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "get_session_status":
        session = session_manager.get_active_session()
        if session is None:
            # v1.12: Auto-restore from checkpoint
            repo_path = arguments.get("repo_path", ".")
            checkpoints = session_manager.list_checkpoints(repo_path)
            if checkpoints:
                latest = checkpoints[-1]  # sorted by checkpoint_at
                restored = session_manager.load_checkpoint(latest["session_id"], repo_path)
                if restored:
                    result = restored.get_status()
                    result["recovered_from_checkpoint"] = True
                    result["checkpoint_session_id"] = latest["session_id"]
                else:
                    result = {
                        "error": "no_active_session",
                        "message": "No active session. Checkpoint found but failed to restore.",
                    }
            else:
                result = {
                    "error": "no_active_session",
                    "message": "No active session. Use start_session first.",
                }
        else:
            result = session.get_status()
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    # =========================================================================
    # v1.11: Unified submit_phase handler
    # =========================================================================
    elif name == "submit_phase":
        session = session_manager.get_active_session()
        if session is None:
            result = {"error": "no_active_session", "message": "No active session. Use start_session first."}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        data = arguments.get("data", {})
        # Handle string-serialized data (MCP client workaround)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as e:
                result = {"error": "invalid_data", "message": f"Failed to parse data JSON: {e}"}
                return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        payload_key = _resolve_payload_key_for_submit(session, data)
        validation_error = _validate_common_payload(session, data, payload_key)
        if validation_error:
            return [TextContent(type="text", text=json.dumps(validation_error, indent=2, ensure_ascii=False))]
        tool_validation_error = _validate_tool_usage(session, data, payload_key)
        if tool_validation_error:
            return [TextContent(type="text", text=json.dumps(tool_validation_error, indent=2, ensure_ascii=False))]

        result = await _handle_submit_phase(session, data)

        if result and not result.get("error"):
            summary = data.get("summary")
            if isinstance(summary, str) and summary.strip():
                step = READY_PAYLOAD_STEP_MAP.get(payload_key, PHASE_STEP_MAP.get(payload_key, 0))
                if step:
                    session.record_phase_summary(payload_key, step, summary)

        # v1.12: Checkpoint persistence after submit_phase
        if result and not result.get("error"):
            repo_path = session.repo_path or "."
            session_complete = result.get("decision", "").startswith("SESSION_COMPLETE") or \
                               result.get("session_complete", False)
            if session_complete:
                # Delete checkpoint on SESSION_COMPLETE (no need to save first)
                SessionManager.delete_checkpoint(session.session_id, repo_path)
            else:
                session_manager.save_checkpoint(session.session_id, repo_path)

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

    elif name == "cleanup_stale_branches":
        # v1.2.1: Clean up stale task branches from interrupted runs
        # v1.10: Added action parameter (delete/merge)
        repo_path = arguments.get("repo_path", ".")
        action = arguments.get("action", "delete")

        cleanup_result = await BranchManager.cleanup_stale_sessions(repo_path, action=action)

        deleted_count = len(cleanup_result.get("deleted_branches", []))
        merged_count = len(cleanup_result.get("merged_branches", []))
        checked_out_to = cleanup_result.get("checked_out_to")

        # v1.12: Also delete session checkpoint files
        checkpoints_deleted = SessionManager.delete_checkpoints(repo_path)

        if action == "merge":
            message = f"Merged {merged_count} branches, deleted {deleted_count} stale branches."
        else:
            message = f"Cleaned up {deleted_count} stale branches."
        if checked_out_to:
            message = f"Checked out to '{checked_out_to}'. " + message
        if checkpoints_deleted > 0:
            message += f" Deleted {checkpoints_deleted} session checkpoint(s)."

        result = {
            "success": True,
            "action": action,
            "deleted_branches": cleanup_result.get("deleted_branches", []),
            "errors": cleanup_result.get("errors", []),
            "message": message,
            "checkpoints_deleted": checkpoints_deleted,
        }

        if cleanup_result.get("merged_branches"):
            result["merged_branches"] = cleanup_result["merged_branches"]
        if checked_out_to:
            result["checked_out_to"] = checked_out_to

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
                "action": "submit_phase",
                "instructions": (
                    "1. Review must_verify files and check if changes affect them\n"
                    "2. Review should_verify files (tests, factories, seeders)\n"
                    "3. Use project_rules to infer additional related files\n"
                    "4. Call submit_phase with verified_files and inferred_from_rules"
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

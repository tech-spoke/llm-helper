# Code Intelligence MCP Server v1.0 Internal Documentation

This document explains the internal workings of the system at a level that other AIs can understand.

---

## Table of Contents

1. [Router](#router)
2. [SessionState](#sessionstate)
3. [QueryFrame](#queryframe)
4. [ChromaDB Manager](#chromadb-manager)
5. [AST Chunker](#ast-chunker)
6. [Embedding](#embedding)
7. [Tool Implementation Details](#tool-implementation-details)
8. [Improvement Cycle](#improvement-cycle)

---

## Router

`tools/router.py`

### Overview

Router analyzes natural language queries and generates appropriate tool execution plans (ExecutionPlan).

### Class Structure

```python
class Router:
    """Analyzes queries and generates tool execution plans"""

    def create_plan(
        self,
        query_frame: QueryFrame,
        intent: str,
        context: dict
    ) -> ExecutionPlan:
        """
        Generate ExecutionPlan from QueryFrame and intent

        Processing flow:
        1. Analyze QueryFrame slots
        2. Determine required phases based on Intent
        3. Select tools for each phase
        4. Generate DecisionLog
        5. Return ExecutionPlan
        """
```

### ExecutionPlan

```python
@dataclass
class ExecutionPlan:
    steps: list[ExecutionStep]      # List of tools to execute
    reasoning: str                   # Reason for plan
    needs_bootstrap: bool            # Whether initial exploration needed
    decision_log: DecisionLog | None # Decision log
    intent: IntentType               # Intent type
    risk_level: str                  # HIGH/MEDIUM/LOW
    missing_slots: list[str]         # Unresolved slots
```

### DecisionLog

```python
@dataclass
class DecisionLog:
    """Decision record for improvement cycle"""
    query: str                    # Original query
    timestamp: str                # Timestamp
    intent: str                   # Intent type
    required_phases: list[str]    # Required phases
    missing_slots: list[str]      # Unresolved slots
    risk_level: str               # Risk level
    tools_planned: list[str]      # Planned tools
    needs_bootstrap: bool         # Initial exploration needed
    bootstrap_reason: str | None  # Reason for initial exploration
    session_id: str | None        # Session ID
```

### Tool Selection Logic

```python
def _select_tools(self, query_frame: QueryFrame, intent: str) -> list[str]:
    """
    Select tools based on slots and Intent

    Rules:
    - target_feature unknown → find_definitions, get_symbols
    - trigger_condition unknown → search_text
    - observed_issue unknown → search_text, query
    - desired_action unknown → find_references, analyze_structure

    Adjustment by Intent:
    - IMPLEMENT: All tools recommended
    - MODIFY: Prioritize find_references
    - INVESTIGATE: Prioritize query, analyze_structure
    - QUESTION: Minimum
    """
```

---

## SessionState

`tools/session.py`

### Overview

SessionState manages the state of a single implementation session. It tracks phase transitions, validation, and tool usage history.

### Class Structure

```python
class SessionState:
    session_id: str
    intent: str                    # IMPLEMENT/MODIFY/INVESTIGATE/QUESTION
    query: str                     # Original query
    phase: Phase                   # Current phase
    created_at: str
    repo_path: str                 # Project path

    # Phase results
    exploration: ExplorationResult | None
    semantic: SemanticResult | None
    verification: VerificationResult | None

    # QueryFrame
    query_frame: QueryFrame | None
    risk_level: str

    # Tool usage history
    tool_calls: list[dict]
```

### Phase Enum

```python
class Phase(Enum):
    EXPLORATION = "exploration"     # Code exploration
    SEMANTIC = "semantic"           # Semantic search
    VERIFICATION = "verification"   # Hypothesis verification
    READY = "ready"                 # Implementation allowed
```

### Phase Transitions

```python
def submit_understanding(self, ...) -> dict:
    """
    Complete EXPLORATION phase

    Processing:
    1. Input consistency check (validate_exploration_consistency)
    2. Evaluate exploration results (evaluate_exploration)
    3. NL→Symbol mapping verification
    4. Determine next phase

    Transition:
    - Evaluation "high" + consistency OK → READY
    - Otherwise → SEMANTIC
    """

def submit_semantic(self, ...) -> dict:
    """
    Complete SEMANTIC phase

    Processing:
    1. Check validity of semantic_reason (validate_semantic_reason)
    2. Record hypotheses
    3. Transition to VERIFICATION
    """

def submit_verification(self, ...) -> dict:
    """
    Complete VERIFICATION phase

    Processing:
    1. Record verification results for each hypothesis
    2. Transition to READY
    """
```

### Validation Functions

```python
def validate_exploration_consistency(
    symbols: list[str],
    entry_points: list[str],
    files: list[str],
    patterns: list[str]
) -> tuple[bool, list[str]]:
    """
    Check exploration result consistency

    Checks:
    - entry_points linked to symbols
    - No duplicate symbols/files
    - If patterns exist, files required

    Returns:
        (is_valid, error_messages)
    """

def validate_semantic_reason(
    reason: str,
    missing_requirements: list[str]
) -> tuple[bool, str]:
    """
    Check validity of SEMANTIC reason

    Allowed combinations:
    - symbols_identified insufficient → no_definition_found, architecture_unknown
    - entry_points insufficient → no_definition_found, no_reference_found
    - patterns insufficient → no_similar_implementation, architecture_unknown
    - files insufficient → context_fragmented, architecture_unknown
    """

def validate_write_target(
    file_path: str,
    explored_files: list[str],
    allow_new_files: bool
) -> tuple[bool, str]:
    """
    Verify write target

    Rules:
    - Existing file → must be in explored_files
    - New file → allow_new_files=True and parent directory explored
    """
```

### Exploration Evaluation

```python
def evaluate_exploration(
    intent: str,
    symbols_count: int,
    entry_points_count: int,
    files_count: int,
    patterns_count: int,
    tools_used: set[str]
) -> tuple[str, list[str]]:
    """
    Evaluate exploration results

    Requirements by Intent:
    - IMPLEMENT/MODIFY:
        - symbols: 3 or more
        - entry_points: 1 or more
        - files: 2 or more
        - patterns: 1 or more
        - tools: find_definitions, find_references used

    - INVESTIGATE:
        - symbols: 1 or more
        - files: 1 or more

    - QUESTION:
        - No requirements (immediate READY)

    Returns:
        (confidence: "high"/"low", missing_requirements)
    """
```

### Markup Context Relaxation (v1.1)

```python
# Markup file extensions (pure markup only)
# Note: .blade.php, .vue, .jsx, .tsx, .svelte etc. are excluded
# These are tightly coupled with logic, where find_definitions/find_references are effective
MARKUP_EXTENSIONS = {
    ".html", ".htm",                    # Static HTML
    ".css", ".scss", ".sass", ".less",  # Stylesheets
    ".xml", ".svg",                     # Data/Graphics
    ".md", ".markdown",                 # Documentation
}

def is_markup_context(files: list[str]) -> bool:
    """Determine if all explored files are markup"""
```

**Excluded files (tightly coupled with logic):**
- `.blade.php` - Laravel: Coupled with PHP via `@if`, `@foreach`, `{{ $var }}`
- `.vue` - Vue: JS logic in `<script>` section
- `.jsx`, `.tsx` - React: JavaScript and HTML mixed
- `.svelte` - Svelte: Has `<script>` section
- `.twig`, `.ejs`, `.pug` etc. - Server-side logic coupling

**Relaxed requirements:**

| Item | Normal (Logic) | Markup |
|------|----------------|--------|
| symbols_identified | 3 or more | 0 (not required) |
| entry_points | 1 or more | 0 (not required) |
| files_analyzed | 2 or more | 1 or more |
| existing_patterns | 1 or more | 0 (not required) |
| required_tools | find_definitions, find_references | search_text only |
| trigger_condition | Required (HIGH risk if missing) | Optional |

**Design rationale:**
- Static HTML/CSS has no concept of "symbols"
- find_definitions/find_references are meaningless
- trigger_condition (reproduction condition) is unnatural for style changes

**Evaluation timing:**
- Check file extensions of `files_analyzed` at `submit_exploration`
- Relaxation only applies if all are pure markup extensions
- If any logic file exists, normal requirements apply

### Recovery Features

```python
def add_explored_files(self, files: list[str]) -> dict:
    """
    Add explored files in READY phase

    Use cases:
    - When blocked by check_write_target
    - Want to create files in new directory
    """

def revert_to_exploration(self, keep_results: bool = True) -> dict:
    """
    Return to EXPLORATION phase

    Use cases:
    - Additional exploration needed
    - Want to re-explore with different approach

    keep_results=True: Keep existing exploration results
    keep_results=False: Reset everything
    """
```

---

## QueryFrame

`tools/query_frame.py`

### Overview

QueryFrame structures natural language queries and clarifies "what is known and what is unknown".

### Class Structure

```python
@dataclass
class QueryFrame:
    raw_query: str                           # Original query
    target_feature: str | None = None        # Target feature
    trigger_condition: str | None = None     # Trigger condition
    observed_issue: str | None = None        # Observed problem
    desired_action: str | None = None        # Expected behavior

    # Slot sources (FACT/HYPOTHESIS)
    slot_sources: dict[str, SlotSource] = field(default_factory=dict)

    # Validated slots
    validated_slots: list[str] = field(default_factory=list)

    # NL→Symbol mapping
    mapped_symbols: list[str] = field(default_factory=list)
```

### SlotSource

```python
class SlotSource(Enum):
    FACT = "fact"           # Confirmed from exploration
    HYPOTHESIS = "hypothesis"  # Inferred from semantic search
```

### Slot Validation

```python
def validate_slot(
    slot_name: str,
    value: str,
    quote: str,
    original_query: str
) -> tuple[bool, str | None]:
    """
    Validate slot validity

    Checks:
    1. Does quote exist in original_query
    2. Semantic consistency between value and quote

    Returns:
        (is_valid, error_message)
    """
```

### Risk Assessment

```python
def assess_risk_level(
    intent: str,
    query_frame: QueryFrame
) -> str:
    """
    Assess risk level

    Rules:
    - MODIFY + observed_issue unknown → HIGH
    - IMPLEMENT → MEDIUM
    - INVESTIGATE → LOW
    - All slots filled → LOW
    """
```

### QueryDecomposer

```python
class QueryDecomposer:
    @staticmethod
    def get_extraction_prompt(query: str) -> str:
        """
        Generate slot extraction prompt

        Prompt for LLM with instructions to:
        1. Extract 4 slots from query
        2. Attach quote (citation from original) to each slot
        3. Omit if not applicable
        """

    @staticmethod
    def decompose(query: str, llm_response: dict) -> QueryFrame:
        """
        Build QueryFrame from LLM response

        Processing:
        1. Validate quote for each slot
        2. Add validated slots to validated_slots
        3. Set slot_sources to FACT
        """
```

---

## ChromaDB Manager

`tools/chromadb_manager.py`

### Overview

ChromaDB Manager manages two vector collections: Forest and Map.

### Class Structure

```python
class ChromaDBManager:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.code_intel_dir = self.repo_path / ".code-intel"
        self.chroma_dir = self.code_intel_dir / "chroma"

        # ChromaDB client
        self.client = chromadb.PersistentClient(path=str(self.chroma_dir))

        # Collections
        self.forest = self.client.get_or_create_collection("forest")
        self.map = self.client.get_or_create_collection("map")

        # Configuration
        self.config = self._load_config()
```

### Forest Sync

```python
def sync_forest(self) -> SyncResult:
    """
    Sync source code to Forest collection

    Processing:
    1. Scan files in source_dirs
    2. Detect changes via SHA256 fingerprint
    3. AST chunk only changed files
    4. Embed chunks and upsert to ChromaDB
    5. Update sync_state.json

    Incremental sync:
    - New files → add
    - Changed files → update
    - Deleted files → delete
    """

def needs_sync(self) -> bool:
    """
    Check if sync needed

    Conditions:
    - sync_state.json doesn't exist
    - sync_ttl_hours elapsed since last sync
    - File fingerprints don't match
    """
```

### Map Management

```python
def index_agreements(self) -> int:
    """
    Index agreements to Map collection

    Processing:
    1. Scan .code-intel/agreements/*.md
    2. Embed each file
    3. Add to Map collection

    Returns:
        Number of agreements added
    """

def add_agreement(
    self,
    nl_term: str,
    symbol: str,
    code_evidence: str,
    session_id: str
) -> str:
    """
    Add new agreement

    Processing:
    1. Generate .md file
    2. Add to Map collection

    Returns:
        Generated file path
    """
```

### Search

```python
def search_forest(
    self,
    query: str,
    n_results: int = 10
) -> list[SearchHit]:
    """
    Search Forest collection

    Processing:
    1. Embed query
    2. Similarity search in ChromaDB
    3. Return SearchHit list
    """

def search_map(
    self,
    query: str,
    n_results: int = 5
) -> list[SearchHit]:
    """
    Search Map collection

    Short-circuit:
    - If score >= 0.7 result exists, skip Forest search
    """

def search(
    self,
    query: str,
    n_results: int = 10
) -> SearchResult:
    """
    Unified search

    Processing:
    1. Search Map
    2. High score in Map → short_circuit=True
    3. Search Forest
    4. Merge results
    """
```

### SearchResult

```python
@dataclass
class SearchResult:
    map_hits: list[SearchHit]      # Results from Map
    forest_hits: list[SearchHit]   # Results from Forest
    short_circuit: bool             # Was Map sufficient
    total_chunks: int               # Total chunk count
    query: str                      # Search query
```

---

## AST Chunker

`tools/ast_chunker.py`

### Overview

AST Chunker parses source code and chunks it into meaningful units (functions, classes, etc.).

### Supported Languages

| Language | Extensions | Chunk Units |
|----------|------------|-------------|
| Python | .py | function, class, method |
| PHP | .php | function, class, method |
| JavaScript | .js, .jsx | function, class, arrow_function |
| TypeScript | .ts, .tsx | function, class, interface |
| Blade | .blade.php | component, directive |
| CSS | .css | rule, at_rule |

### Chunk Structure

```python
@dataclass
class CodeChunk:
    file_path: str          # File path
    start_line: int         # Start line
    end_line: int           # End line
    content: str            # Code content
    symbol_name: str        # Symbol name (function name, class name, etc.)
    symbol_type: str        # Symbol type (function, class, etc.)
    language: str           # Language
    fingerprint: str        # SHA256 hash
```

### Chunking Process

```python
def chunk_file(file_path: str) -> list[CodeChunk]:
    """
    Chunk a file

    Processing:
    1. Determine language from extension
    2. Parse with tree-sitter
    3. Extract function/class/method nodes
    4. Convert each node to CodeChunk
    5. Split chunks exceeding max tokens
    """

def chunk_directory(
    directory: str,
    exclude_patterns: list[str]
) -> list[CodeChunk]:
    """
    Chunk all files in directory

    Processing:
    1. Scan files (excluding exclude_patterns)
    2. Process each file with chunk_file
    3. Merge results
    """
```

---

## Embedding

`tools/embedding.py`

### Overview

Embedding converts text to vector representations. Uses multilingual-e5-small model.

### Class Structure

```python
class EmbeddingModel:
    def __init__(self, model_name: str = "multilingual-e5-small"):
        self.model = SentenceTransformer(model_name)
        self.dimension = 384  # multilingual-e5-small dimensions

    def encode(self, texts: list[str]) -> np.ndarray:
        """
        Embed text

        Returns:
            shape: (len(texts), 384)
        """

    def similarity(self, text1: str, text2: str) -> float:
        """
        Calculate similarity between two texts

        Returns:
            Cosine similarity (0.0 - 1.0)
        """
```

### Similarity Judgment

```python
def validate_symbol_relevance(
    target_feature: str,
    symbols: list[str]
) -> dict:
    """
    Verify symbol relevance with Embedding

    3-tier judgment:
    - Similarity > 0.6: Approve as FACT
    - Similarity 0.3-0.6: Approve but raise risk_level to HIGH
    - Similarity < 0.3: Reject, provide re-exploration guidance
    """
```

---

## Tool Implementation Details

### ctags_tool.py

Uses Universal Ctags to search for symbol definitions.

```python
async def find_definitions(
    symbol: str,
    path: str = ".",
    language: str | None = None,
    exact_match: bool = False
) -> dict:
    """
    Search for symbol definitions

    Processing:
    1. Execute ctags (--output-format=json)
    2. Parse results
    3. Filter by symbol

    Returns:
        {
            "symbol": str,
            "definitions": [
                {
                    "name": str,
                    "file": str,
                    "line": int,
                    "kind": str,  # function, class, variable, etc.
                    "scope": str,
                    "signature": str
                }
            ],
            "total": int
        }
    """
```

### ripgrep_tool.py

Uses ripgrep for text search.

```python
async def search_text(
    pattern: str,
    path: str = ".",
    file_type: str | None = None
) -> dict:
    """
    Text search

    Processing:
    1. Execute ripgrep (--json)
    2. Parse results

    Returns:
        {
            "pattern": str,
            "matches": [
                {
                    "file": str,
                    "line": int,
                    "content": str,
                    "context_before": list[str],
                    "context_after": list[str]
                }
            ],
            "total": int
        }
    """

async def find_references(
    symbol: str,
    path: str = "."
) -> dict:
    """
    Search for symbol references (excluding definitions)

    Processing:
    1. Get definition locations with find_definitions
    2. Search for symbol with ripgrep
    3. Exclude definition locations
    """
```

### treesitter_tool.py

Uses tree-sitter for syntax analysis.

```python
async def analyze_structure(path: str) -> dict:
    """
    Analyze file/directory structure

    Returns:
        {
            "path": str,
            "files": [
                {
                    "file": str,
                    "language": str,
                    "symbols": [
                        {
                            "name": str,
                            "type": str,
                            "start_line": int,
                            "end_line": int,
                            "children": [...]  # Nested structure
                        }
                    ]
                }
            ]
        }
    """

async def get_function_at_line(
    file_path: str,
    line: int
) -> dict:
    """
    Get function containing specified line

    Returns:
        {
            "file": str,
            "line": int,
            "function": {
                "name": str,
                "start_line": int,
                "end_line": int,
                "content": str
            }
        }
    """
```

---

## Improvement Cycle

`tools/outcome_log.py`

### Overview

The improvement cycle uses two logs, DecisionLog and OutcomeLog, to collect data needed for system improvement.

### DecisionLog Recording

```python
def record_decision(decision_log: dict) -> dict:
    """
    Record decision (automatic)

    Timing: On query tool execution

    Records:
    - session_id
    - query
    - timestamp
    - intent
    - tools_planned
    - risk_level
    - missing_slots
    """
```

### OutcomeLog Recording

```python
def record_outcome(outcome_log: OutcomeLog) -> dict:
    """
    Record outcome (automatic/manual)

    Timing:
    - Automatic: Failure detection at /code start
    - Manual: /outcome skill

    Records:
    - session_id
    - outcome (success/failure/partial)
    - phase_at_outcome
    - intent
    - semantic_used
    - analysis
    - trigger_message
    """
```

### Analysis Functions

```python
def get_session_analysis(session_id: str) -> dict:
    """
    Combine session decision + outcome

    Returns:
        {
            "session_id": str,
            "decision": dict | None,
            "outcomes": list[dict],
            "analysis": {
                "had_decision": bool,
                "had_outcome": bool,
                "final_outcome": str | None,
                "tools_planned": list[str],
                "failure_point": str | None
            }
        }
    """

def get_improvement_insights(limit: int = 100) -> dict:
    """
    Analyze failure patterns

    Returns:
        {
            "total_sessions_with_outcomes": int,
            "sessions_with_decisions": int,
            "tool_failure_correlation": dict,
            "risk_level_correlation": dict,
            "common_failure_points": dict
        }
    """
```

---

## Overall Data Flow Diagram

```
[User Request]
        │
        ▼
[/code skill start]
        │
        ├─→ [Step 0: Failure Check]
        │        │
        │        └─→ [record_outcome (failure)]
        │
        ├─→ [Step 1: Intent Determination]
        │
        ├─→ [Step 2: start_session]
        │        │
        │        └─→ [SessionState creation]
        │
        ├─→ [Step 3: set_query_frame]
        │        │
        │        └─→ [QueryFrame setup]
        │
        ├─→ [Step 4: EXPLORATION]
        │        │
        │        ├─→ [find_definitions] → [ctags]
        │        ├─→ [find_references] → [ripgrep]
        │        └─→ [submit_understanding]
        │                  │
        │                  └─→ [evaluate_exploration]
        │                           │
        │                           ├─→ high → [READY]
        │                           └─→ low → [SEMANTIC]
        │
        ├─→ [Step 5: SEMANTIC]
        │        │
        │        ├─→ [semantic_search] → [ChromaDB]
        │        └─→ [submit_semantic]
        │
        ├─→ [Step 6: VERIFICATION]
        │        │
        │        └─→ [submit_verification]
        │
        └─→ [Step 7: READY]
                 │
                 ├─→ [check_write_target]
                 │        │
                 │        ├─→ allowed → [Edit/Write]
                 │        └─→ blocked → [add_explored_files] or [revert_to_exploration]
                 │
                 └─→ [Implementation complete]
```

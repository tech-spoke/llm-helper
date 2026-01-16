"""
Impact Analyzer for Code Intelligence MCP Server v1.1.

Analyzes the impact of code changes by detecting:
1. Static references (direct callers, type hints)
2. Naming convention matches (tests, factories, seeders)
3. Markup relaxation for style-only files
4. Document mentions (keyword search in docs) - v1.1.1

Design principles:
- Direct references only (1 level) - LLM handles deeper investigation
- Generic naming conventions - framework-specific patterns handled by LLM via project_rules
- Markup relaxation for .html, .css, .md (NOT for .blade.php, .vue, .jsx, .tsx)
- Document keyword search to catch related documentation that needs updating
"""

import asyncio
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import yaml

from tools.ctags_tool import find_references, find_definitions


# File extensions for markup relaxation
RELAXED_MARKUP_EXTENSIONS = {".html", ".htm", ".css", ".scss", ".md", ".markdown"}

# File extensions that look like markup but contain logic (NOT relaxed)
LOGIC_MARKUP_EXTENSIONS = {".blade.php", ".vue", ".jsx", ".tsx"}

# Document file patterns for keyword search (default, can be overridden in context.yml)
DEFAULT_DOCUMENT_PATTERNS = ["**/*.md", "**/README*", "**/docs/**/*"]

# Default exclude patterns for document search (can be overridden in context.yml)
DEFAULT_DOCUMENT_EXCLUDE_PATTERNS = [
    "node_modules/**",
    "vendor/**",
    ".git/**",
    ".venv/**",
    "__pycache__/**",
]

# Extensions to search for document mentions
DOCUMENT_EXTENSIONS = {".md", ".markdown", ".rst", ".txt"}

# Limits for document search results
MAX_MENTIONS_PER_FILE = 3      # Max mentions per file (prevents CHANGELOG domination)
MAX_TOTAL_FILES = 20           # Max files in results
MAX_KEYWORDS = 10              # Max keywords to search


@dataclass
class StaticReference:
    """A static reference found in the codebase."""
    file: str
    line: int
    context: str = ""
    ref_type: str = "caller"  # caller, type_hint, import


@dataclass
class NamingConventionMatches:
    """Matches found by naming convention."""
    tests: list[str] = field(default_factory=list)
    factories: list[str] = field(default_factory=list)
    seeders: list[str] = field(default_factory=list)


@dataclass
class DocumentMention:
    """A mention of a keyword found in documentation."""
    file: str
    line: int
    content: str
    keyword: str


@dataclass
class ImpactAnalysisResult:
    """Result of impact analysis."""
    mode: str  # "standard" or "relaxed_markup"
    depth: str = "direct_only"
    reason: str = ""
    static_references: dict = field(default_factory=dict)
    naming_convention_matches: dict = field(default_factory=dict)
    document_mentions: dict = field(default_factory=dict)  # v1.1.1: keyword search in docs
    inference_hint: str | None = None
    confirmation_required: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "impact_analysis": {
                "mode": self.mode,
                "depth": self.depth,
                "static_references": self.static_references,
                "naming_convention_matches": self.naming_convention_matches,
            }
        }

        # v1.1.1: Include document mentions if any
        if self.document_mentions:
            result["impact_analysis"]["document_mentions"] = self.document_mentions

        if self.reason:
            result["impact_analysis"]["reason"] = self.reason

        if self.inference_hint:
            result["impact_analysis"]["inference_hint"] = self.inference_hint

        result["confirmation_required"] = self.confirmation_required

        return result


class ImpactAnalyzer:
    """
    Analyzes impact of code changes.

    Main responsibilities:
    1. Detect direct references to target files/symbols
    2. Find related files by naming convention
    3. Apply markup relaxation when appropriate
    4. Generate confirmation requirements for LLM
    5. Search for keyword mentions in documentation (v1.1.1)
    """

    def __init__(self, repo_path: str = "."):
        self.repo_path = Path(repo_path).resolve()
        self._document_config = self._load_document_config()

    def _load_document_config(self) -> dict:
        """
        Load document_search configuration from context.yml.

        Returns empty dict if context.yml doesn't exist or has no document_search section.
        In that case, default patterns will be used.
        """
        context_file = self.repo_path / ".code-intel" / "context.yml"
        if not context_file.exists():
            return {}

        try:
            with open(context_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config.get("document_search", {}) if config else {}
        except Exception:
            return {}

    async def analyze(
        self,
        target_files: list[str],
        change_description: str = "",
    ) -> ImpactAnalysisResult:
        """
        Analyze impact of changes to target files.

        Args:
            target_files: List of file paths to analyze
            change_description: Description of the change (for inference hints)

        Returns:
            ImpactAnalysisResult with references, matches, and confirmation requirements
        """
        # Check if markup relaxation applies
        if self._should_relax_markup(target_files):
            return self._create_relaxed_result()

        # Analyze each target file
        all_callers = []
        all_type_hints = []
        all_naming_matches = NamingConventionMatches()

        for target_file in target_files:
            # Extract base name and symbols
            base_name = self._extract_base_name(target_file)

            if base_name:
                # Find references to symbols in this file
                refs = await self._find_static_references(target_file, base_name)
                all_callers.extend(refs.get("callers", []))
                all_type_hints.extend(refs.get("type_hints", []))

                # Find naming convention matches
                matches = await self._find_naming_convention_matches(base_name)
                all_naming_matches.tests.extend(matches.tests)
                all_naming_matches.factories.extend(matches.factories)
                all_naming_matches.seeders.extend(matches.seeders)

        # Deduplicate
        all_callers = self._deduplicate_refs(all_callers)
        all_type_hints = self._deduplicate_refs(all_type_hints)
        all_naming_matches.tests = list(set(all_naming_matches.tests))
        all_naming_matches.factories = list(set(all_naming_matches.factories))
        all_naming_matches.seeders = list(set(all_naming_matches.seeders))

        # v1.1.1: Search for keywords in documentation
        keywords = self._extract_keywords(change_description, target_files)
        doc_mentions = await self._find_document_mentions(keywords, target_files)

        # Build result
        static_refs = {}
        if all_callers:
            static_refs["callers"] = [
                {"file": r["file"], "line": r["line"], "context": r.get("context", "")}
                for r in all_callers
            ]
        if all_type_hints:
            static_refs["type_hints"] = [
                {"file": r["file"], "line": r["line"]}
                for r in all_type_hints
            ]

        naming_matches = {}
        if all_naming_matches.tests:
            naming_matches["tests"] = all_naming_matches.tests
        if all_naming_matches.factories:
            naming_matches["factories"] = all_naming_matches.factories
        if all_naming_matches.seeders:
            naming_matches["seeders"] = all_naming_matches.seeders

        # Build confirmation requirements
        must_verify = list(set(r["file"] for r in all_callers))
        should_verify = list(set(
            all_naming_matches.tests +
            all_naming_matches.factories +
            all_naming_matches.seeders
        ))

        # v1.1.1: Add document files to should_verify
        doc_files = list(set(m["file"] for m in doc_mentions))
        should_verify.extend(doc_files)

        # Remove target files from verification lists
        target_set = set(str(Path(f).resolve()) for f in target_files)
        must_verify = [f for f in must_verify if str(Path(f).resolve()) not in target_set]
        should_verify = [f for f in should_verify if str(Path(f).resolve()) not in target_set]

        confirmation = {
            "must_verify": must_verify,
            "should_verify": should_verify,
            "llm_should_infer": [
                "project_rules の命名規則に従い、対応する Resource/Policy を確認"
            ] if must_verify or should_verify else [],
            "indirect_note": (
                "間接参照（2段階以上）が必要な場合は find_references で追加調査してください"
            ),
            "schema": {
                "verified_files": [
                    {
                        "file": "string",
                        "status": "will_modify | no_change_needed | not_affected",
                        "reason": "string (status != will_modify 時は必須)",
                    }
                ]
            },
        }

        # v1.1.1: Build document mentions dict
        doc_mentions_dict = {}
        if doc_mentions:
            doc_mentions_dict["files"] = doc_mentions
            doc_mentions_dict["keywords_searched"] = keywords

        return ImpactAnalysisResult(
            mode="standard",
            depth="direct_only",
            static_references=static_refs,
            naming_convention_matches=naming_matches,
            document_mentions=doc_mentions_dict,
            inference_hint=(
                "project_rules に基づき、関連する Resource や Policy も確認してください"
                if must_verify or should_verify else None
            ),
            confirmation_required=confirmation,
        )

    def _should_relax_markup(self, target_files: list[str]) -> bool:
        """
        Check if all target files qualify for markup relaxation.

        Returns True if ALL files are pure markup (no logic).
        """
        if not target_files:
            return False

        for file_path in target_files:
            path = Path(file_path)
            suffix = path.suffix.lower()

            # Check for multi-part extensions like .blade.php
            full_suffix = "".join(path.suffixes).lower()

            # Logic-containing markup files are NOT relaxed
            if full_suffix in LOGIC_MARKUP_EXTENSIONS or suffix in LOGIC_MARKUP_EXTENSIONS:
                return False

            # Only relax pure markup files
            if suffix not in RELAXED_MARKUP_EXTENSIONS:
                return False

        return True

    def _create_relaxed_result(self) -> ImpactAnalysisResult:
        """Create a relaxed markup result."""
        return ImpactAnalysisResult(
            mode="relaxed_markup",
            depth="direct_only",
            reason="対象ファイルがマークアップのみのため緩和モード適用",
            static_references={},
            naming_convention_matches={},
            inference_hint=None,
            confirmation_required={
                "must_verify": [],
                "should_verify": [],
                "llm_should_infer": [],
                "schema": {
                    "verified_files": [
                        {
                            "file": "string",
                            "status": "will_modify | no_change_needed | not_affected",
                            "reason": "string (status != will_modify 時は必須)",
                        }
                    ]
                },
            },
        )

    def _extract_base_name(self, file_path: str) -> str:
        """
        Extract base name from file path for naming convention matching.

        Examples:
            app/Models/Product.php -> Product
            src/components/UserProfile.tsx -> UserProfile
            services/cart_service.py -> CartService (normalized)
        """
        path = Path(file_path)
        stem = path.stem

        # Handle multi-part extensions
        while stem and "." in stem:
            stem = Path(stem).stem

        # Convert snake_case to PascalCase for consistency
        if "_" in stem:
            stem = "".join(word.capitalize() for word in stem.split("_"))

        return stem

    async def _find_static_references(
        self,
        target_file: str,
        base_name: str,
    ) -> dict:
        """
        Find static references to the target file/symbol.

        Uses find_references to locate callers and type hints.
        """
        callers = []
        type_hints = []

        try:
            # Search for the base name (class/function name)
            refs_result = await find_references(
                symbol=base_name,
                path=str(self.repo_path),
            )

            if "references" in refs_result:
                for ref in refs_result["references"]:
                    ref_info = {
                        "file": ref.get("file", ""),
                        "line": ref.get("line", 0),
                        "context": ref.get("content", "")[:100],
                    }

                    # Basic heuristic to identify type hints
                    content = ref.get("content", "")
                    if self._looks_like_type_hint(content, base_name):
                        type_hints.append(ref_info)
                    else:
                        callers.append(ref_info)

        except Exception:
            pass

        return {"callers": callers, "type_hints": type_hints}

    def _looks_like_type_hint(self, content: str, symbol: str) -> bool:
        """Check if a reference looks like a type hint."""
        # Common type hint patterns
        type_hint_patterns = [
            rf":\s*{re.escape(symbol)}[\s,\)\]]",  # : Symbol or : Symbol,
            rf"->\s*{re.escape(symbol)}",  # -> Symbol
            rf"<{re.escape(symbol)}>",  # Generic<Symbol>
            rf"\[{re.escape(symbol)}\]",  # List[Symbol]
            rf"@param\s+{re.escape(symbol)}",  # PHPDoc @param
            rf"@return\s+{re.escape(symbol)}",  # PHPDoc @return
            rf"@var\s+{re.escape(symbol)}",  # PHPDoc @var
        ]

        for pattern in type_hint_patterns:
            if re.search(pattern, content):
                return True

        return False

    async def _find_naming_convention_matches(
        self,
        base_name: str,
    ) -> NamingConventionMatches:
        """
        Find files that match naming conventions based on the base name.

        Generic patterns only - framework-specific patterns are left to LLM inference.
        """
        matches = NamingConventionMatches()

        # Search for test files
        test_patterns = [
            f"**/*{base_name}*Test.*",
            f"**/*{base_name}*test.*",
            f"**/test_*{base_name}*.*",
            f"**/tests/**/*{base_name}*.*",
        ]

        # Search for factory files
        factory_patterns = [
            f"**/*{base_name}Factory.*",
            f"**/*{base_name}*factory.*",
            f"**/factories/*{base_name}*.*",
        ]

        # Search for seeder files
        seeder_patterns = [
            f"**/*{base_name}Seeder.*",
            f"**/*{base_name}*seeder.*",
            f"**/seeders/*{base_name}*.*",
        ]

        # Execute glob searches
        for pattern in test_patterns:
            matches.tests.extend(self._glob_files(pattern))

        for pattern in factory_patterns:
            matches.factories.extend(self._glob_files(pattern))

        for pattern in seeder_patterns:
            matches.seeders.extend(self._glob_files(pattern))

        return matches

    def _glob_files(self, pattern: str) -> list[str]:
        """Glob for files matching pattern."""
        try:
            return [str(p) for p in self.repo_path.glob(pattern) if p.is_file()]
        except Exception:
            return []

    def _deduplicate_refs(self, refs: list[dict]) -> list[dict]:
        """Deduplicate references by file and line."""
        seen = set()
        unique = []

        for ref in refs:
            key = (ref.get("file", ""), ref.get("line", 0))
            if key not in seen:
                seen.add(key)
                unique.append(ref)

        return unique

    def _extract_keywords(
        self,
        change_description: str,
        target_files: list[str],
    ) -> list[str]:
        """
        Extract keywords from change description and target files.

        v1.1.1: Keywords are used to search for mentions in documentation.

        Priority levels (processed in order, limited by MAX_KEYWORDS):
        - High: Quoted strings (explicit, user-specified)
        - Medium: CamelCase/snake_case technical terms
        - Low: File base names (often too generic)

        Args:
            change_description: Description of the change
            target_files: List of target file paths

        Returns:
            List of keywords to search for (prioritized, limited)
        """
        # Use ordered list to maintain priority
        high_priority = []    # Quoted strings
        medium_priority = []  # Technical terms (CamelCase, snake_case)
        low_priority = []     # File base names

        # HIGH: Extract quoted strings from change_description
        quoted_patterns = [
            r'"([^"]+)"',  # "double quoted"
            r"'([^']+)'",  # 'single quoted'
            r'`([^`]+)`',  # `backtick`
        ]
        for pattern in quoted_patterns:
            for match in re.findall(pattern, change_description):
                if len(match) >= 2 and match not in high_priority:
                    high_priority.append(match)

        # MEDIUM: Extract technical terms (CamelCase, snake_case, specific patterns)
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "this", "that", "these", "those", "it", "its",
            "and", "or", "but", "if", "then", "else",
            "add", "remove", "change", "update", "modify", "fix", "delete",
            "file", "files", "code", "field", "type", "value", "name",
            "を", "に", "は", "が", "の", "で", "と", "へ", "から", "まで",
            "する", "した", "します", "される", "された",
            "追加", "削除", "変更", "修正", "更新", "フィールド", "型",
        }

        # CamelCase pattern (e.g., ProductPrice, UserAccount)
        camel_case = re.findall(r'[A-Z][a-z]+(?:[A-Z][a-z]+)+', change_description)
        for term in camel_case:
            if term not in medium_priority:
                medium_priority.append(term)

        # snake_case pattern (e.g., product_price, user_account)
        snake_case = re.findall(r'[a-z]+(?:_[a-z]+)+', change_description)
        for term in snake_case:
            if term not in medium_priority:
                medium_priority.append(term)

        # Other significant words (4+ chars, not stop words)
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*|[ぁ-んァ-ン一-龥]+', change_description)
        for word in words:
            if (len(word) >= 4 and
                word.lower() not in stop_words and
                word not in medium_priority and
                word not in high_priority):
                medium_priority.append(word)

        # LOW: Extract base names from target files (often too generic)
        for target_file in target_files:
            base_name = self._extract_base_name(target_file)
            if (base_name and
                len(base_name) >= 4 and
                base_name not in low_priority and
                base_name not in medium_priority and
                base_name not in high_priority):
                low_priority.append(base_name)

        # Combine with priority order, respecting limit
        all_keywords = high_priority + medium_priority + low_priority
        return all_keywords[:MAX_KEYWORDS]

    def _matches_exclude_pattern(self, file_path: str, exclude_patterns: list[str]) -> bool:
        """Check if a file path matches any exclude pattern."""
        # Get relative path from repo root
        try:
            rel_path = str(Path(file_path).relative_to(self.repo_path))
        except ValueError:
            rel_path = file_path

        for pattern in exclude_patterns:
            if fnmatch(rel_path, pattern):
                return True
            # Also check just the filename for simple patterns
            if fnmatch(Path(file_path).name, pattern):
                return True
        return False

    async def _find_document_mentions(
        self,
        keywords: list[str],
        target_files: list[str],
    ) -> list[dict]:
        """
        Search for keyword mentions in documentation files.

        v1.1.1: Finds related documentation that might need updating.
        Returns file-level aggregation to avoid overwhelming LLM with line-by-line details.

        Configuration from context.yml document_search section:
        - include_patterns: Glob patterns for files to search (default: ["**/*.md", ...])
        - exclude_patterns: Glob patterns for files to exclude (default: ["node_modules/**", ...])

        Args:
            keywords: Keywords to search for
            target_files: Target files (excluded from results)

        Returns:
            List of document mentions aggregated by file:
            [{"file": "...", "match_count": N, "keywords": [...], "sample_lines": [...]}]
        """
        if not keywords:
            return []

        target_set = set(str(Path(f).resolve()) for f in target_files)

        # Get patterns from config or use defaults
        include_patterns = self._document_config.get(
            "include_patterns", DEFAULT_DOCUMENT_PATTERNS
        )
        exclude_patterns = self._document_config.get(
            "exclude_patterns", DEFAULT_DOCUMENT_EXCLUDE_PATTERNS
        )

        # Find all document files
        doc_files = []
        for pattern in include_patterns:
            doc_files.extend(self._glob_files(pattern))

        # Filter to document extensions only
        doc_files = [
            f for f in doc_files
            if Path(f).suffix.lower() in DOCUMENT_EXTENSIONS
        ]

        # Apply exclude patterns
        doc_files = [
            f for f in doc_files
            if not self._matches_exclude_pattern(f, exclude_patterns)
        ]

        # Deduplicate and exclude target files
        doc_files = list(set(
            f for f in doc_files
            if str(Path(f).resolve()) not in target_set
        ))

        # Search each document for keywords, aggregate by file
        file_results: dict[str, dict] = {}

        for doc_file in doc_files:
            try:
                path = Path(doc_file)
                if not path.exists() or not path.is_file():
                    continue

                content = path.read_text(encoding="utf-8", errors="ignore")
                lines = content.split("\n")

                file_matches = {
                    "keywords_found": set(),
                    "sample_lines": [],
                    "match_count": 0,
                }

                for keyword in keywords:
                    pattern = re.compile(re.escape(keyword), re.IGNORECASE)

                    for line_num, line in enumerate(lines, start=1):
                        if pattern.search(line):
                            file_matches["match_count"] += 1
                            file_matches["keywords_found"].add(keyword)

                            # Keep sample lines (up to MAX_MENTIONS_PER_FILE)
                            if len(file_matches["sample_lines"]) < MAX_MENTIONS_PER_FILE:
                                file_matches["sample_lines"].append({
                                    "line": line_num,
                                    "content": line.strip()[:80],
                                    "keyword": keyword,
                                })

                # Only include files with matches
                if file_matches["match_count"] > 0:
                    file_results[str(path)] = file_matches

            except Exception:
                continue

        # Sort by match count (most relevant first) and limit
        sorted_files = sorted(
            file_results.items(),
            key=lambda x: x[1]["match_count"],
            reverse=True,
        )[:MAX_TOTAL_FILES]

        # Convert to output format
        return [
            {
                "file": file_path,
                "match_count": data["match_count"],
                "keywords": list(data["keywords_found"]),
                "sample_lines": data["sample_lines"],
            }
            for file_path, data in sorted_files
        ]


async def analyze_impact(
    target_files: list[str],
    change_description: str = "",
    repo_path: str = ".",
) -> dict:
    """
    Analyze impact of changes to target files.

    This is the main entry point for the analyze_impact MCP tool.

    Args:
        target_files: List of file paths to analyze
        change_description: Description of the change
        repo_path: Project root path

    Returns:
        Dictionary with impact analysis results
    """
    analyzer = ImpactAnalyzer(repo_path)
    result = await analyzer.analyze(target_files, change_description)
    return result.to_dict()

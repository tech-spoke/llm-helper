"""Universal Ctags wrapper for symbol definition and reference analysis."""

import asyncio
import json
import tempfile
from pathlib import Path


# Common directories to exclude from ctags scanning
CTAGS_EXCLUDE_PATTERNS = [
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".git",
    ".svn",
    "dist",
    "build",
    ".tox",
    ".eggs",
    "*.egg-info",
]


def _build_ctags_exclude_args() -> list[str]:
    """Build ctags exclude arguments."""
    args = []
    for pattern in CTAGS_EXCLUDE_PATTERNS:
        args.append(f"--exclude={pattern}")
    return args


async def _scan_file_with_cache(
    file_path: Path,
    language: str | None,
    cache_manager: "CtagsCacheManager | None",
) -> list[dict]:
    """
    Scan a single file with caching support.

    Args:
        file_path: File to scan
        language: Optional language filter
        cache_manager: Optional cache manager for persistent caching

    Returns:
        List of tag dictionaries
    """
    # Try cache first
    if cache_manager:
        cached_tags = cache_manager.get_cached_tags(file_path, language)
        if cached_tags is not None:
            return cached_tags

    # Cache miss - run ctags on single file
    cmd = [
        "ctags",
        "--output-format=json",
        "--fields=+n+S+K",
    ]

    if language:
        cmd.extend(["--languages", language])

    try:
        # Create temporary file for output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            temp_path = f.name

        cmd_with_output = cmd + ["-f", temp_path, str(file_path)]

        process = await asyncio.create_subprocess_exec(
            *cmd_with_output,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

        # Parse results
        tags = []
        with open(temp_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        tags.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        Path(temp_path).unlink(missing_ok=True)

        # Cache results
        if cache_manager:
            cache_manager.cache_tags(file_path, tags, language)

        return tags

    except Exception:
        # On error, return empty list
        return []


async def find_definitions(
    symbol: str,
    path: str = ".",
    language: str | None = None,
    exact_match: bool = False,
    session: "SessionState | None" = None,
    use_persistent_cache: bool = True,
) -> dict:
    """
    Find symbol definitions using Universal Ctags with file-level caching.

    Args:
        symbol: Symbol name to search for
        path: Path to search in (file or directory)
        language: Filter by language (e.g., "Python", "JavaScript")
        exact_match: Whether to match symbol name exactly
        session: Optional SessionState for caching
        use_persistent_cache: Whether to use persistent file-level cache

    Returns:
        Dictionary with definition locations
    """
    search_path = Path(path).resolve()

    # Check session cache first (Phase 1)
    if session:
        cache_key = (symbol, str(search_path), language, exact_match)
        if cache_key in session.definitions_cache:
            session.cache_stats["hits"] += 1
            cached_result = session.definitions_cache[cache_key].copy()
            cached_result["cache_hit"] = True
            return cached_result
        session.cache_stats["misses"] += 1

    if not search_path.exists():
        return {"error": f"Path does not exist: {path}"}

    # Get persistent cache manager (Phase 2)
    cache_manager = None
    if use_persistent_cache:
        try:
            # Import locally to avoid circular dependency
            import sys
            import os
            server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if server_dir not in sys.path:
                sys.path.insert(0, server_dir)
            from code_intel_server import get_ctags_cache_manager
            cache_manager = get_ctags_cache_manager(
                search_path if search_path.is_dir() else search_path.parent
            )
        except Exception:
            # Fall back to no persistent cache on error
            cache_manager = None

    # Collect all tags from files
    all_tags = []
    files_scanned = 0
    files_cached = 0

    try:
        if search_path.is_file():
            # Single file - use file-level cache
            tags = await _scan_file_with_cache(search_path, language, cache_manager)
            all_tags.extend(tags)
            files_scanned = 1
            files_cached = 1 if cache_manager and cache_manager.get_cached_tags(search_path, language) is not None else 0
        else:
            # Directory - scan all files with caching
            # Common source file extensions
            extensions = [".py", ".js", ".ts", ".jsx", ".tsx", ".php", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb"]

            for ext in extensions:
                for file_path in search_path.rglob(f"*{ext}"):
                    if not file_path.is_file():
                        continue

                    # Check exclusion patterns
                    skip = False
                    for excl in CTAGS_EXCLUDE_PATTERNS:
                        if excl in str(file_path):
                            skip = True
                            break
                    if skip:
                        continue

                    # Check if cached before scan
                    was_cached = cache_manager and cache_manager.get_cached_tags(file_path, language) is not None

                    tags = await _scan_file_with_cache(file_path, language, cache_manager)
                    all_tags.extend(tags)
                    files_scanned += 1
                    if was_cached:
                        files_cached += 1

        # Filter by symbol name
        definitions = []
        for tag in all_tags:
            tag_name = tag.get("name", "")

            if exact_match:
                if tag_name != symbol:
                    continue
            else:
                if symbol.lower() not in tag_name.lower():
                    continue

            definitions.append({
                "name": tag_name,
                "file": tag.get("path", ""),
                "line": tag.get("line", 0),
                "kind": tag.get("kind", ""),
                "scope": tag.get("scope", ""),
                "signature": tag.get("signature", ""),
                "language": tag.get("language", ""),
            })

        result = {
            "symbol": symbol,
            "path": str(search_path),
            "definitions": definitions,
            "total": len(definitions),
            "cache_hit": False,
            "cache_stats": {
                "files_scanned": files_scanned,
                "files_cached": files_cached,
                "persistent_cache_enabled": use_persistent_cache and cache_manager is not None,
            }
        }

        # Cache result in session
        if session:
            cache_key = (symbol, str(search_path), language, exact_match)
            session.definitions_cache[cache_key] = result

        return result

    except FileNotFoundError:
        return {
            "error": "Universal Ctags not found. Install with: apt install universal-ctags"
        }
    except Exception as e:
        return {"error": f"Failed to find definitions: {str(e)}"}


async def find_references(
    symbol: str,
    path: str = ".",
    language: str | None = None,
    session: "SessionState | None" = None,
) -> dict:
    """
    Find symbol references using ripgrep (ctags doesn't track references).

    This uses ripgrep to find occurrences of the symbol, then filters
    out the definitions to get references.

    Args:
        symbol: Symbol name to search for
        path: Path to search in
        language: File type filter (e.g., "py", "js")

    Returns:
        Dictionary with reference locations
    """
    search_path = Path(path).resolve()

    if not search_path.exists():
        return {"error": f"Path does not exist: {path}"}

    # Use ripgrep to find all occurrences
    cmd = [
        "rg",
        "--json",
        "-w",  # Word boundary matching
        symbol,
        str(search_path),
    ]

    if language:
        cmd.extend(["-t", language])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        # Get definitions to filter them out
        definitions_result = await find_definitions(symbol, path, language, exact_match=True, session=session)
        definition_locations = set()
        if "definitions" in definitions_result:
            for d in definitions_result["definitions"]:
                definition_locations.add((d["file"], d["line"]))

        # Parse ripgrep output
        references = []
        for line in stdout.decode().strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "match":
                    match_data = data["data"]
                    file_path = match_data["path"]["text"]
                    line_num = match_data["line_number"]

                    # Skip if this is a definition
                    if (file_path, line_num) in definition_locations:
                        continue

                    references.append({
                        "file": file_path,
                        "line": line_num,
                        "content": match_data["lines"]["text"].strip(),
                    })
            except json.JSONDecodeError:
                continue

        return {
            "symbol": symbol,
            "path": str(search_path),
            "references": references,
            "total": len(references),
        }

    except FileNotFoundError:
        return {"error": "ripgrep (rg) not found"}
    except Exception as e:
        return {"error": f"Failed to find references: {str(e)}"}


async def get_symbols(
    path: str,
    kind: str | None = None,
    language: str | None = None,
) -> dict:
    """
    Get all symbols in a file or directory.

    Args:
        path: Path to analyze
        kind: Filter by symbol kind (e.g., "function", "class", "variable")
        language: Filter by language

    Returns:
        Dictionary with all symbols
    """
    search_path = Path(path).resolve()

    if not search_path.exists():
        return {"error": f"Path does not exist: {path}"}

    cmd = [
        "ctags",
        "--output-format=json",
        "--fields=+n+S+K",
        "-R",
    ]

    # Add exclude patterns
    cmd.extend(_build_ctags_exclude_args())

    if language:
        cmd.extend(["--languages", language])

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            temp_path = f.name

        # -f must come before the path argument
        cmd_with_output = cmd + ["-f", temp_path, str(search_path)]

        process = await asyncio.create_subprocess_exec(
            *cmd_with_output,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

        symbols = []
        with open(temp_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tag = json.loads(line)

                    # Filter by kind if specified
                    if kind and tag.get("kind", "").lower() != kind.lower():
                        continue

                    symbols.append({
                        "name": tag.get("name", ""),
                        "file": tag.get("path", ""),
                        "line": tag.get("line", 0),
                        "kind": tag.get("kind", ""),
                        "scope": tag.get("scope", ""),
                        "language": tag.get("language", ""),
                    })
                except json.JSONDecodeError:
                    continue

        Path(temp_path).unlink(missing_ok=True)

        return {
            "path": str(search_path),
            "symbols": symbols,
            "total": len(symbols),
        }

    except FileNotFoundError:
        return {"error": "Universal Ctags not found"}
    except Exception as e:
        return {"error": f"Failed to get symbols: {str(e)}"}

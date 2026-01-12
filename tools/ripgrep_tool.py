"""Ripgrep wrapper for fast text search."""

import asyncio
import json
from pathlib import Path


async def search_text(
    pattern: str,
    path: str = ".",
    file_type: str | None = None,
    case_sensitive: bool = True,
    context_lines: int = 0,
    max_results: int = 100,
    regex: bool = True,
) -> dict:
    """
    Search for text patterns using ripgrep.

    Args:
        pattern: Search pattern (regex by default)
        path: Path to search in
        file_type: File type filter (e.g., "py", "js", "ts")
        case_sensitive: Whether search is case sensitive
        context_lines: Number of context lines before/after match
        max_results: Maximum number of results to return
        regex: Whether pattern is a regex (False for literal)

    Returns:
        Dictionary with search results
    """
    search_path = Path(path).resolve()

    if not search_path.exists():
        return {"error": f"Path does not exist: {path}"}

    # Build ripgrep command
    cmd = ["rg", "--json"]

    # Case sensitivity
    if not case_sensitive:
        cmd.append("-i")

    # Literal search (not regex)
    if not regex:
        cmd.append("-F")

    # File type filter
    if file_type:
        cmd.extend(["-t", file_type])

    # Context lines
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])

    # Max count
    cmd.extend(["-m", str(max_results)])

    # Pattern and path
    cmd.append(pattern)
    cmd.append(str(search_path))

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        # Parse JSON output
        results = []
        for line in stdout.decode().strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "match":
                    match_data = data["data"]
                    results.append({
                        "file": match_data["path"]["text"],
                        "line_number": match_data["line_number"],
                        "line_content": match_data["lines"]["text"].rstrip(),
                        "submatches": [
                            {
                                "match": sm["match"]["text"],
                                "start": sm["start"],
                                "end": sm["end"],
                            }
                            for sm in match_data.get("submatches", [])
                        ],
                    })
            except json.JSONDecodeError:
                continue

        return {
            "pattern": pattern,
            "path": str(search_path),
            "matches": results,
            "total_matches": len(results),
        }

    except FileNotFoundError:
        return {
            "error": "ripgrep (rg) not found. Install with: apt install ripgrep"
        }
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}


async def search_files(
    pattern: str,
    path: str = ".",
    file_type: str | None = None,
) -> dict:
    """
    Search for files matching a glob pattern.

    Args:
        pattern: Glob pattern for file names
        path: Path to search in
        file_type: File type filter

    Returns:
        Dictionary with matching file paths
    """
    search_path = Path(path).resolve()

    if not search_path.exists():
        return {"error": f"Path does not exist: {path}"}

    cmd = ["rg", "--files", "-g", pattern]

    if file_type:
        cmd.extend(["-t", file_type])

    cmd.append(str(search_path))

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        files = [f for f in stdout.decode().strip().split("\n") if f]

        return {
            "pattern": pattern,
            "path": str(search_path),
            "files": files,
            "total_files": len(files),
        }

    except FileNotFoundError:
        return {"error": "ripgrep (rg) not found"}
    except Exception as e:
        return {"error": f"File search failed: {str(e)}"}

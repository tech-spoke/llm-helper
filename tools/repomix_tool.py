"""Repomix wrapper for packing repositories into LLM-friendly format."""

import asyncio
import subprocess
import tempfile
from pathlib import Path


async def pack_repository(
    path: str,
    output_format: str = "markdown",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> dict:
    """
    Pack a repository using Repomix for LLM consumption.

    Args:
        path: Path to the repository
        output_format: Output format (markdown, xml, plain)
        include_patterns: Glob patterns to include
        exclude_patterns: Glob patterns to exclude

    Returns:
        Dictionary with packed content and metadata
    """
    repo_path = Path(path).resolve()

    if not repo_path.exists():
        return {"error": f"Path does not exist: {path}"}

    # Build repomix command
    cmd = ["repomix", str(repo_path)]

    # Output format
    if output_format in ("markdown", "xml", "plain"):
        cmd.extend(["--style", output_format])

    # Include patterns
    if include_patterns:
        for pattern in include_patterns:
            cmd.extend(["--include", pattern])

    # Exclude patterns
    if exclude_patterns:
        for pattern in exclude_patterns:
            cmd.extend(["--ignore", pattern])

    # Output to stdout
    cmd.append("--stdout")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            return {
                "error": f"Repomix failed: {stderr.decode()}",
                "returncode": process.returncode,
            }

        content = stdout.decode("utf-8", errors="replace")

        return {
            "content": content,
            "path": str(repo_path),
            "format": output_format,
            "size_bytes": len(content.encode()),
        }

    except FileNotFoundError:
        return {
            "error": "Repomix not found. Install with: npm install -g repomix"
        }
    except Exception as e:
        return {"error": f"Failed to pack repository: {str(e)}"}


def pack_repository_sync(
    path: str,
    output_format: str = "markdown",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> dict:
    """Synchronous version of pack_repository."""
    return asyncio.run(
        pack_repository(path, output_format, include_patterns, exclude_patterns)
    )

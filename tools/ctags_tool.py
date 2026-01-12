"""Universal Ctags wrapper for symbol definition and reference analysis."""

import asyncio
import json
import tempfile
from pathlib import Path


async def find_definitions(
    symbol: str,
    path: str = ".",
    language: str | None = None,
    exact_match: bool = False,
) -> dict:
    """
    Find symbol definitions using Universal Ctags.

    Args:
        symbol: Symbol name to search for
        path: Path to search in (file or directory)
        language: Filter by language (e.g., "Python", "JavaScript")
        exact_match: Whether to match symbol name exactly

    Returns:
        Dictionary with definition locations
    """
    search_path = Path(path).resolve()

    if not search_path.exists():
        return {"error": f"Path does not exist: {path}"}

    # Build ctags command
    # --output-format=json provides structured output
    cmd = [
        "ctags",
        "--output-format=json",
        "--fields=+n+S+K",  # Include line number, signature, kind
        "-R",  # Recursive
    ]

    if language:
        cmd.extend(["--languages", language])

    cmd.append(str(search_path))

    try:
        # Create temporary file for output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            temp_path = f.name

        cmd_with_output = cmd + ["-f", temp_path]

        process = await asyncio.create_subprocess_exec(
            *cmd_with_output,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            return {
                "error": f"ctags failed: {stderr.decode()}",
                "returncode": process.returncode,
            }

        # Read and parse JSON output
        definitions = []
        with open(temp_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tag = json.loads(line)
                    tag_name = tag.get("name", "")

                    # Filter by symbol name
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
                except json.JSONDecodeError:
                    continue

        # Clean up temp file
        Path(temp_path).unlink(missing_ok=True)

        return {
            "symbol": symbol,
            "path": str(search_path),
            "definitions": definitions,
            "total": len(definitions),
        }

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
        definitions_result = await find_definitions(symbol, path, language, exact_match=True)
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

    if language:
        cmd.extend(["--languages", language])

    cmd.append(str(search_path))

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            temp_path = f.name

        cmd_with_output = cmd + ["-f", temp_path]

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

"""Tree-sitter wrapper for code structure analysis."""

from pathlib import Path
from typing import Any

# Language extension mapping
LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "c_sharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".lua": "lua",
    ".r": "r",
    ".R": "r",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".sh": "bash",
    ".bash": "bash",
}

# Node types to extract for each language
STRUCTURE_QUERIES = {
    "python": {
        "functions": ["function_definition"],
        "classes": ["class_definition"],
        "imports": ["import_statement", "import_from_statement"],
        "variables": ["assignment"],
    },
    "javascript": {
        "functions": ["function_declaration", "arrow_function", "function_expression"],
        "classes": ["class_declaration"],
        "imports": ["import_statement"],
        "exports": ["export_statement"],
        "variables": ["variable_declaration", "lexical_declaration"],
    },
    "typescript": {
        "functions": ["function_declaration", "arrow_function", "function_expression"],
        "classes": ["class_declaration"],
        "interfaces": ["interface_declaration"],
        "types": ["type_alias_declaration"],
        "imports": ["import_statement"],
        "exports": ["export_statement"],
    },
    "go": {
        "functions": ["function_declaration", "method_declaration"],
        "structs": ["type_declaration"],
        "imports": ["import_declaration"],
        "interfaces": ["type_declaration"],
    },
    "rust": {
        "functions": ["function_item"],
        "structs": ["struct_item"],
        "enums": ["enum_item"],
        "traits": ["trait_item"],
        "impls": ["impl_item"],
        "imports": ["use_declaration"],
    },
}


def detect_language(file_path: str) -> str | None:
    """Detect programming language from file extension."""
    ext = Path(file_path).suffix.lower()
    return LANGUAGE_EXTENSIONS.get(ext)


def analyze_structure(
    file_path: str | None = None,
    code: str | None = None,
    language: str | None = None,
) -> dict:
    """
    Analyze code structure using tree-sitter.

    Args:
        file_path: Path to source file (optional if code provided)
        code: Source code string (optional if file_path provided)
        language: Programming language (auto-detected from extension if not provided)

    Returns:
        Dictionary with extracted structure information
    """
    try:
        import tree_sitter_languages
    except ImportError:
        return {
            "error": "tree-sitter-languages not installed. Run: pip install tree-sitter-languages"
        }

    # Get code content
    if file_path:
        path = Path(file_path)
        if not path.exists():
            return {"error": f"File not found: {file_path}"}
        code = path.read_text(encoding="utf-8", errors="replace")
        if not language:
            language = detect_language(file_path)

    if not code:
        return {"error": "No code provided"}

    if not language:
        return {"error": "Could not detect language. Please specify explicitly."}

    # Get parser for language
    try:
        parser = tree_sitter_languages.get_parser(language)
    except Exception as e:
        return {"error": f"Unsupported language: {language}. Error: {str(e)}"}

    # Parse code
    tree = parser.parse(code.encode())
    root_node = tree.root_node

    # Extract structure
    structure = {
        "language": language,
        "file_path": file_path,
        "functions": [],
        "classes": [],
        "imports": [],
        "exports": [],
        "other": [],
    }

    def extract_name(node) -> str:
        """Extract the name from a node."""
        for child in node.children:
            if child.type in ("identifier", "name", "property_identifier"):
                return code[child.start_byte:child.end_byte]
            # For Python function/class definitions
            if child.type == "identifier":
                return code[child.start_byte:child.end_byte]
        # Fallback: first identifier in the node
        for child in node.children:
            if "identifier" in child.type or "name" in child.type:
                return code[child.start_byte:child.end_byte]
        return "<anonymous>"

    def extract_params(node) -> list[str]:
        """Extract parameter names from a function node."""
        params = []
        for child in node.children:
            if child.type in ("parameters", "formal_parameters", "parameter_list"):
                for param in child.children:
                    if param.type in ("identifier", "parameter", "typed_parameter", "required_parameter"):
                        # Get the parameter name
                        if param.type == "identifier":
                            params.append(code[param.start_byte:param.end_byte])
                        else:
                            # Look for identifier inside parameter node
                            for sub in param.children:
                                if sub.type == "identifier":
                                    params.append(code[sub.start_byte:sub.end_byte])
                                    break
        return params

    def traverse(node, depth=0):
        """Traverse AST and collect structure information."""
        node_type = node.type
        start_line = node.start_point[0] + 1  # 1-indexed
        end_line = node.end_point[0] + 1

        # Get language-specific queries
        queries = STRUCTURE_QUERIES.get(language, {})

        # Check if this node matches any category
        for category, types in queries.items():
            if node_type in types:
                info = {
                    "name": extract_name(node),
                    "type": node_type,
                    "start_line": start_line,
                    "end_line": end_line,
                }

                # Add parameters for functions
                if category == "functions":
                    info["parameters"] = extract_params(node)

                # Add to appropriate category
                if category in ("functions",):
                    structure["functions"].append(info)
                elif category in ("classes", "structs", "interfaces", "types", "traits", "enums"):
                    structure["classes"].append(info)
                elif category in ("imports",):
                    info["statement"] = code[node.start_byte:node.end_byte].strip()
                    structure["imports"].append(info)
                elif category in ("exports",):
                    structure["exports"].append(info)
                else:
                    structure["other"].append(info)

        # Recursively process children
        for child in node.children:
            traverse(child, depth + 1)

    traverse(root_node)

    # Add summary
    structure["summary"] = {
        "total_functions": len(structure["functions"]),
        "total_classes": len(structure["classes"]),
        "total_imports": len(structure["imports"]),
        "total_lines": code.count("\n") + 1,
    }

    return structure


def get_function_at_line(file_path: str, line_number: int) -> dict:
    """
    Get the function that contains a specific line.

    Args:
        file_path: Path to source file
        line_number: Line number (1-indexed)

    Returns:
        Function information or error
    """
    structure = analyze_structure(file_path=file_path)

    if "error" in structure:
        return structure

    for func in structure["functions"]:
        if func["start_line"] <= line_number <= func["end_line"]:
            return func

    return {"error": f"No function found at line {line_number}"}


def get_class_at_line(file_path: str, line_number: int) -> dict:
    """
    Get the class that contains a specific line.

    Args:
        file_path: Path to source file
        line_number: Line number (1-indexed)

    Returns:
        Class information or error
    """
    structure = analyze_structure(file_path=file_path)

    if "error" in structure:
        return structure

    for cls in structure["classes"]:
        if cls["start_line"] <= line_number <= cls["end_line"]:
            return cls

    return {"error": f"No class found at line {line_number}"}

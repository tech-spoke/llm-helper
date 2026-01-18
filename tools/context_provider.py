"""
Essential Context Provider for Code Intelligence MCP Server.

v1.1: Context-Aware Guardrails
- Provides design document summaries at session start
- Provides project rules (DO/DON'T) from CLAUDE.md
- Integrates with sync_index for freshness checking
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DocSummary:
    """Summary of a design document."""
    file: str
    path: str
    summary: str
    extra_notes: str = ""
    content_hash: str = ""


@dataclass
class DocResearchConfig:
    """Document research configuration for v1.3."""
    enabled: bool = True
    docs_path: list[str] = field(default_factory=list)
    default_prompts: list[str] = field(default_factory=lambda: ["default.md"])


@dataclass
class EssentialContext:
    """Essential context loaded from context.yml."""
    design_docs: list[DocSummary] = field(default_factory=list)
    design_docs_source: str = ""
    project_rules_source: str = ""
    project_rules_summary: str = ""
    project_rules_extra_notes: str = ""
    last_synced: str = ""
    # v1.3: Document research config
    doc_research: DocResearchConfig | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {}

        if self.design_docs:
            result["design_docs"] = {
                "source": self.design_docs_source,
                "summaries": [
                    {
                        "file": doc.file,
                        "path": doc.path,
                        "summary": doc.summary,
                        **({"extra_notes": doc.extra_notes} if doc.extra_notes else {}),
                    }
                    for doc in self.design_docs
                ],
            }

        if self.project_rules_summary:
            result["project_rules"] = {
                "source": self.project_rules_source,
                "summary": self.project_rules_summary,
                **({"extra_notes": self.project_rules_extra_notes} if self.project_rules_extra_notes else {}),
            }

        if self.last_synced:
            result["last_synced"] = self.last_synced

        # v1.3: Document research configuration
        if self.doc_research:
            result["doc_research"] = {
                "enabled": self.doc_research.enabled,
                "docs_path": self.doc_research.docs_path,
                "default_prompts": self.doc_research.default_prompts,
            }

        return result


class ContextProvider:
    """
    Provides essential context for LLM sessions.

    Loads design document summaries and project rules from context.yml.
    """

    def __init__(self, repo_path: str = "."):
        self.repo_path = Path(repo_path)
        self.code_intel_dir = self.repo_path / ".code-intel"
        self.context_file = self.code_intel_dir / "context.yml"

    def load_context(self) -> EssentialContext | None:
        """
        Load essential context from context.yml.

        Returns None if context.yml doesn't exist or has no essential_docs/project_rules.
        """
        if not self.context_file.exists():
            return None

        try:
            with open(self.context_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except Exception:
            return None

        if not config:
            return None

        context = EssentialContext()
        context.last_synced = config.get("last_synced", "")

        # Load design docs
        essential_docs = config.get("essential_docs", {})
        if essential_docs:
            context.design_docs_source = essential_docs.get("source", "")
            summaries = essential_docs.get("summaries", [])
            for s in summaries:
                context.design_docs.append(DocSummary(
                    file=s.get("file", ""),
                    path=s.get("path", ""),
                    summary=s.get("summary", ""),
                    extra_notes=s.get("extra_notes", ""),
                    content_hash=s.get("content_hash", ""),
                ))

        # Load project rules
        project_rules = config.get("project_rules", {})
        if project_rules:
            context.project_rules_source = project_rules.get("source", "")
            context.project_rules_summary = project_rules.get("summary", "")
            context.project_rules_extra_notes = project_rules.get("extra_notes", "")

        # v1.3: Load doc_research configuration
        doc_research_config = config.get("doc_research", {})
        if doc_research_config:
            docs_path = doc_research_config.get("docs_path", [])
            # Normalize to list
            if isinstance(docs_path, str):
                docs_path = [docs_path]
            context.doc_research = DocResearchConfig(
                enabled=doc_research_config.get("enabled", True),
                docs_path=docs_path,
                default_prompts=doc_research_config.get("default_prompts", ["default.md"]),
            )
        else:
            # Auto-detect docs_path if not configured
            detected_paths = self._detect_docs_path()
            if detected_paths:
                context.doc_research = DocResearchConfig(
                    enabled=True,
                    docs_path=detected_paths,
                    default_prompts=["default.md"],
                )

        # Check if we have any meaningful content
        # v1.3: doc_research alone is also meaningful
        if not context.design_docs and not context.project_rules_summary and not context.doc_research:
            return None

        return context

    def get_context_config(self) -> dict | None:
        """
        Get the raw context configuration (sources only, not summaries).

        Used to check what should be indexed.
        """
        if not self.context_file.exists():
            return None

        try:
            with open(self.context_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config
        except Exception:
            return None

    def check_docs_changed(self) -> list[dict]:
        """
        Check if any source documents have changed since last sync.

        Returns a list of changed files with their paths and change types.
        """
        config = self.get_context_config()
        if not config:
            return []

        changes = []

        # Check essential_docs
        essential_docs = config.get("essential_docs", {})
        if essential_docs:
            source = essential_docs.get("source", "")
            if source:
                source_path = self.repo_path / source
                if source_path.exists() and source_path.is_dir():
                    summaries = {s.get("path"): s for s in essential_docs.get("summaries", [])}

                    for md_file in source_path.glob("**/*.md"):
                        rel_path = str(md_file.relative_to(self.repo_path))
                        current_hash = self._file_hash(md_file)

                        if rel_path in summaries:
                            stored_hash = summaries[rel_path].get("content_hash", "")
                            if stored_hash and stored_hash != current_hash:
                                changes.append({
                                    "type": "essential_doc",
                                    "path": rel_path,
                                    "change": "modified",
                                })
                        else:
                            changes.append({
                                "type": "essential_doc",
                                "path": rel_path,
                                "change": "new",
                            })

        # Check project_rules
        project_rules = config.get("project_rules", {})
        if project_rules:
            source = project_rules.get("source", "")
            if source:
                source_path = self.repo_path / source
                if source_path.exists():
                    current_hash = self._file_hash(source_path)
                    stored_hash = project_rules.get("content_hash", "")
                    if stored_hash and stored_hash != current_hash:
                        changes.append({
                            "type": "project_rules",
                            "path": source,
                            "change": "modified",
                        })
                    elif not stored_hash:
                        changes.append({
                            "type": "project_rules",
                            "path": source,
                            "change": "new",
                        })

        return changes

    def extract_doc_summary(self, file_path: Path) -> str:
        """
        Extract a simple summary from a markdown file.

        This is a fallback when LLM-generated summaries are not available.
        Extracts: title + headers + first paragraph of each section.
        """
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return ""

        lines = content.split("\n")
        summary_parts = []

        current_section = []
        in_code_block = False

        for line in lines:
            # Track code blocks
            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                continue

            if in_code_block:
                continue

            # Headers
            if line.startswith("#"):
                # Save previous section's first paragraph
                if current_section:
                    first_para = self._get_first_paragraph(current_section)
                    if first_para:
                        summary_parts.append(first_para)
                    current_section = []

                # Add header
                summary_parts.append(line.strip())
            else:
                current_section.append(line)

        # Don't forget last section
        if current_section:
            first_para = self._get_first_paragraph(current_section)
            if first_para:
                summary_parts.append(first_para)

        return "\n".join(summary_parts)

    def extract_project_rules(self, file_path: Path) -> str:
        """
        Extract DO/DON'T rules from a project rules file (e.g., CLAUDE.md).

        This is a fallback when LLM-generated summaries are not available.
        """
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return ""

        # Try to find existing DO/DON'T sections
        do_pattern = r"(?:^|\n)#+\s*(?:DO|すべきこと|やること)[^\n]*\n((?:[-*]\s*[^\n]+\n?)+)"
        dont_pattern = r"(?:^|\n)#+\s*(?:DON'?T|すべきでないこと|やらないこと|禁止)[^\n]*\n((?:[-*]\s*[^\n]+\n?)+)"

        do_matches = re.findall(do_pattern, content, re.IGNORECASE | re.MULTILINE)
        dont_matches = re.findall(dont_pattern, content, re.IGNORECASE | re.MULTILINE)

        summary_parts = []

        if do_matches:
            summary_parts.append("DO:")
            for match in do_matches:
                for line in match.strip().split("\n"):
                    if line.strip():
                        summary_parts.append(line.strip())

        if dont_matches:
            summary_parts.append("\nDON'T:")
            for match in dont_matches:
                for line in match.strip().split("\n"):
                    if line.strip():
                        summary_parts.append(line.strip())

        if summary_parts:
            return "\n".join(summary_parts)

        # Fallback: extract all bullet points as rules
        bullet_pattern = r"^[-*]\s+(.+)$"
        bullets = re.findall(bullet_pattern, content, re.MULTILINE)

        if bullets:
            return "Rules:\n" + "\n".join(f"- {b}" for b in bullets[:20])  # Limit to 20

        return ""

    def generate_initial_context(self) -> dict:
        """
        Generate initial context.yml structure based on detected files.

        Returns a dict that can be written as YAML.
        """
        result = {}

        # Check for common design doc locations
        design_dirs = [
            "docs/設計資料/アーキテクチャ",
            "docs/architecture",
            "docs/design",
            "docs",
        ]

        for design_dir in design_dirs:
            design_path = self.repo_path / design_dir
            if design_path.exists() and design_path.is_dir():
                md_files = list(design_path.glob("*.md"))
                if md_files:
                    result["essential_docs"] = {
                        "source": design_dir,
                        "summaries": [],
                    }
                    break

        # Check for project rules files
        rules_files = [
            ".claude/CLAUDE.md",
            "CLAUDE.md",
            ".cursor/rules.md",
            "CONTRIBUTING.md",
        ]

        for rules_file in rules_files:
            rules_path = self.repo_path / rules_file
            if rules_path.exists():
                result["project_rules"] = {
                    "source": rules_file,
                    "summary": "",
                }
                break

        # Add document_search defaults (for analyze_impact keyword search)
        result["document_search"] = {
            "include_patterns": [
                "**/*.md",
                "**/README*",
                "**/docs/**/*",
            ],
            "exclude_patterns": [
                "node_modules/**",
                "vendor/**",
                ".git/**",
                ".venv/**",
                "__pycache__/**",
            ],
        }

        return result

    def save_context(self, config: dict) -> None:
        """Save context configuration to context.yml."""
        self.code_intel_dir.mkdir(parents=True, exist_ok=True)

        with open(self.context_file, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def update_summaries(self, summaries: list[DocSummary], project_rules_summary: str = "") -> None:
        """
        Update summaries in context.yml while preserving extra_notes.

        This is called after LLM generates new summaries.
        """
        config = self.get_context_config() or {}

        # Update design doc summaries
        if summaries:
            essential_docs = config.get("essential_docs", {})
            existing_summaries = {s.get("path"): s for s in essential_docs.get("summaries", [])}

            new_summaries = []
            for s in summaries:
                existing = existing_summaries.get(s.path, {})
                new_summaries.append({
                    "file": s.file,
                    "path": s.path,
                    "summary": s.summary,
                    "content_hash": s.content_hash,
                    # Preserve existing extra_notes
                    **({"extra_notes": existing.get("extra_notes", "")} if existing.get("extra_notes") else {}),
                })

            essential_docs["summaries"] = new_summaries
            config["essential_docs"] = essential_docs

        # Update project rules summary
        if project_rules_summary:
            project_rules = config.get("project_rules", {})
            project_rules["summary"] = project_rules_summary
            config["project_rules"] = project_rules

        config["last_synced"] = datetime.now().isoformat()

        self.save_context(config)

    def _file_hash(self, file_path: Path) -> str:
        """Calculate SHA256 hash of a file."""
        try:
            content = file_path.read_bytes()
            return hashlib.sha256(content).hexdigest()[:16]
        except Exception:
            return ""

    def _get_first_paragraph(self, lines: list[str]) -> str:
        """Extract first non-empty paragraph from lines."""
        paragraph = []
        started = False

        for line in lines:
            stripped = line.strip()
            if stripped:
                started = True
                paragraph.append(stripped)
            elif started:
                break

        return " ".join(paragraph) if paragraph else ""

    def _detect_docs_path(self) -> list[str]:
        """
        Auto-detect documentation paths for v1.3 DOCUMENT_RESEARCH.

        Returns a list of existing documentation paths.
        """
        detected = []

        # Check for docs directory
        docs_dir = self.repo_path / "docs"
        if docs_dir.exists() and docs_dir.is_dir():
            # Verify it contains markdown files
            md_files = list(docs_dir.glob("**/*.md"))
            if md_files:
                detected.append("docs/")

        # Check for DESIGN.md or DESIGN*.md
        design_files = list(self.repo_path.glob("DESIGN*.md"))
        for f in design_files:
            detected.append(f.name)

        # Fallback: README.md (if no other docs found)
        if not detected:
            readme = self.repo_path / "README.md"
            if readme.exists():
                detected.append("README.md")

        return detected

    def get_doc_research_config(self) -> DocResearchConfig | None:
        """
        Get document research configuration for v1.3.

        Returns DocResearchConfig from context.yml or auto-detected.
        """
        context = self.load_context()
        if context and context.doc_research:
            return context.doc_research

        # If no context.yml exists, try auto-detection
        detected_paths = self._detect_docs_path()
        if detected_paths:
            return DocResearchConfig(
                enabled=True,
                docs_path=detected_paths,
                default_prompts=["default.md"],
            )

        return None


# =============================================================================
# Summary Generation Prompts (for external LLM use)
# =============================================================================

DESIGN_DOC_SUMMARY_PROMPT = """以下の設計ドキュメントから、実装時に守るべき決定事項と制約を抽出してください。
箇条書きで、各項目は1文以内で簡潔に。
技術的な命名規則、アーキテクチャの制約、禁止事項を優先してください。

---
{document_content}
---"""

PROJECT_RULES_SUMMARY_PROMPT = """以下のプロジェクトルールから、コード実装時に守るべき
「DO（すべきこと）」と「DON'T（禁止事項）」を抽出してください。
箇条書きで、各項目は命令形で簡潔に。
ディレクトリ構造や命名規則があれば必ず含めてください。

---
{document_content}
---"""


def get_summary_prompts() -> dict[str, str]:
    """Get prompts for LLM summary generation."""
    return {
        "design_doc": DESIGN_DOC_SUMMARY_PROMPT,
        "project_rules": PROJECT_RULES_SUMMARY_PROMPT,
    }

# Documentation Rules / ドキュメント管理ルール

## Overview / 概要

This document defines the rules for maintaining documentation in this project.
このドキュメントは、本プロジェクトのドキュメント管理ルールを定義します。

---

## Directory Structure / ディレクトリ構成

```
docs/
├── DOCUMENTATION_RULES.md    # This file / このファイル
├── DESIGN.md                 # Main design doc (English)
├── DESIGN_ja.md              # Main design doc (Japanese)
├── updates/                   # Version update changelogs
│   ├── v1.1.md               # English
│   ├── v1.1_ja.md            # Japanese
│   ├── v1.2.md
│   ├── v1.2_ja.md
│   ├── v1.3.md
│   └── v1.3_ja.md
├── draft/                     # Version upgrade design drafts (WIP)
├── old/                       # Archived old documents (gitignored)
└── qiita/                     # Qiita articles (gitignored)
```

---

## Main Documents / メインドキュメント

### DESIGN.md / DESIGN_ja.md

- **Purpose**: Describes the **current state** of the system
- **Location**: `docs/DESIGN.md` (English), `docs/DESIGN_ja.md` (Japanese)
- **Content**: Always reflects the latest version's complete specification
- **Note**: No version number in filename - always describes current version

### README.md / README_ja.md

- **Purpose**: Project overview and quick start
- **Location**: Repository root
- **Content**: Always reflects the current state

---

## Version Update Process / バージョンアップ時の手順

### 1. Create Update Document / 更新ドキュメント作成

Create new files in `docs/updates/` with the version number (both English and Japanese):

**Filename Format**: `v{MAJOR}.{MINOR}.md` and `v{MAJOR}.{MINOR}_ja.md`

Examples:
- `v1.1.md`, `v1.1_ja.md` - Minor version update
- `v1.2.md`, `v1.2_ja.md` - Minor version update
- `v2.0.md`, `v2.0_ja.md` - Major version update

### 2. Update Document Template / 更新ドキュメントテンプレート

**English (`v1.X.md`):**
```markdown
# v1.X Update

Release Date: YYYY-MM-DD

## Summary

Brief description of what this version adds.

## New Features

### Feature Name

Description of the feature.

**Usage:**
```
example code or command
```

## Breaking Changes

- List any breaking changes

## Migration Guide

Steps to migrate from previous version.

## Bug Fixes

- List of bug fixes
```

**Japanese (`v1.X_ja.md`):**
```markdown
# v1.X アップデート

Release Date: YYYY-MM-DD

## 概要

このバージョンで追加された機能の概要。

## 新機能

### 機能名

機能の説明。

**使用例:**
```
example code or command
```

## 破壊的変更

- 破壊的変更のリスト

## 移行ガイド

前バージョンからの移行手順。

## バグ修正

- バグ修正のリスト
```

### 3. Update Main Documents / メインドキュメント更新

After creating the update documents, update:

| Document | Action |
|----------|--------|
| `docs/DESIGN.md` | Rewrite to reflect current state (English) |
| `docs/DESIGN_ja.md` | Rewrite to reflect current state (Japanese) |
| `README.md` | Update to reflect current state |
| `README_ja.md` | Update to reflect current state |

**IMPORTANT**:
- DESIGN docs should describe the **current system**, not accumulate version notes
- Add CHANGELOG section referencing `docs/updates/` for history
- Always update both English and Japanese versions together

### 4. Add CHANGELOG Reference / CHANGELOG参照の追加

Add or update the CHANGELOG section in DESIGN docs:

**English:**
```markdown
## CHANGELOG

For version history and detailed changes, see:

| Version | Description | Link |
|---------|-------------|------|
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3.md) |
| v1.2 | OverlayFS, Gate Levels | [v1.2](updates/v1.2.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1.md) |
```

**Japanese:**
```markdown
## CHANGELOG

バージョン履歴と詳細な変更内容については以下を参照してください：

| Version | Description | Link |
|---------|-------------|------|
| v1.3 | Document Research, Markup Cross-Reference | [v1.3](updates/v1.3_ja.md) |
| v1.2 | OverlayFS, Gate Levels | [v1.2](updates/v1.2_ja.md) |
| v1.1 | Impact Analysis, Context Provider | [v1.1](updates/v1.1_ja.md) |
```

---

## Checklist / チェックリスト

Use this checklist for every version update:

```
[ ] docs/updates/v{VERSION}.md created (English)
[ ] docs/updates/v{VERSION}_ja.md created (Japanese)
[ ] docs/DESIGN.md updated to current state
[ ] docs/DESIGN_ja.md updated to current state
[ ] CHANGELOG section added/updated in DESIGN docs
[ ] README.md updated
[ ] README_ja.md updated
[ ] CHANGELOG section added/updated in README files
```

---

## Language Sync / 言語間の同期

**CRITICAL**: Always update both English and Japanese versions together.

| English | Japanese |
|---------|----------|
| `docs/DESIGN.md` | `docs/DESIGN_ja.md` |
| `docs/updates/v{X.Y}.md` | `docs/updates/v{X.Y}_ja.md` |
| `README.md` | `README_ja.md` |

---

## What Goes Where / 記載場所

| Content Type | Location |
|--------------|----------|
| Current system specification | `DESIGN.md` / `DESIGN_ja.md` |
| Version-specific changes | `docs/updates/v{X.Y}.md` / `docs/updates/v{X.Y}_ja.md` |
| Quick start & overview | `README.md` / `README_ja.md` |
| Version upgrade design drafts | `docs/draft/` |
| Internal implementation | `DESIGN.md` (Internal Reference section) |
| Archived old documents | `docs/old/` (gitignored) |
| Qiita articles (private) | `docs/qiita/` (gitignored) |

---

## Draft Folder Usage / ドラフトフォルダの使い方

The `docs/draft/` folder is for **version upgrade design documents** before they are finalized.

`docs/draft/` フォルダは、**バージョンアップ設計資料**の完成前のドラフトを置く場所です。

**Workflow / ワークフロー:**

1. Create design draft in `docs/draft/` (e.g., `v1.4-feature-name.md`)
2. Iterate on the design until implementation is complete
3. Once implemented, integrate the content into `DESIGN.md` / `DESIGN_ja.md`
4. Create the changelog in `docs/updates/v{X.Y}.md` / `docs/updates/v{X.Y}_ja.md`
5. Delete the draft file

**Naming Convention / 命名規則:**

- `v{VERSION}-{feature-name}.md` (e.g., `v1.4-session-cache.md`)
- No language suffix needed (drafts are typically single-language)

**Notes / 注意:**

- Drafts are excluded from document research (`doc_research`)
- Delete drafts after the feature is released and documented

---

## Anti-Patterns / アンチパターン

**DON'T**:
- Add "v1.X additions" sections that accumulate in DESIGN docs
- Keep outdated information in main documents
- Update only one language
- Forget to update README files
- Create separate INTERNALS documents (merged into DESIGN)
- Use `en/` or `ja/` subdirectories (flat structure preferred)

**DO**:
- Rewrite DESIGN docs to reflect current state
- Keep update history in `docs/updates/`
- Always update English and Japanese together
- Reference CHANGELOG for history
- Use `_ja` suffix for Japanese files

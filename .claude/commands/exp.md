# /exp - Fast Code Exploration Command

**Purpose**: Lightweight exploration and investigation leveraging parallel search

## Features

- **Parallel execution**: Automatically executes search_text, Read, and Grep in parallel
- **Lightweight**: Focused on exploration and understanding, no implementation
- **Fast**: 20-30 seconds faster than standard exploration due to parallel execution

## Usage

Simply specify what you want to explore in natural language:

```
/exp Find all authentication related code
/exp Understand how the modal system works
/exp List all API endpoints
```

## Execution Process

### 1. Task Understanding
Analyze the user's exploration objective

### 2. Parallel Search Execution

**⚠️ CRITICAL: Always use parallel execution**

#### search_text (Multiple Patterns)
Identify necessary patterns and search in parallel with **a single call**:

✅ **Correct method**:
```
Search with mcp__code-intel__search_text using ["pattern1", "pattern2", "pattern3"]
```

❌ **Wrong method**:
```
search_text("pattern1")
<!-- wait -->
search_text("pattern2")
<!-- wait -->
search_text("pattern3")
```

#### Read (Multiple Files)
Read related files in parallel with **a single message**:

✅ **Correct method**:
```xml
<Read file_path="file1.py" />
<Read file_path="file2.py" />
<Read file_path="file3.py" />
```

#### Grep (Multiple Patterns)
Search multiple patterns in parallel with **a single message**:

✅ **Correct method**:
```xml
<Grep pattern="class.*Service" />
<Grep pattern="function.*handler" />
<Grep pattern="async def" />
```

### 3. Organizing and Reporting Results

Organize exploration results and report in the following format:

```markdown
## Exploration Results

### Discovered Files
- file1.py: Role description
- file2.py: Role description

### Key Patterns
- Pattern 1: Description
- Pattern 2: Description

### Architecture
Brief description

### Next Steps (Optional)
Recommended investigation directions
```

## Parallel Execution Principles

**When using the same tool multiple times, always combine them into a single message/call**

| Tool | Parallelization Method | Time Saved |
|------|----------------------|------------|
| search_text | Pass patterns as array | 15-20 sec |
| Read | Multiple calls in one message | 4-6 sec |
| find_definitions | Multiple calls in one message | 2-3 sec |
| find_references | Multiple calls in one message | 2-3 sec |
| Grep | Multiple calls in one message | 2-4 sec |
| Glob | Multiple calls in one message | 1-2 sec |
| Edit* | Multiple calls in one message (different files) | 2-4 sec |
| Write* | Multiple calls in one message | 2-4 sec |

*Edit and Write are not available in `/exp` (exploration only), but follow the same parallel execution principle in `/code`

## Usage Examples

### Example 1: Understanding Authentication
```
/exp Find all authentication related code
```

Execution:
1. search_text(["auth", "login", "session", "token", "password"])
2. Read discovered files in parallel
3. Analyze structure and report

### Example 2: Listing API Endpoints
```
/exp List all API endpoints
```

Execution:
1. Glob("**/*controller*.py"), Glob("**/*route*.py"), Glob("**/*api*.py")
2. Read discovered files in parallel
3. Extract and report endpoint list

### Example 3: Understanding Modal System
```
/exp Understand how the modal system works
```

Execution:
1. search_text(["modal", "dialog", "popup", "overlay"])
2. Read related files in parallel
3. Explain how the system works

## Prohibited Actions

- ❌ Edit/Write/Bash not allowed (exploration only)
- ❌ No implementation work
- ❌ Sequential execution (always use parallel)
- ❌ No git operations

## Differences from `/code`

| Item | /exp | /code |
|------|------|-------|
| Purpose | Exploration/Understanding | Implementation |
| Implementation | ❌ | ✅ |
| Phase Gates | None | Yes |
| Duration | 30-90 sec | 402 sec |
| Parallel Execution | Mandatory | Automatic |
| Git Operations | None | Yes |

## Important Notes

**This command is designed for parallel execution.**

- search_text must always pass multiple patterns as an array
- Read/Grep must always be called multiple times in a single message
- Sequential execution eliminates time-saving benefits

See `.claude/PARALLEL_GUIDE.md` for detailed parallel execution guide.

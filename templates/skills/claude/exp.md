# /exp - Fast Parallel Execution Tool

**Purpose**: Lightweight implementation tool optimized for parallel execution

## Features

- **Parallel execution**: Core design - all tools (search_text, Read, Grep, Edit, Write, find_*, Glob) execute in parallel
- **No Phase Gates**: Direct execution without mandatory workflow constraints
- **Fast**: 20-30 seconds faster than /code due to parallel execution and zero overhead
- **Flexible**: Use for quick fixes, investigation, simple implementations, or any task benefiting from speed

## Usage

Simply specify what you want to accomplish in natural language:

```
/exp Find all authentication related code
/exp Fix the button color in header to blue
/exp Understand how the modal system works
/exp Add close button to modal component
```

## Execution Process

### 1. Task Understanding
Analyze the user's task objective

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

Organize results and report in the following format:

```markdown
## Results

### Discovered Files
- file1.py: Role description
- file2.py: Role description

### Key Patterns
- Pattern 1: Description
- Pattern 2: Description

### Implementation (if applicable)
Changes made

### Next Steps (Optional)
Recommended actions
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
| Edit | Multiple calls in one message (different files) | 2-4 sec |
| Write | Multiple calls in one message | 2-4 sec |

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

### Example 4: Quick Fix Implementation
```
/exp Fix the button color in header to blue
```

Execution:
1. Glob("**/*header*"), search_text(["button", "color"])
2. Read discovered files in parallel
3. Identify the target file (e.g., styles.css)
4. Edit the file to change button color
5. Report completion

### Example 5: Investigation + Implementation
```
/exp Find modal component and add close button
```

Execution:
1. search_text(["modal", "component"])
2. Read modal files in parallel
3. Analyze structure
4. Edit modal.tsx to add close button
5. Write test file for close functionality
6. Report completion

## Important Notes

- ✅ Edit/Write/Bash are allowed (full implementation capability)
- ⚡ Sequential execution prohibited (parallel execution is mandatory)
- ⚠️ No git operations (commits, branches) - use `/code` for full workflow with version control
- ✅ Zero overhead - no Phase Gates, no mandatory verification

## Differences from `/code`

| Item | /exp | /code |
|------|------|-------|
| Purpose | Fast Parallel Execution | Full Workflow with Safety Checks |
| Implementation | ✅ (direct) | ✅ (with verification) |
| Phase Gates | None | Yes (mandatory) |
| Duration | 30-120 sec | 402 sec |
| Parallel Execution | Mandatory (core design) | Automatic |
| Git Operations | None | Yes (branch, commit, merge) |
| Verification | None | Automatic |
| Best For | Quick tasks, speed priority | Complex changes, safety priority |

## Important Notes

**This command is designed for parallel execution.**

- search_text must always pass multiple patterns as an array
- Read/Grep must always be called multiple times in a single message
- Sequential execution eliminates time-saving benefits

See `.claude/PARALLEL_GUIDE.md` for detailed parallel execution guide.

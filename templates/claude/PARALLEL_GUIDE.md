# Claude Code Efficiency Guide

## 🔴 CRITICAL: Always Use `/exp` Command

**Always use the `/exp` command when searching for, reading, or exploring files.**

- When the user says "investigate XX" or "check XX"
- When you need to check related files before implementation
- When you need to determine which files to edit

**Example**:
- User: "Edit files in the sample folder"
- You: First `/exp` to investigate the sample folder → Determine edit targets → Implement with Edit

## ⚡ Time Savings Through Parallel Execution (v1.7)

Claude Code can execute multiple tool calls in parallel within a single message, enabling significant time savings.

### Basic Principle

**When calling the same tool multiple times, always call them together in a single message**

### Effective Patterns

#### 1. Reading Multiple Files

❌ **Slow method** (sequential execution):
```xml
<Read file_path="file1.py" />
<!-- wait -->
<Read file_path="file2.py" />
<!-- wait -->
<Read file_path="file3.py" />
```

✅ **Fast method** (parallel execution):
```xml
<Read file_path="file1.py" />
<Read file_path="file2.py" />
<Read file_path="file3.py" />
```

**Time saved**: 4-6 seconds

#### 2. Multiple Pattern Search (Grep)

❌ **Slow method**:
```xml
<Grep pattern="class.*Service" />
<!-- wait -->
<Grep pattern="function.*calculate" />
<!-- wait -->
<Grep pattern="interface.*Repository" />
```

✅ **Fast method**:
```xml
<Grep pattern="class.*Service" />
<Grep pattern="function.*calculate" />
<Grep pattern="interface.*Repository" />
```

**Time saved**: 2-4 seconds

#### 3. Multiple Pattern Text Search (search_text, v1.7 feature)

❌ **Slow method**:
```
search "modal" with mcp__code-intel__search_text
<!-- wait -->
search "dialog" with mcp__code-intel__search_text
<!-- wait -->
search "popup" with mcp__code-intel__search_text
```

✅ **Fast method**:
```
search ["modal", "dialog", "popup"] with mcp__code-intel__search_text
```

**Time saved**: 15-20 seconds

#### 4. Documentation Updates

❌ **Slow method**:
```xml
<Read file_path="README.md" />
<!-- check content -->
<Edit file_path="README.md" ... />
<!-- wait -->
<Read file_path="CHANGELOG.md" />
<!-- check content -->
<Edit file_path="CHANGELOG.md" ... />
```

✅ **Fast method**:
```xml
<!-- Read all necessary files first -->
<Read file_path="README.md" />
<Read file_path="CHANGELOG.md" />
<Read file_path="docs/guide.md" />
<!-- After checking content, edit in parallel -->
<Edit file_path="README.md" ... />
<Edit file_path="CHANGELOG.md" ... />
<Edit file_path="docs/guide.md" ... />
```

**Time saved**: 5-10 seconds

### Applicable Tools

The following tools benefit from parallel execution:

| Tool | Parallel Execution | Effect |
|------|-------------------|---------|
| Read | ✅ | 4-6 sec/file |
| Grep | ✅ | 2-3 sec/pattern |
| Glob | ✅ | 1-2 sec/pattern |
| search_text (v1.7) | ✅ | Pass multiple patterns as array |
| Edit | ✅ | 2-3 sec/file |
| Write | ✅ | 2-3 sec/file |
| Bash | ❌ | Sequential due to dependencies |

### Usage Examples

#### Code Review
```xml
<!-- Read all related files in parallel -->
<Read file_path="src/auth/service.py" />
<Read file_path="src/auth/controller.py" />
<Read file_path="tests/test_auth.py" />
<Read file_path="docs/auth_design.md" />

<!-- After analysis, update multiple files in parallel -->
<Edit file_path="src/auth/service.py" ... />
<Edit file_path="tests/test_auth.py" ... />
```

#### Bulk Documentation Updates
```xml
<!-- Read documents in parallel -->
<Read file_path="README.md" />
<Read file_path="README_ja.md" />
<Read file_path="docs/api.md" />

<!-- After checking content, update in parallel -->
<Edit file_path="README.md" ... />
<Edit file_path="README_ja.md" ... />
<Edit file_path="docs/api.md" ... />
```

### Total Time Savings Example

Typical `/code` task (402 seconds):
- EXPLORATION: **20 seconds saved** with search_text parallelization
- READY: **5-10 seconds saved** with Read/Grep parallelization
- Other phases: **2-5 seconds saved**

**Total savings**: 27-35 seconds (approx 7-9%)

### Important Notes

1. **Use sequential execution when there are dependencies**
   - Example: Reading a file after creating it
   - Chain Bash commands with `&&` for sequential execution

2. **search_text limitations**
   - Maximum 5 patterns
   - Split into multiple calls if more are needed

3. **Truncation prevention**
   - Be mindful of 30,000 character limit when fetching large amounts of data in parallel
   - search_text limits patterns to 5 to mitigate this

## `/exp` Command - Fast Investigation with Parallel Execution

### What is `/exp`

A lightweight investigation and exploration command that automatically leverages parallel execution.
Can be used for both pre-implementation investigation and during implementation.

### When to Use

- When you want to understand code structure
- When looking for specific patterns
- When investigating related code before implementation
- When you need to verify something during implementation

### How to Use

```
/exp Find all authentication related code
/exp Understand how the modal system works
/exp List all API endpoints in the project
/exp Investigate files in the sample folder for testing
```

### Features

- **Automatic parallel execution**: Automatically executes search_text, Read, and Grep in parallel
- **Fast**: 20-30 seconds faster than standard investigation
- **Lightweight**: Focused on investigation and understanding, no implementation

### Details

See [commands/exp.md](commands/exp.md) for implementation details.

## References

- [/exp Command Details (commands/exp.md)](commands/exp.md)
- [Project Rules (CLAUDE.md)](CLAUDE.md)

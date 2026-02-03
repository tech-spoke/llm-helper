# Quality Review Checklist

Review the changes and check for the following issues:

## Security (CRITICAL)

- [ ] No hardcoded credentials (API keys, passwords, tokens)
- [ ] No sensitive data in logs
- [ ] Input validation where needed
- [ ] No SQL injection risks (string concatenation in queries)
- [ ] No XSS vulnerabilities (unescaped user input)
- [ ] No path traversal risks (user-controlled file paths)
- [ ] No CSRF vulnerabilities
- [ ] No authentication bypasses

## Code Quality (HIGH)

- [ ] No unused imports
- [ ] No dead code or unreachable statements
- [ ] No duplicate code that should be extracted
- [ ] Proper error handling (try/catch)
- [ ] No large functions (>50 lines)
- [ ] No large files (>800 lines)
- [ ] No deep nesting (>4 levels)
- [ ] No console.log/print statements left in

## Conventions (MEDIUM)

- [ ] Follows project naming conventions
- [ ] Matches existing code style
- [ ] Follows CLAUDE.md rules
- [ ] No TODO/FIXME without tickets
- [ ] No magic numbers without explanation

## Performance (MEDIUM)

- [ ] No obvious N+1 queries
- [ ] No unnecessary loops or iterations
- [ ] Efficient data structures used
- [ ] No O(n²) when O(n log n) possible
- [ ] Missing memoization considered
- [ ] Missing caching considered

## Priority Classification

- **CRITICAL**: Security issues, data loss risks - must fix
- **HIGH**: Code quality, missing error handling - should fix
- **MEDIUM**: Performance, conventions - consider fixing

## Approval Criteria

- ✅ **Approve**: No CRITICAL or HIGH issues
- ⚠️ **Warning**: MEDIUM issues only (can proceed with caution)
- ❌ **Block**: CRITICAL or HIGH issues found

## Output Format

For each issue found:
```
[CRITICAL] Hardcoded API key
File: src/api/client.ts:42
Issue: API key exposed in source code
Fix: Move to environment variable
```

## Action

**If issues found:**
1. List all issues with file:line references
2. Report with submit_quality_review(issues_found=true, issues=[...])
3. You will be reverted to READY phase
4. Fix the issues, then proceed through POST_IMPL_VERIFY → PRE_COMMIT → QUALITY_REVIEW

**If no issues:**
1. Report with submit_quality_review(issues_found=false)
2. Proceed to merge_to_base

**Important:** Do NOT fix issues in QUALITY_REVIEW phase. Always report first, then fix in READY phase.

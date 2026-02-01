# Quality Review Checklist

Review the changes and check for the following issues:

## Code Quality
- [ ] No unused imports
- [ ] No dead code or unreachable statements
- [ ] No duplicate code that should be extracted
- [ ] Proper error handling

## Conventions
- [ ] Follows project naming conventions
- [ ] Matches existing code style
- [ ] Follows CLAUDE.md rules

## Security
- [ ] No hardcoded secrets or credentials
- [ ] No sensitive data in logs
- [ ] Input validation where needed

## Performance
- [ ] No obvious N+1 queries
- [ ] No unnecessary loops or iterations
- [ ] Efficient data structures used

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

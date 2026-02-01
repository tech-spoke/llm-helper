# User Escalation

## When to Select

Select this when failure_history shows the following patterns:

- Other intervention prompts have been tried without success
- The same error occurs no matter what is tried
- The root cause cannot be identified

## Ask Yourself

1. **Are you truly stuck?**
   - Are there any untried approaches?
   - Is there any overlooked information?

2. **What should you ask the user?**
   - Have you prepared specific questions?
   - Can you explain the situation accurately?

## Report Template for User

Report to the user in the following format:

---

**Status Report**

Verification has failed {failure_count} times. The following attempts did not resolve the issue:

1. [Attempt 1]
2. [Attempt 2]
3. [Attempt 3]

**Error Summary**

- [Latest error details]

**Questions**

- [Specific questions or suggestions]

---

## Recommended Actions (by priority)

1. **Revert first** - Undo all changes so far
2. Report the situation using the template above
3. Wait for the user's instructions
4. Request re-confirmation of requirements if needed

**Important:** Before reporting to the user, revert first to ensure you can "return to square one".

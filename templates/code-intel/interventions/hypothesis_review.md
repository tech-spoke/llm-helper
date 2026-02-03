# Hypothesis Review

## When to Select

Select this when failure_history shows the following patterns:

- Error messages differ each time
- Fixes and results don't align
- Root cause guesses keep being wrong

## Ask Yourself

1. **Are you reading the error message accurately?**
   - Are you identifying the root cause, not just the surface symptom?
   - Have you traced the stack trace to the end?

2. **Is your hypothesis wrong?**
   - Have you verified that your root cause guess is correct?
   - Have you considered alternative causes?

3. **Are the assumptions correct?**
   - Could this be an environment issue?
   - Could this be a dependency issue?
   - Could this be a configuration or data issue?

## Recommended Actions (by priority)

1. **Revert first** - Undo changes and start from a clean state
2. Re-read the error message carefully
3. Re-read the related code (verify with Read tool)
4. Report the situation to the user and ask for advice

**Important:** If your hypothesis keeps being wrong, continuing to modify code is futile. Revert and restart the investigation.

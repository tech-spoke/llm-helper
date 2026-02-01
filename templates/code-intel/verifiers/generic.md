# Generic Verification

Verify the implementation above.

<rules>
- Execute the checklist in order
- Report what was executed and the result for each step
- Do not skip any steps
</rules>

## Verification Checklist

<instructions>
Execute the following checklist from top to bottom and report results for each item.

- [ ] **Verify Changes**
  - Review diff of changed files
  - Report: Summary of changes

- [ ] **Check Syntax Errors**
  - Config files: JSON/YAML syntax check (verify by parsing)
  - Code files: Run syntax check command
  - Report: Command executed and result

- [ ] **Functional Verification** (if possible)
  - Verify the changes work as intended
  - Report: Verification method and result
</instructions>

## Result Reporting

### On Success
Report "Verification successful" and proceed to next phase

### On Failure
Report "Verification failed" and include:
- What differs from expectation
- Problem location
- Specific details observed

Then fix the issue and re-verify

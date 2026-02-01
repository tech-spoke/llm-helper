# Backend Verification

Verify the implementation above.

<rules>
- Execute the checklist in order
- Report the command/tool executed and result for each step
- Do not skip any steps
</rules>

## Verification Checklist

<instructions>
Execute the following checklist from top to bottom and report results for each item.

- [ ] **Identify Test Files**
  - Identify test files related to the changes
  - Report: Test file names identified

- [ ] **Run Tests**
  - Use pytest / npm test / php artisan test / other test runners
  - Report: Command executed and output

- [ ] **Verify Test Results**
  - Confirm all tests pass
  - Report: Pass/fail count, failed test names if any

- [ ] **API Verification** (only if API was modified)
  - Call API using curl or appropriate tool
  - Report: Request and response
</instructions>

## Result Reporting

### On Success
Report "Verification successful" and proceed to next phase

### On Failure
Report "Verification failed" and include:
- Failed test name
- Error type and content (e.g., AssertionError: expected 200 but got 401)
- Related code location (e.g., validate_token function in auth.py)

Then fix the issue and re-verify

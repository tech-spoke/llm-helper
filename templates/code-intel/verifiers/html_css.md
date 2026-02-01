# HTML/CSS Verification

Verify the implementation above.

<rules>
- Execute the checklist in order
- Report the tool executed and result for each step
- Do not skip any steps
</rules>

## Verification Checklist

<instructions>
Execute the following checklist from top to bottom and report results for each item.

- [ ] **Run Build** (only if required)
  - Run build if the project requires it
  - Report: Command executed and result

- [ ] **Open Page with Playwright**
  - Navigate to appropriate URL using `browser_navigate`
  - Report: URL accessed

- [ ] **Force Reload**
  - Execute `await page.reload({ waitUntil: 'networkidle' })` using `browser_run_code`
  - Report: Execution result

- [ ] **Verify Changes**
  - Get CSS values using `browser_evaluate` or `browser_run_code`
  - Report: Retrieved values (e.g., color = rgb(0, 0, 255))

- [ ] **Take Screenshot** (if needed)
  - Capture the changed area using `browser_take_screenshot`
</instructions>

## Result Reporting

### On Success
Report "Verification successful" and proceed to next phase

### On Failure
Report "Verification failed" and include:
- What differs from expectation (e.g., logo not displayed, color still #ff0000)
- Problem location (e.g., inside header element, .btn-primary class)
- Specific values observed (e.g., actual color is #333333, element not found)

Then fix the issue and re-verify

# Garbage Detection

Review changes in the PRE_COMMIT phase and detect unnecessary code.

## Criteria

For each changed file, decide whether to **keep** or **discard**.

### Discard

1. **Debug output**
   - Debug statements like `console.log()`, `print()`, `var_dump()`, `dd()`
   - Temporary logging added for debugging purposes

2. **Commented-out code**
   - Code that was commented out instead of deleted
   - Old code kept "just in case"

3. **Unrequested files**
   - Test files not requested by the user
   - Changes to unrelated files

4. **Temporary hacks**
   - Workarounds with TODO comments
   - Dirty code written with the assumption of "fixing later"

5. **Unrelated changes**
   - Format-only changes (when not requested)
   - Adding/removing blank lines only
   - Import order changes only

### Keep

1. **Implementation code for the requested feature**
2. **Necessary validation and error handling**
3. **Related type definitions and interface changes**
4. **Requested tests**
5. **Necessary configuration file changes**

## Notes

- **When in doubt, keep** - The risk of removing necessary code is greater
- **Read the diff in context** - Understand the purpose of each change
- **Check the user's request** - Compare against the original request

## Output Format

```json
{
  "reviewed_files": [
    {"path": "file.py", "decision": "keep"},
    {"path": "debug.log", "decision": "discard", "reason": "Debug output file"}
  ]
}
```

**reason is required only for discard decisions**

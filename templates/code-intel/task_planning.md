# Task Planning Guide

- Create tasks for all modification targets identified during exploration phase
- The purpose is to prevent implementation omissions; granularity is flexible
- Do not include post-implementation verification (tests, etc.) in tasks - these are handled separately in POST_IMPL_VERIFY
- Identify dependencies between tasks
- Order by dependencies (independent tasks first)
- Note risk level (High/Medium/Low) for tasks requiring extra caution

## Checklist Requirements

Each task MUST include a `checklist` of specific implementation items.

### How to Write Checklist Items

**Good examples** (concrete, verifiable):
- "Add login() method to auth.py"
- "Implement validate_password() in UserService class"
- "Add new authentication config section to config.yml"
- "Implement /login endpoint in AuthController"

**Bad examples** (vague, unverifiable):
- "Implement authentication" (which file? what specifically?)
- "Login feature" (method? UI? API?)
- "Security fix" (what exactly?)

### Checklist Item Format

```
[verb] [specific change] in/to [file/class/module]
```

Examples:
- Add `login()` method to `auth.py`
- Add `is_active` field to `UserModel`
- Implement `/logout` endpoint in `routes/auth.ts`
- Add hover styles for `.btn-primary` in `styles.css`

### Deriving Checklist from EXPLORATION

Include all modification targets identified in EXPLORATION in the checklist:

```
EXPLORATION findings:
- auth.py: needs authentication logic
- models/user.py: needs is_active field
- routes/auth.ts: needs /login, /logout endpoints
- tests/: needs test files

=> Convert to checklist:

Task: Implement authentication feature
checklist:
- Add login() method to auth.py
- Add logout() method to auth.py
- Add is_active field to models/user.py
- Implement /login endpoint in routes/auth.ts
- Implement /logout endpoint in routes/auth.ts
```

### When Completing a Task

- `done`: Provide evidence as file:line reference (e.g., "auth.py:42-58")
- `skipped`: Provide reason (min 10 characters) explaining why not needed

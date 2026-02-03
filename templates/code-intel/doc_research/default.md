# Document Research Prompt

## Task
**User Request:** {{task_description}}

## Target Documents
{{docs_path}}

## Instructions
1. Use Glob to list and understand the structure under docs_path
2. Respect the include/exclude settings from `document_search` in context.yml
3. Read only highly relevant files, focusing on necessary sections
4. Extract rules to follow for this modification/investigation and sections the main agent should read
5. Focus on reference locations (file:line / file:line-range) in the output

## Output Format

### rule_refs
Reference locations for rules to follow:
- [Rule summary]: path:line

### must_read
Sections the main agent must read:
- [Purpose/Reason]: path:line-range

### dependencies
Dependencies/impacts to consider:
- [Target]: reason

### warnings
Caveats/pitfalls:
- [Content]: basis or reason

## Guidelines
- Read the minimum necessary (no full file reads)
- Always specify references in file:line format
- Prioritize task-specific rules over general guidelines
- If none found, explicitly state "No applicable rules" or "No required reading"
- Priority order:
  1. Architecture constraints
  2. Data/schema requirements
  3. Naming/placement conventions
  4. Integration patterns
  5. Security requirements

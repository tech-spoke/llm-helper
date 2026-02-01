# Structure Review

## When to Select

Select this when failure_history shows the following patterns:

- Repeatedly changing layout adjustments (margin, padding, position)
- Tweaking the same CSS property multiple times
- Continuously adding position: absolute or z-index
- Attempting to use !important

## Ask Yourself

1. **Are you trying to solve a structural problem with CSS?**
   - Would changing the parent element structure solve this?
   - Is component decomposition needed?

2. **Can Flexbox/Grid solve this?**
   - Grid is simpler than combining multiple margin/padding values
   - Overuse of position: absolute is a sign of structural problems

3. **Are you about to use !important?**
   - !important is admitting defeat
   - Review selector specificity instead

## Recommended Actions (by priority)

1. **Revert first** - Undo the CSS changes and start from a clean state
2. Review the HTML structure
3. Ask the user: "May I restructure the HTML?"

**Important:** Minor tweaks with the same approach are forbidden. Revert and try a different approach.

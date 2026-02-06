# Baseline Command

View or modify the baseline patterns used for anomaly detection.

**Usage**:
- `/baseline` or `/baseline show` - Display current baseline
- `/baseline add <pattern>` - Add an ignore pattern
- `/baseline remove <pattern>` - Remove an ignore pattern
- `/baseline reset` - Reset baseline to defaults (with confirmation)

## Instructions

### Show Baseline (default)

1. Read `memory/baseline.json`
2. Display formatted output:
   ```
   ## Current Baseline

   ### Learned Patterns
   | Resource | Error Signature | Normal Frequency | Last Seen |
   |----------|-----------------|------------------|-----------|
   | cloud_run/api | connection timeout | 2-5/hour | 2024-01-20 |
   | cloud_run/web | 503 upstream | 1-2/hour | 2024-01-19 |

   ### Ignore Patterns
   - `*health check*`
   - `*healthz*`
   - `context deadline exceeded`

   ### Error Frequency Baselines
   | Resource | Error Type | Baseline Count |
   |----------|-----------|----------------|
   | api-server | timeout | 12/hour |
   | api-server | auth_failed | 3/hour |

   ### Metadata
   - **Version**: 1.0
   - **Last updated**: [timestamp]
   - **Total patterns**: X learned, Y ignored
   ```

### Add Ignore Pattern

1. Parse the pattern from arguments
2. Validate it's not empty and not a duplicate
3. Add to `ignore_patterns` array in baseline
4. Update `last_updated` timestamp
5. Save baseline file
6. Confirm: "Added ignore pattern: `<pattern>`"

### Remove Ignore Pattern

1. Parse the pattern from arguments
2. Check if it exists in `ignore_patterns`
3. If found: remove it and save
4. If not found: report "Pattern not found"
5. Confirm: "Removed ignore pattern: `<pattern>`"

### Reset Baseline

1. **Require confirmation**: "This will reset all learned patterns. Type 'confirm' to proceed."
2. If confirmed:
   - Reset `learned_patterns` to empty array
   - Reset `error_frequencies` to empty object
   - Keep default `ignore_patterns`
   - Update `last_updated`
   - Clear `created_issues`
3. Report: "Baseline reset to defaults. Learned patterns cleared."

## Pattern Syntax

Ignore patterns support wildcards:
- `*` matches any characters
- Patterns are case-insensitive
- Examples:
  - `*health*` - matches any message containing "health"
  - `connection * timeout` - matches "connection read timeout", "connection write timeout"
  - `error-code-404` - exact match

## Notes

- Learned patterns are automatically added by the monitoring process
- Manually adding ignore patterns is useful for known-noisy errors
- Resetting baseline means the agent will re-learn all patterns from scratch
- Baseline file is versioned for future compatibility

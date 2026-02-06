# Investigate Command

Deep-dive investigation into a specific resource or error pattern.

**Usage**: `/investigate <resource-name>` or `/investigate <error-pattern>`

## Instructions

1. **Identify the target**:
   - If argument looks like a resource name → focus on that resource
   - If argument looks like an error message → search for that pattern
   - If no argument → ask what to investigate

2. **Gather detailed logs**:
   ```bash
   # For resource investigation
   gcloud logging read "resource.labels.service_name=\"$RESOURCE\" AND severity>=WARNING" \
     --project="${GCP_PROJECT_ID}" \
     --limit=200 \
     --format=json

   # For error pattern investigation
   gcloud logging read "textPayload:\"$PATTERN\" OR jsonPayload.message:\"$PATTERN\"" \
     --project="${GCP_PROJECT_ID}" \
     --limit=200 \
     --format=json
   ```

3. **Analyze the timeline**:
   - When did this error first appear?
   - Is it increasing, decreasing, or steady?
   - Are there correlated errors in other services?

4. **Find the source code**:
   - Look up the resource in `resource-repo-map.yaml`
   - Clone the repository:
     ```bash
     git clone --depth 1 "https://github.com/${REPO}.git" /tmp/investigation
     ```
   - Search for error origins:
     ```bash
     grep -rn "error message pattern" /tmp/investigation/src/
     ```

5. **Trace the error path**:
   - Find where the error is thrown/logged
   - Identify the triggering conditions
   - Look for recent changes (git log)

6. **Report findings**:
   ```
   ## Investigation Report: [Target]

   ### Timeline
   - First occurrence: [timestamp]
   - Frequency: [X per hour]
   - Trend: [increasing/stable/decreasing]

   ### Root Cause Analysis
   **Error origin**: [file:line in source code]
   **Trigger condition**: [what causes this error]
   **Recent changes**: [relevant git commits if any]

   ### Impact Assessment
   - Affected users: [estimate]
   - Data integrity: [any concerns]
   - Service availability: [impact level]

   ### Code Context
   ```[language]
   [relevant code snippet]
   ```

   ### Recommended Fix
   [Suggested solution or mitigation]

   ### Next Steps
   - [ ] [Action item 1]
   - [ ] [Action item 2]
   ```

7. **Clean up**:
   ```bash
   rm -rf /tmp/investigation
   ```

8. **Offer to create issue**:
   - If investigation reveals actionable problem → offer to create GitHub issue
   - Include investigation findings in the issue body

## Notes

- Always clean up cloned repos after investigation
- Sanitize any sensitive data before reporting
- Link to specific commits or lines when possible

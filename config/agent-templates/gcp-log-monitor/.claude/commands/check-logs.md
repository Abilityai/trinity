# Check Logs Command

Perform an immediate scan of GCP logs and report findings.

## Instructions

1. **Query recent logs** from the GCP project:
   ```bash
   gcloud logging read "severity>=ERROR AND timestamp>=\"$(date -u -d '30 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')\"" \
     --project="${GCP_PROJECT_ID}" \
     --format=json \
     --limit=500
   ```

2. **Parse and aggregate** the results:
   - Group errors by resource type and service name
   - Count occurrences of each error signature
   - Note any CRITICAL or EMERGENCY level entries

3. **Compare against baseline** in `memory/baseline.json`:
   - Identify new error types not in `learned_patterns`
   - Calculate frequency spikes vs `error_frequencies`
   - Filter out entries matching `ignore_patterns`

4. **Report findings** in this format:
   ```
   ## Log Scan Results
   **Time range**: [start] to [end]
   **Project**: ${GCP_PROJECT_ID}

   ### Summary
   - Total errors found: X
   - Unique error types: Y
   - Resources affected: Z

   ### Notable Findings
   [List any unusual patterns, spikes, or new errors]

   ### By Resource
   | Resource | Error Count | Status |
   |----------|-------------|--------|
   | service-a | 15 | ⚠️ Elevated |
   | service-b | 3 | ✅ Normal |

   ### Recommendations
   [Any suggested actions or investigations]
   ```

5. **Decide on follow-up**:
   - If critical issues found → Offer to investigate further
   - If issues warrant GitHub issue → Ask for confirmation before creating
   - If all normal → Confirm healthy status

## Notes

- This is a point-in-time scan, not continuous monitoring
- Use `/investigate <resource>` for deeper analysis
- Results are not automatically persisted to baseline

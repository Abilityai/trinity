# Status Command

Report agent status, health metrics, and recent activity.

## Instructions

1. **Check agent health**:
   - Verify GCP credentials are working:
     ```bash
     gcloud auth list --format="value(account)"
     gcloud projects describe "${GCP_PROJECT_ID}" --format="value(name)" 2>/dev/null
     ```
   - Verify GitHub credentials:
     ```bash
     gh auth status 2>&1 | head -5
     ```

2. **Read baseline state** from `memory/baseline.json`:
   - Last check timestamp
   - Last update timestamp
   - Number of learned patterns
   - Number of ignore patterns

3. **Check recent activity**:
   - List recent issues created (from baseline or query GitHub):
     ```bash
     gh issue list --repo "${GITHUB_ISSUES_REPO}" \
       --label "gcp-log-monitor" \
       --limit 5 \
       --json number,title,createdAt
     ```

4. **Report status**:
   ```
   ## GCP Log Monitor Status

   ### Health Checks
   | Check | Status |
   |-------|--------|
   | GCP Authentication | ✅ OK / ❌ Failed |
   | GCP Project Access | ✅ OK / ❌ Failed |
   | GitHub Authentication | ✅ OK / ❌ Failed |
   | Baseline File | ✅ OK / ❌ Missing |

   ### Configuration
   - **GCP Project**: ${GCP_PROJECT_ID}
   - **Issues Repo**: ${GITHUB_ISSUES_REPO}
   - **Schedule**: Every 15 minutes

   ### Baseline Statistics
   - **Learned patterns**: X
   - **Ignore patterns**: Y
   - **Tracked resources**: Z
   - **Last check**: [timestamp or "never"]
   - **Last baseline update**: [timestamp or "never"]

   ### Recent Activity
   | Date | Action | Details |
   |------|--------|---------|
   | [date] | Issue created | #123: Error in service-a |
   | [date] | Pattern learned | New baseline for service-b |

   ### Issues Created (Last 7 Days)
   [List of recent issues or "None"]
   ```

5. **Report any problems**:
   - If credentials are invalid → provide remediation steps
   - If baseline is missing/corrupted → offer to reinitialize
   - If configuration is incomplete → list missing values

## Notes

- This command is read-only and makes no changes
- Use for troubleshooting if scheduled monitoring isn't working
- Credentials are tested with minimal-scope operations

# Recent Issues Command

List GitHub issues recently created by this agent.

## Instructions

1. **Query GitHub for issues**:
   ```bash
   gh issue list --repo "${GITHUB_ISSUES_REPO}" \
     --label "gcp-log-monitor" \
     --state all \
     --limit 20 \
     --json number,title,state,createdAt,closedAt,labels,url
   ```

2. **Also check local baseline** for `created_issues` tracking

3. **Format and display**:
   ```
   ## Recent Issues Created by GCP Log Monitor

   ### Open Issues
   | # | Title | Created | Labels |
   |---|-------|---------|--------|
   | [#123](url) | Error spike in api-server | 2024-01-20 | P1, ops |
   | [#120](url) | New error type in worker | 2024-01-19 | P2, ops |

   ### Recently Closed
   | # | Title | Created | Closed | Resolution |
   |---|-------|---------|--------|------------|
   | [#115](url) | Database timeout errors | 2024-01-15 | 2024-01-16 | Fixed |

   ### Statistics
   - **Total created (30 days)**: X issues
   - **Currently open**: Y issues
   - **Average time to close**: Z hours
   - **Most affected resource**: [resource name]

   ### Issue Trends
   [Brief analysis of issue patterns - are things improving or degrading?]
   ```

4. **Offer follow-up actions**:
   - "Would you like me to investigate any of these issues further?"
   - "Would you like to see details for a specific issue?"

## Notes

- Issues are identified by the `gcp-log-monitor` label
- Shows both open and closed issues for trend analysis
- Local tracking in baseline supplements GitHub query
- Useful for understanding if monitoring is working effectively

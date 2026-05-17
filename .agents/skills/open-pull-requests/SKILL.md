---
name: open-pull-requests
description: List all open pull requests with status, reviewers, and your review state
allowed-tools:
  - Bash
user-invocable: true
---

# Open Pull Requests

## Purpose

List all open pull requests for the current repository in a table showing status, author, reviewers, and whether you've already reviewed or commented.

## Process

### Step 1: Get Repository Info

```bash
gh repo view --json owner,name -q '.owner.login + "/" + .name'
```

### Step 2: Fetch Open PRs with Review Status

```bash
gh pr list --state open --json number,title,author,reviewRequests,reviews,labels,updatedAt,url,isDraft --limit 50
```

### Step 3: Render Results Table

For each open PR, display a markdown table with these columns:

| Column | Source |
|--------|--------|
| **#** | PR number (linked to URL) |
| **Title** | PR title (truncate to 50 chars if needed) |
| **Author** | `author.login` |
| **Status** | Draft / Review Requested / Changes Requested / Approved / Pending |
| **My Review** | Whether the current user (`gh api user -q .login`) has reviewed or commented. Show: "Reviewed", "Commented", "Requested", or "-" |
| **Updated** | Relative time (e.g., "2h ago", "3d ago") |

#### Status Logic

Derive the **Status** column from the PR data:
- If `isDraft` is true → "Draft"
- Check `reviews` array for the latest review per reviewer:
  - If any review has `state: "CHANGES_REQUESTED"` → "Changes Requested"
  - If any review has `state: "APPROVED"` (and none requesting changes) → "Approved"
- If `reviewRequests` is non-empty and no reviews yet → "Review Requested"
- Otherwise → "Pending"

#### My Review Logic

Derive the **My Review** column:
1. Get current user: `gh api user -q .login`
2. Check if user appears in `reviewRequests` → "Requested"
3. Check if user appears in `reviews` with `state: "APPROVED"` or `state: "CHANGES_REQUESTED"` → "Reviewed"
4. Check if user has commented (review with `state: "COMMENTED"`) → "Commented"
5. Otherwise → "-"

### Step 4: Summary

After the table, show a one-line summary:
```
X open PRs: Y need your review, Z you've already reviewed
```

## Outputs

- Markdown table of all open PRs printed to the conversation
- Summary line with action items

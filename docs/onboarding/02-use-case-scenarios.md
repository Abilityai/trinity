# Trinity Use Case Scenarios

This guide presents practical, real-world scenarios showing how to use Trinity to build autonomous AI systems. Each scenario includes the problem, solution architecture, step-by-step implementation, and expected outcomes.

---

## Table of Contents

1. [Scenario 1: Personal Research Assistant](#scenario-1-personal-research-assistant)
2. [Scenario 2: Social Media Content Automation](#scenario-2-social-media-content-automation)
3. [Scenario 3: Email Management & Response System](#scenario-3-email-management--response-system)
4. [Scenario 4: Document Processing Pipeline](#scenario-4-document-processing-pipeline)
5. [Scenario 5: Development Team Assistant](#scenario-5-development-team-assistant)
6. [Scenario 6: Customer Support Triage System](#scenario-6-customer-support-triage-system)

---

## Scenario 1: Personal Research Assistant

### The Problem

You need to stay up-to-date on industry topics, but manually reading articles, watching videos, and summarizing findings takes hours each week. You want an agent that autonomously researches topics and creates digestible summaries.

### The Solution

A single agent that runs on a daily schedule to:
1. Monitor RSS feeds and news sources
2. Read and summarize relevant articles
3. Store summaries in a searchable knowledge base
4. Create a daily digest email

### Implementation

#### Step 1: Create the Agent

```yaml
# Start from the Scout starter template
Name: research-assistant
Template: local:scout
```

#### Step 2: Customize Instructions

Edit the agent's `CLAUDE.md` to include:

```markdown
# Research Assistant

## Purpose
You are a research assistant that monitors topics of interest and creates summaries.

## Topics to Monitor
- Artificial Intelligence
- Deep Learning
- Agent Systems
- Productivity Tools

## Daily Workflow
1. Check RSS feeds for new articles
2. Read and summarize the most relevant ones (top 5)
3. Store summaries in vector memory using MCP tools
4. Create a daily digest markdown file in workspace/digests/

## Summary Format
For each article:
- Title and URL
- 3-4 sentence summary
- Key takeaways (bullet points)
- Relevance score (1-10)
```

#### Step 3: Add MCP Tools

Create `.mcp.json.template` with RSS and web browsing tools:

```json
{
  "mcpServers": {
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"]
    }
  }
}
```

#### Step 4: Create Schedule

In the Trinity UI:
- Go to agent → Schedules
- Create schedule:
  - Name: `Daily Research`
  - Cron: `0 8 * * *` (8 AM daily)
  - Message: `Run your daily research workflow: check feeds, summarize articles, and create digest.`
  - Timezone: Your timezone
  - Enabled: ✅

#### Step 5: Test

Trigger the schedule manually to verify:
1. Agent reads sources
2. Summarizes articles
3. Stores in vector memory
4. Creates digest file

### Expected Outcome

Every morning at 8 AM:
- Agent runs autonomously
- Processes 5-10 articles
- Stores summaries in vector memory
- Creates `workspace/digests/YYYY-MM-DD.md`

You can chat with the agent anytime to ask: "What did you learn about X yesterday?"

### Advanced: Query Your Knowledge Base

Chat with the agent:

```
User: "What have we learned about transformer models in the past week?"

Agent: [Uses vector memory MCP to query summaries, synthesizes response]
```

---

## Scenario 2: Social Media Content Automation

### The Problem

You need to maintain an active social media presence, but creating, scheduling, and posting content daily is time-consuming. You want automated content creation that still maintains quality and brand voice.

### The Solution

A multi-agent system with specialized roles:
- **Content Creator Agent**: Generates post ideas and drafts
- **Scheduler Agent**: Plans posting times and manages calendar
- **Publisher Agent**: Posts to platforms at scheduled times

### Implementation

#### Step 1: Deploy Multi-Agent System

Create a system manifest `content-system.yaml`:

```yaml
name: social-content
description: Automated social media content system

agents:
  scheduler:
    template: local:default
    resources:
      cpu: "1"
      memory: "2g"
    folders:
      expose: true
      consume: true
    schedules:
      - name: weekly-planning
        cron: "0 9 * * 1"  # Monday at 9 AM
        message: "Plan this week's content schedule"
        enabled: true

  creator:
    template: local:default
    resources:
      cpu: "2"
      memory: "4g"
    folders:
      expose: true
      consume: true
    schedules:
      - name: daily-creation
        cron: "0 10 * * *"  # Daily at 10 AM
        message: "Check schedule and create today's content"
        enabled: true

  publisher:
    template: local:default
    resources:
      cpu: "1"
      memory: "2g"
    folders:
      expose: true
      consume: true
    schedules:
      - name: publishing-check
        cron: "*/30 * * * *"  # Every 30 minutes
        message: "Check for content ready to publish"
        enabled: true

permissions:
  preset: full-mesh
```

Deploy via API:

```bash
curl -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"manifest\": \"$(cat content-system.yaml)\"}"
```

#### Step 2: Configure Scheduler Agent

Customize `CLAUDE.md` for scheduler:

```markdown
# Content Scheduler

## Purpose
Plan weekly content schedule and coordinate with creator/publisher.

## Weekly Planning Workflow
1. Review past week's performance (check shared-in/publisher/stats.json)
2. Create content calendar for upcoming week
3. Write schedule.json to shared-out/
4. Notify creator agent of new schedule

## Schedule Format
```json
{
  "week_of": "2025-12-23",
  "posts": [
    {
      "id": "post-001",
      "scheduled_time": "2025-12-23T14:00:00Z",
      "topic": "AI trends",
      "platform": "twitter",
      "status": "pending"
    }
  ]
}
```
```

#### Step 3: Configure Creator Agent

Customize `CLAUDE.md` for creator:

```markdown
# Content Creator

## Purpose
Generate engaging social media posts based on schedule.

## Daily Workflow
1. Read schedule from shared-in/scheduler/schedule.json
2. Find posts assigned to today
3. Generate content for each post
4. Save to shared-out/content/{post-id}.json

## Content Guidelines
- Keep tweets under 280 characters
- Include relevant hashtags
- Maintain professional but friendly tone
- Include call-to-action when appropriate

## Output Format
```json
{
  "post_id": "post-001",
  "content": "Post text here...",
  "hashtags": ["#AI", "#TechTrends"],
  "created_at": "2025-12-23T10:15:00Z",
  "status": "ready"
}
```
```

#### Step 4: Configure Publisher Agent

Add Twitter MCP credentials:

```json
{
  "mcpServers": {
    "twitter": {
      "command": "uvx",
      "args": ["twitter-mcp"],
      "env": {
        "TWITTER_API_KEY": "${TWITTER_API_KEY}",
        "TWITTER_API_SECRET": "${TWITTER_API_SECRET}",
        "TWITTER_ACCESS_TOKEN": "${TWITTER_ACCESS_TOKEN}",
        "TWITTER_ACCESS_SECRET": "${TWITTER_ACCESS_SECRET}"
      }
    }
  }
}
```

Store credentials via Trinity UI → Settings → Credentials.

#### Step 5: Test the System

1. Trigger scheduler manually: Creates weekly plan
2. Trigger creator manually: Generates content
3. Trigger publisher manually: Posts to Twitter (test mode first!)

### Expected Outcome

Weekly automation:
- **Monday 9 AM**: Scheduler plans the week
- **Daily 10 AM**: Creator generates content for scheduled posts
- **Every 30 min**: Publisher checks for posts ready to publish and posts them

### Monitoring

- Dashboard shows all three agents and their connections
- Check shared folders to see data flow
- View Activity tab for each agent to see their work

---

## Scenario 3: Email Management & Response System

### The Problem

Your inbox gets 50+ emails daily. Many are routine questions that could be answered automatically. You want an agent that triages emails, drafts responses, and flags items needing your attention.

### The Solution

Single agent with Gmail access that:
1. Monitors inbox on schedule
2. Categorizes emails (urgent/routine/spam)
3. Drafts responses for routine emails
4. Creates summary report of items needing attention

### Implementation

#### Step 1: Create Agent

```yaml
Name: email-assistant
Template: local:scribe
```

#### Step 2: Set Up Gmail Access

Create `.mcp.json.template`:

```json
{
  "mcpServers": {
    "google": {
      "command": "uvx",
      "args": ["mcp-server-google-gmail"],
      "env": {
        "GOOGLE_CLIENT_ID": "${GOOGLE_CLIENT_ID}",
        "GOOGLE_CLIENT_SECRET": "${GOOGLE_CLIENT_SECRET}",
        "GOOGLE_REFRESH_TOKEN": "${GOOGLE_REFRESH_TOKEN}"
      }
    }
  }
}
```

Follow [Google OAuth Setup Guide](../GOOGLE_OAUTH_SETUP.md) to get credentials.

#### Step 3: Customize Instructions

```markdown
# Email Assistant

## Purpose
Monitor Gmail inbox and handle routine emails automatically.

## Email Categories
1. **Urgent**: Requires immediate attention → Flag for human review
2. **Routine**: Can be answered with templates → Draft response
3. **Info**: FYI only → Mark as read
4. **Spam**: Obvious spam → Archive

## Hourly Workflow
1. Fetch unread emails from last hour
2. For each email:
   - Categorize using the above rules
   - If routine: draft response using appropriate template
   - If urgent: add to summary report
3. Write summary report to workspace/email-reports/YYYY-MM-DD-HH.md
4. Store email patterns in vector memory for learning

## Response Templates

### Meeting Request
"Thanks for reaching out! I'd be happy to meet. Please use my calendar link to find a time that works: [link]"

### Information Request
"Thanks for your question about [topic]. Here's the information you requested: [details]"

### Out of Office
"Thanks for your email. I'm currently [status] and will respond within [timeframe]."

## Don't Auto-Respond To
- Emails from CEO or direct manager
- Emails containing "urgent" or "ASAP"
- First-time senders (unknown email addresses)
```

#### Step 4: Create Schedule

```
Name: Email Check
Cron: 0 * * * *  # Every hour
Message: Run your hourly email triage workflow
Enabled: ✅
```

#### Step 5: Review Workflow

Each hour:
1. Check agent's workspace: `workspace/email-reports/`
2. Review flagged urgent items
3. Approve drafted responses before sending (or configure auto-send for trusted patterns)

### Expected Outcome

- **60% of routine emails** handled automatically
- **Urgent emails** flagged within 1 hour
- **Daily summary** of what the agent handled
- **Learning over time**: Vector memory improves categorization

### Safety Features

Configure the agent to:
- Never auto-send without human approval (until you trust it)
- Save drafts only, you review and send
- Flag uncertain emails for review
- Log all actions for audit trail

---

## Scenario 4: Document Processing Pipeline

### The Problem

Your team receives documents (PDFs, contracts, reports) that need to be:
1. Categorized and filed
2. Key information extracted
3. Summarized for quick review
4. Searchable in a knowledge base

Doing this manually takes hours per document.

### The Solution

Multi-agent pipeline:
- **Intake Agent**: Watches folder for new documents
- **Processor Agent**: Extracts text, categorizes, extracts metadata
- **Summarizer Agent**: Creates executive summaries
- **Indexer Agent**: Stores in vector database for searching

### Implementation

#### Step 1: Deploy System

```yaml
name: doc-pipeline
description: Automated document processing

agents:
  intake:
    template: local:default
    folders:
      expose: true
    schedules:
      - name: check-inbox
        cron: "*/5 * * * *"  # Every 5 minutes
        message: "Check for new documents"

  processor:
    template: local:default
    folders:
      expose: true
      consume: true
    schedules:
      - name: process-queue
        cron: "*/10 * * * *"
        message: "Process queued documents"

  summarizer:
    template: local:default
    folders:
      expose: true
      consume: true
    schedules:
      - name: summarize-queue
        cron: "*/10 * * * *"
        message: "Summarize processed documents"

  indexer:
    template: local:default
    folders:
      consume: true
    schedules:
      - name: index-queue
        cron: "*/10 * * * *"
        message: "Index completed summaries"

permissions:
  preset: orchestrator-workers
```

#### Step 2: Configure Data Flow

**Intake** writes to `shared-out/queue/`:
```json
{
  "document_id": "doc-001",
  "filename": "contract.pdf",
  "received_at": "2025-12-23T10:00:00Z",
  "status": "queued"
}
```

**Processor** reads queue, extracts data, writes to `shared-out/processed/`:
```json
{
  "document_id": "doc-001",
  "category": "contract",
  "parties": ["Company A", "Company B"],
  "date": "2025-12-15",
  "extracted_text": "...",
  "status": "processed"
}
```

**Summarizer** reads processed, creates summaries, writes to `shared-out/summaries/`:
```json
{
  "document_id": "doc-001",
  "summary": "Service agreement between Company A and B...",
  "key_points": ["Term: 12 months", "Value: $50k", "Renewal: automatic"],
  "status": "summarized"
}
```

**Indexer** reads summaries and stores in vector memory for searching.

#### Step 3: Set Up Document Upload

Option 1: Manual upload to agent workspace
```bash
docker cp document.pdf agent-doc-pipeline-intake:/home/developer/workspace/inbox/
```

Option 2: Watch external folder (mount volume in docker-compose)
Option 3: API endpoint for document upload

#### Step 4: Query the System

Chat with indexer agent:
```
User: "Find all contracts from 2025 worth over $25k"

Agent: [Queries vector memory, returns results]
```

### Expected Outcome

Pipeline processes documents in 15-30 minutes:
- **Minute 0**: Document uploaded
- **Minute 5**: Intake detects and queues
- **Minute 10**: Processor extracts and categorizes
- **Minute 20**: Summarizer creates summary
- **Minute 30**: Indexer stores in searchable database

### Scaling

For higher volume:
- Reduce schedule intervals
- Increase agent resources
- Add multiple processor agents for parallel processing

---

## Scenario 5: Development Team Assistant

### The Problem

Your dev team needs help with:
- Monitoring GitHub PRs and issues
- Running tests and reporting results
- Updating documentation when code changes
- Tracking deployment status

### The Solution

Single agent with GitHub and CI/CD access that autonomously monitors and assists.

### Implementation

#### Step 1: Create Agent

```yaml
Name: dev-assistant
Template: local:default
Resources:
  cpu: "2"
  memory: "4g"
```

#### Step 2: Add GitHub Access

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PAT}"
      }
    }
  }
}
```

#### Step 3: Configure Monitoring

```markdown
# Dev Assistant

## Purpose
Monitor GitHub repository and assist development team.

## Daily Workflow (9 AM)
1. Check for new PRs
2. For each PR:
   - Verify tests passed
   - Check for obvious issues
   - Add comment with suggestions if needed
3. Check open issues
4. Identify stale issues (no activity > 30 days)
5. Create daily summary report

## Weekly Workflow (Monday 9 AM)
1. Summarize week's activity
2. Identify documentation gaps
3. Create list of improvement suggestions
4. Generate weekly report

## What to Flag
- PRs without tests
- PRs failing CI
- Issues without labels
- Security vulnerabilities
- Outdated dependencies
```

#### Step 4: Create Schedules

Daily check:
```
Cron: 0 9 * * *
Message: Run daily GitHub monitoring workflow
```

Weekly report:
```
Cron: 0 9 * * 1
Message: Generate weekly team report
```

### Expected Outcome

Daily:
- Team gets report of PR status
- Agents flags issues needing attention
- Documentation gaps identified

Weekly:
- Comprehensive activity summary
- Trend analysis
- Improvement recommendations

---

## Scenario 6: Customer Support Triage System

### The Problem

Support tickets come in via email, chat, and forms. Many are duplicate questions or can be answered with existing knowledge base articles. Support team is overwhelmed.

### The Solution

Multi-agent system:
- **Intake Agent**: Receives tickets from all channels
- **Classifier Agent**: Categories by type and urgency
- **Response Agent**: Drafts responses using knowledge base
- **Escalation Agent**: Routes complex issues to humans

### Implementation

#### Step 1: Deploy System

```yaml
name: support-triage
description: Customer support automation

agents:
  intake:
    template: local:default
    schedules:
      - name: check-channels
        cron: "*/2 * * * *"  # Every 2 minutes
        message: "Check all channels for new tickets"

  classifier:
    template: local:default
    schedules:
      - name: classify-queue
        cron: "*/3 * * * *"
        message: "Classify pending tickets"

  responder:
    template: local:default
    schedules:
      - name: respond-queue
        cron: "*/5 * * * *"
        message: "Draft responses for classified tickets"

  escalation:
    template: local:default
    schedules:
      - name: route-complex
        cron: "*/10 * * * *"
        message: "Route complex issues to humans"

permissions:
  preset: orchestrator-workers
```

#### Step 2: Build Knowledge Base

Upload existing support articles to a knowledge base agent:

```markdown
# Knowledge Base

## Common Issues

### Issue: Login Problems
**Solution**: Reset password via link, clear cookies, check email for 2FA code

### Issue: Payment Failed
**Solution**: Verify card details, check billing address, try different payment method
...
```

Store articles in vector memory for semantic search.

#### Step 3: Configure Classification

```markdown
# Classifier Agent

## Categories
1. **Technical Issue** - Product bug or technical problem
2. **Billing** - Payment, invoices, subscription
3. **Feature Request** - New feature or enhancement
4. **How-To** - Usage questions
5. **Bug Report** - Detailed bug report

## Urgency Levels
- **P0 Critical**: Service down, data loss, security issue
- **P1 High**: Major feature broken, affects multiple users
- **P2 Medium**: Feature partially broken, workaround exists
- **P3 Low**: Minor issue, feature request

## Auto-Resolve Criteria
- Duplicate of closed ticket
- Already answered in knowledge base
- User error with clear solution
```

#### Step 4: Configure Response Agent

```markdown
# Response Agent

## Purpose
Draft helpful, friendly responses using knowledge base.

## Workflow
1. Read classified tickets from shared-in/classifier/
2. For each ticket with classification:
   - Query knowledge base using vector memory
   - Draft response based on matching articles
   - Add response to ticket in shared-out/responses/
3. Flag for human review if confidence < 80%

## Response Template
"Hi [name],

Thanks for contacting us about [issue].

[Solution based on knowledge base]

[Additional helpful info]

Let me know if this resolves your issue!

Best regards,
Support Team"
```

### Expected Outcome

- **50-70% of tickets** auto-resolved with knowledge base articles
- **Response time**: 2-5 minutes for common issues
- **Human agents** focus on complex, high-value issues
- **Learning**: System improves as knowledge base grows

### Metrics to Track

- Resolution rate (auto vs. manual)
- Average response time
- Customer satisfaction (for auto-responses)
- Escalation rate
- Knowledge base coverage

---

## Choosing the Right Scenario

| Your Need | Best Scenario | Agents Needed |
|-----------|---------------|---------------|
| Stay informed | Research Assistant | 1 |
| Social presence | Content Automation | 3 |
| Email overload | Email Management | 1 |
| Document chaos | Document Pipeline | 4 |
| Dev team help | Dev Assistant | 1 |
| Support tickets | Support Triage | 4 |

## Next Steps

Ready to implement one of these scenarios? Here's your path:

1. **Start Simple**: Pick the scenario that matches your need
2. **Single Agent First**: Build and test with one agent
3. **Add Complexity**: Add more agents only when needed
4. **Monitor & Iterate**: Use Trinity dashboard to observe and improve

Continue to **[Common Workflows](03-common-workflows.md)** to learn day-to-day operations.

---

## Contributing Your Scenario

Built something awesome? Share it!

1. Document your use case
2. Share agent templates
3. Submit PR to add to this guide

Community scenarios help everyone build better systems.






# Getting Help

Get instant answers about Trinity through the AI-powered documentation assistant, or connect with the community.

## Trinity Docs Q&A Bot

Ask questions about Trinity and get grounded answers from the documentation.

### CLI Usage

```bash
./scripts/ask-trinity.sh "How do I create an agent?"
./scripts/ask-trinity.sh "What credentials do agents need?"
./scripts/ask-trinity.sh "How do I troubleshoot a stuck agent?"
```

### API Usage

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  "https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity" \
  -d '{"question": "How do I schedule an agent task?"}'
```

**Response:**
```json
{
  "answer": "To schedule an agent task...",
  "state": "SUCCEEDED"
}
```

No authentication required. The bot uses Vertex AI Search with Gemini to generate answers from the onboarding documentation.

## Report a Bug, Request a Feature, or Send Feedback

Use the floating **Help widget** to file a report without leaving Trinity. The widget has four tabs:

- **Ask** -- Query the docs Q&A bot (above).
- **Bug** -- Report something that isn't working.
- **Feature** -- Request a new capability.
- **Feedback** -- Send general feedback privately to the Trinity team.

### How it works

1. Pick the **Bug**, **Feature**, or **Feedback** tab.
2. Enter a title and description. Optionally leave a contact email if you want a reply.
3. Review the **see-before-send** screen, which shows exactly what will be submitted.
4. Confirm to send. Nothing is submitted until you confirm.

### Where reports go

- **Bug** and **Feature** reports are filed as public, labelled GitHub issues in [`abilityai/trinity`](https://github.com/abilityai/trinity/issues). The success screen links to the created issue.
- **Feedback** is private -- it is emailed to the Trinity team only and never made public.
- Every report also emails the team. A contact email you provide rides only the private team email (as the reply-to) and is never written into the public issue.

### What's captured

Each report includes a small diagnostics bundle to speed up triage: app version and git commit, the current page, browser user agent, viewport, OS, and the last few console errors or warnings. Tokens, credentials, emails, and private IP addresses are automatically scrubbed -- both before you review and again on the server -- so secrets aren't exposed in a public issue. You see the already-scrubbed payload on the review screen.

Your self-hosted instance never stores a GitHub token; reports post to an Ability.ai-operated intake endpoint that files the issue on your behalf. Operators can disable the report tabs or repoint the intake URL at build time.

## Other Resources

| Resource | Description |
|----------|-------------|
| [GitHub Issues](https://github.com/abilityai/trinity/issues) | Report bugs and request features |
| [GitHub Discussions](https://github.com/abilityai/trinity/discussions) | Ask questions and share ideas |
| [Demo Video](https://youtu.be/SWpNphnuPpQ) | Watch Trinity in action |
| [API Docs](http://localhost:8000/docs) | Interactive Swagger documentation |

## See Also

- [Overview](overview.md) — What is Trinity
- [Setup](setup.md) — Installation and configuration
- [Quick Start](quick-start.md) — Create your first agent

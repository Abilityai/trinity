---
description: Request Scout to conduct research on a topic
argument-hint: [topic]
---
Request research from Scout on: $ARGUMENTS

If no topic was provided, ask for one and stop.

Use the Trinity MCP server: find the Scout agent via mcp__trinity__list_agents() (named acme-scout when deployed as the acme system, scout when deployed individually), then send it the request with mcp__trinity__chat_with_agent(agent_name=..., message="/research $ARGUMENTS"). Report back what Scout returned.

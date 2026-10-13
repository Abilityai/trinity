# Trinity CLI

Command-line interface for [Trinity](https://github.com/abilityai/trinity), the operating system for the AI-native company.

## Install

```bash
# With pip
pip install trinity-cli

# With pipx (recommended — isolated environment)
pipx install trinity-cli
```

## Quick Start

```bash
# Connect to your Trinity instance
trinity init

# List your agents
trinity agents list

# Chat with an agent
trinity chat my-agent "Hello, what can you do?"

# Check fleet health
trinity health fleet
```

## Multi-Instance Profiles

Manage multiple Trinity instances (local dev, staging, production):

```bash
# First instance (created during init)
trinity init

# Add another instance
trinity init --profile production

# Switch between instances
trinity profile use production
trinity profile list
```

## Commands

| Command | Description |
|---------|-------------|
| `trinity init` | Connect to a Trinity instance |
| `trinity login` | Re-authenticate with stored instance |
| `trinity agents list` | List all agents |
| `trinity agents create <name>` | Create a new agent |
| `trinity agents start <name>` | Start an agent |
| `trinity agents stop <name>` | Stop an agent |
| `trinity chat <agent> "msg"` | Chat with an agent |
| `trinity history <agent>` | View chat history |
| `trinity logs <agent>` | View agent logs |
| `trinity health fleet` | Fleet health overview |
| `trinity health agent <name>` | Single agent health |
| `trinity skills list` | Browse skill library |
| `trinity schedules list <agent>` | View agent schedules |
| `trinity profile list` | List configured profiles |
| `trinity profile use <name>` | Switch active profile |

## Output Formats

```bash
# Table (default, human-readable)
trinity agents list

# JSON (for piping/scripting)
trinity agents list --format json
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TRINITY_URL` | Override instance URL |
| `TRINITY_API_KEY` | Override auth token |
| `TRINITY_PROFILE` | Override active profile |

## Documentation

- [Full CLI docs](https://github.com/abilityai/trinity/blob/main/docs/CLI.md)
- [Trinity Platform](https://github.com/abilityai/trinity)

## License

Apache-2.0

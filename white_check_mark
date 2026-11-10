# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| Latest  | :white_check_mark: |

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Email security@ability.ai with:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will respond within 48 hours and work with you on a fix.

## Security Design

Trinity is designed with security-first principles:

- **Isolated containers** — Each agent runs in its own Docker container with resource limits
- **Zero-trust architecture** — All agent actions are verified and logged
- **Complete audit trails** — Every action logged via Vector log aggregation
- **Self-hosted** — Data never leaves your infrastructure
- **Credential isolation** — Redis-backed secrets with hot-reload capability
- **Role-based access** — Authentication required for all operations
- **Ephemeral SSH** — Time-limited terminal access, no permanent exposure
- **Network isolation** — Agents communicate through controlled MCP channels

## Security Best Practices

When deploying Trinity:

1. **Use strong secrets** — Generate `SECRET_KEY` with `openssl rand -hex 32`
2. **Secure credentials** — Store API keys in Redis, never in code or logs
3. **Limit network access** — Use firewall rules to restrict access to Trinity services
4. **Enable HTTPS** — Use a reverse proxy (nginx, Caddy) with TLS in production
5. **Regular updates** — Keep Docker images and dependencies updated
6. **Audit logs** — Monitor Vector logs for unusual agent activity

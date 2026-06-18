# Tailnet deployment

Trinity can be launched in a Tailscale-friendly mode with the `--tailnet` flag. This mode keeps internal service ports bound to localhost, exposes the MCP server on a stable host port for Tailscale peers, rewrites browser-facing URLs to the tailnet HTTPS hostname, and configures `tailscale serve` for the frontend.

## Usage

```bash
./quickstart.sh --tailnet
./install.sh --tailnet
./scripts/deploy/start.sh --tailnet
```

If automatic hostname detection is unavailable, provide the hostname explicitly:

```bash
./quickstart.sh --tailnet --tailnet-hostname my-host.tailnet-name.ts.net
```

The same `--tailnet-hostname` option is accepted by `install.sh` and `scripts/deploy/start.sh`.

## What tailnet mode does

- Detects the Tailscale DNS name from `tailscale status --json` when available.
- Generates `docker-compose.override.yml` in the working tree.
- Binds most host ports to `127.0.0.1` so they are not exposed beyond the host.
- Binds the MCP server to `0.0.0.0:18081` so Tailscale peers can reach it.
- Detects the Docker socket group ID and adds it to the backend service with `group_add`.
- Rewrites `.env` values:
  - `FRONTEND_URL=https://<tailnet-hostname>`
  - `PUBLIC_CHAT_URL=https://<tailnet-hostname>/chat`
  - `EXTRA_CORS_ORIGINS=https://<tailnet-hostname>`
- Runs `tailscale serve --bg https / proxy http://localhost:18080` for the frontend.
- Prints a tailnet URL and MCP configuration snippet.

## Port remapping

- Backend API: `127.0.0.1:18000 -> 8000`
- Frontend: `127.0.0.1:18080 -> 80`
- MCP server: `0.0.0.0:18081 -> 8080`
- Redis: `127.0.0.1:16379 -> 6379`
- Vector: `127.0.0.1:18686 -> 8686`
- OpenTelemetry gRPC: `127.0.0.1:14317 -> 4317`
- OpenTelemetry HTTP: `127.0.0.1:14318 -> 4318`
- Prometheus: `127.0.0.1:18889 -> 8889`
- Health check: `127.0.0.1:11313 -> 13133`

## Requirements

- Tailscale installed and logged in for automatic hostname detection and Serve configuration.
- Docker and Docker Compose v2.
- Free tailnet-mode host ports listed above.

If Tailscale is not running, setup prints a warning and still generates local deployment files when possible. If a required tailnet host port is occupied, setup stops before writing the override so the operator can free the port or change the mapping.

#!/usr/bin/env bash
# Tailnet deployment helpers for Trinity. Source this file to make tailnet functions available.

_TN_PORT_MAP=(
    "8000:18000:127.0.0.1:backend"
    "80:18080:127.0.0.1:frontend"
    "8080:18081:0.0.0.0:mcp-server"
    "6379:16379:127.0.0.1:redis"
    "8686:18686:127.0.0.1:vector"
    "4317:14317:127.0.0.1:otel-collector"
    "4318:14318:127.0.0.1:otel-collector"
    "8889:18889:127.0.0.1:otel-collector"
    "13133:11313:127.0.0.1:otel-collector"
)

_tn_blank() {
    local value="${1:-}"
    value="${value//[[:space:]]/}"
    [[ -z "$value" ]]
}

tn_detect_hostname() {
    local hostname=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --tailnet-hostname) hostname="${2:-}"; shift 2 ;;
            *) shift ;;
        esac
    done
    if [[ -n "$hostname" ]]; then
        _tn_blank "$hostname" && { echo "WARNING: tailnet hostname override is blank." >&2; return 1; }
        printf '%s\n' "$hostname"; return 0
    fi
    if [[ -n "${TN_HOSTNAME:-}" ]]; then
        _tn_blank "$TN_HOSTNAME" && { echo "WARNING: TN_HOSTNAME is blank." >&2; return 1; }
        printf '%s\n' "$TN_HOSTNAME"; return 0
    fi
    command -v tailscale >/dev/null 2>&1 || { echo "WARNING: tailscale not found; cannot detect tailnet hostname." >&2; return 1; }
    local json fqdn
    json=$(tailscale status --json 2>/dev/null) || { echo "WARNING: tailscale status failed." >&2; return 1; }
    fqdn=$(printf '%s' "$json" | grep -o '"DNSName"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"\(.*\)"/\1/')
    fqdn="${fqdn%.}"
    [[ -z "$fqdn" ]] && { echo "WARNING: could not parse DNSName from tailscale status." >&2; return 1; }
    printf '%s\n' "$fqdn"
}

tn_detect_docker_gid() {
    local explicit="${TN_DOCKER_SOCK:-${DOCKER_SOCK:-}}" gid="" candidate
    local candidates=()
    if [[ -n "$explicit" ]]; then candidates+=("$explicit"); else candidates+=("/var/run/docker.sock" "$HOME/.colima/default/docker.sock"); fi
    for candidate in "${candidates[@]}"; do
        [[ -z "$candidate" ]] && continue
        if [[ ! -e "$candidate" ]]; then [[ -n "$explicit" ]] && return 1; continue; fi
        gid=$(stat -c '%g' "$candidate" 2>/dev/null || stat -f '%g' "$candidate" 2>/dev/null || true)
        if [[ "$gid" =~ ^[0-9]+$ ]]; then printf '%s\n' "$gid"; return 0; fi
        [[ -n "$explicit" ]] && return 1
    done
    [[ -n "$explicit" ]] && return 1
    printf '%s\n' "999"
}

tn_check_tailscale() {
    command -v tailscale >/dev/null 2>&1 || { echo "WARNING: tailscale not found. Install tailscale and re-run." >&2; return 1; }
    local json state
    json=$(tailscale status --json 2>/dev/null) || true
    state=$(printf '%s' "$json" | grep -o '"BackendState"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"\(.*\)"/\1/') || true
    case "$state" in
        Running) return 0 ;;
        NeedsLogin) echo "WARNING: tailscale needs login. Run: tailscale login" >&2; return 1 ;;
        Stopped) echo "WARNING: tailscale is stopped. Run: tailscale up" >&2; return 1 ;;
        *) echo "WARNING: tailscale state is '${state:-unknown}'. Run: tailscale status" >&2; return 1 ;;
    esac
}

tn_check_ports() {
    local host_ports=(18000 18080 18081 16379 18686 14317 14318 18889 11313)
    local occupied=() ss_output="" port in_use
    if command -v ss >/dev/null 2>&1; then ss_output=$(ss -tlnH 2>/dev/null) || true; fi
    for port in "${host_ports[@]}"; do
        in_use=0
        if [[ -n "$ss_output" ]]; then echo "$ss_output" | grep -qE ":${port}[[:space:]]|:${port}$" && in_use=1
        elif command -v lsof >/dev/null 2>&1; then lsof -i ":${port}" -sTCP:LISTEN >/dev/null 2>&1 && in_use=1; fi
        [[ "$in_use" -eq 1 ]] && occupied+=("$port")
    done
    if [[ ${#occupied[@]} -gt 0 ]]; then
        echo "ERROR: Tailnet host ports already in use: ${occupied[*]}" >&2
        echo "  Stop the services using these ports before enabling --tailnet, or" >&2
        echo "  adjust the port map in scripts/lib/tailnet.sh." >&2
        return 1
    fi
}

tn_generate_override() {
    local gid=""
    while [[ $# -gt 0 ]]; do
        case "$1" in --docker-gid) gid="${2:-}"; shift 2 ;; *) shift ;; esac
    done
    if [[ -z "$gid" ]]; then
        gid=$(tn_detect_docker_gid) || return 1
    fi
    [[ "$gid" =~ ^[0-9]+$ ]] || return 1

    local base_dir="$PWD"
    if [[ -n "${WORK:-}" ]] && [[ -n "${REPO_ROOT:-}" ]] && [[ "$PWD" = "$REPO_ROOT" ]]; then
        base_dir="$WORK"
    fi
    local outfile="${base_dir}/docker-compose.override.yml"
    local lockdir="${outfile}.lock"
    local waited=0
    until mkdir "$lockdir" 2>/dev/null; do
        sleep 0.05
        waited=$((waited + 1))
        [[ "$waited" -gt 200 ]] && return 1
    done
    trap 'rm -rf "$lockdir"' RETURN

    local tmpfile
    tmpfile=$(mktemp "${outfile}.tmp.XXXXXX") || return 1
    if [[ -f "$outfile" ]]; then cp "$outfile" "${outfile}.bak" || { rm -f "$tmpfile"; return 1; }; fi
    cat > "$tmpfile" << EOF
# Generated by tn_generate_override — do not edit by hand.
# Tailnet mode: binds most ports to 127.0.0.1; MCP remains on 0.0.0.0.
services:
  backend:
    ports: !override
      - "127.0.0.1:18000:8000"
    group_add:
      - "${gid}"
  frontend:
    ports: !override
      - "127.0.0.1:18080:80"
  mcp-server:
    ports: !override
      - "0.0.0.0:18081:8080"
  redis:
    ports: !override
      - "127.0.0.1:16379:6379"
  vector:
    ports: !override
      - "127.0.0.1:18686:8686"
  otel-collector:
    ports: !override
      - "127.0.0.1:14317:4317"
      - "127.0.0.1:14318:4318"
      - "127.0.0.1:18889:8889"
      - "127.0.0.1:11313:13133"
EOF
    mv "$tmpfile" "$outfile"
}

tn_apply_env() {
    local hostname="${1:-}" env_file="${2:-.env}"
    [[ -z "$hostname" ]] || _tn_blank "$hostname" && { echo "ERROR: tn_apply_env requires a hostname argument." >&2; return 1; }
    if [[ ! -f "$env_file" ]] && [[ "$env_file" = ".env" ]] && [[ -n "${WORK:-}" ]] && [[ -f "${WORK}/.env" ]]; then env_file="${WORK}/.env"; fi
    [[ ! -f "$env_file" ]] && { echo "ERROR: tn_apply_env: $env_file not found." >&2; return 1; }
    local base_url="https://${hostname}" tmp saw_frontend=0 saw_chat=0 saw_cors=0 line
    tmp=$(mktemp) || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        case "$line" in
            FRONTEND_URL=*) printf '%s\n' "FRONTEND_URL=${base_url}"; saw_frontend=1 ;;
            PUBLIC_CHAT_URL=*) printf '%s\n' "PUBLIC_CHAT_URL=${base_url}/chat"; saw_chat=1 ;;
            EXTRA_CORS_ORIGINS=*) printf '%s\n' "EXTRA_CORS_ORIGINS=${base_url}"; saw_cors=1 ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$env_file" > "$tmp"
    [[ $saw_frontend -eq 0 ]] && printf '%s\n' "FRONTEND_URL=${base_url}" >> "$tmp"
    [[ $saw_chat -eq 0 ]] && printf '%s\n' "PUBLIC_CHAT_URL=${base_url}/chat" >> "$tmp"
    [[ $saw_cors -eq 0 ]] && printf '%s\n' "EXTRA_CORS_ORIGINS=${base_url}" >> "$tmp"
    mv "$tmp" "$env_file"
}

tn_configure_serve() {
    command -v tailscale >/dev/null 2>&1 || { echo "WARNING: tailscale not found; skipping serve configuration." >&2; return 1; }
    tailscale serve --bg https / proxy http://localhost:18080 2>/dev/null || { echo "WARNING: tailscale serve failed for frontend. Check: tailscale serve status" >&2; return 1; }
    tailscale serve --bg https /mcp proxy http://localhost:18081 2>/dev/null || { echo "WARNING: tailscale serve failed for MCP. Check: tailscale serve status" >&2; return 1; }
}

tn_print_summary() {
    local hostname="$1"
    printf '\n'
    printf '  Tailnet URL:  https://%s\n' "$hostname"
    printf '  MCP Server:   https://%s/mcp\n' "$hostname"
    printf '\n'
    printf '  MCP config snippet (Claude Code):\n'
    printf '  {\n'
    printf '    "url": "https://%s/mcp",\n' "$hostname"
    printf '    "transport": "http"\n'
    printf '  }\n'
    printf '\n'
}


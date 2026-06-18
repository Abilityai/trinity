#!/usr/bin/env bash
# Common helpers for Trinity tailnet bats tests

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TN_LIB="$REPO_ROOT/scripts/lib/tailnet.sh"
export REPO_ROOT TN_LIB

tn_setup_sandbox() {
    WORK="$(mktemp -d)"
    MOCK_BIN="$(mktemp -d)"
    PATH="$MOCK_BIN:$PATH"
    export WORK MOCK_BIN PATH
    unset TN_HOSTNAME TN_DOCKER_SOCK MOCK_SS_OUTPUT 2>/dev/null || true
}

load_tailnet() {
    # shellcheck source=/dev/null
    source "$TN_LIB"
}

make_mock_tailscale() {
    local json_path="$1"
    cat > "$MOCK_BIN/tailscale" << EOF
#!/bin/bash
if [[ "\$*" == *"--json"* ]]; then
    cat "$json_path"
    exit 0
fi
echo "tailscale \$*" >> "$WORK/tailscale_calls"
exit 0
EOF
    chmod +x "$MOCK_BIN/tailscale"
}

make_mock_tailscale_serve() {
    local exit_code="${1:-0}"
    cat > "$MOCK_BIN/tailscale" << EOF
#!/bin/bash
echo "\$*" >> "$WORK/tailscale_args"
exit $exit_code
EOF
    chmod +x "$MOCK_BIN/tailscale"
}

make_mock_stat() {
    local gid="$1"
    cat > "$MOCK_BIN/stat" << EOF
#!/bin/bash
printf '%s\n' "$gid"
EOF
    chmod +x "$MOCK_BIN/stat"
}

make_mock_docker() {
    cat > "$MOCK_BIN/docker" << 'EOF'
#!/bin/bash
case "$*" in
    *"compose version"*) echo "Docker Compose version v2.0.0" ;;
    "info"*|info) exit 0 ;;
    *"images"*) printf '' ;;
    *) exit 0 ;;
esac
EOF
    chmod +x "$MOCK_BIN/docker"
}

make_fake_docker_socket() {
    local path="$1"
    mkdir -p "$(dirname "$path")"
    touch "$path"
}

seed_env() {
    local dir="${1:-$WORK}"
    cat > "$dir/.env" << 'EOF'
FRONTEND_URL=http://localhost
PUBLIC_CHAT_URL=http://localhost/chat
EXTRA_CORS_ORIGINS=http://localhost
SOME_OTHER_KEY=unchanged
EOF
}

seed_status_json() {
    local fqdn="$1"
    local path="${2:-$WORK/status.json}"
    cat > "$path" << EOF
{
  "BackendState": "Running",
  "Self": {
    "DNSName": "$fqdn"
  }
}
EOF
    echo "$path"
}

assert_binding() {
    local file="$1"
    local addr="$2"
    local host_port="$3"
    local container_port="$4"
    grep -q "${addr}:${host_port}:${container_port}" "$file"
}

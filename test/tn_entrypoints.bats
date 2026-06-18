#!/usr/bin/env bats
# AC-1/10: tn_entrypoints

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

@test "library sources cleanly and all required functions are defined" {
    run bash -c "
        source '$REPO_ROOT/scripts/lib/tailnet.sh'
        declare -f tn_detect_hostname >/dev/null
        declare -f tn_detect_docker_gid >/dev/null
        declare -f tn_check_tailscale >/dev/null
        declare -f tn_check_ports >/dev/null
        declare -f tn_generate_override >/dev/null
        declare -f tn_apply_env >/dev/null
        declare -f tn_configure_serve >/dev/null
        declare -f tn_print_summary >/dev/null
        echo ok
    "
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}

@test "quickstart.sh contains --tailnet flag handling" {
    grep -q '\-\-tailnet' "$REPO_ROOT/quickstart.sh"
}

@test "install.sh contains --tailnet flag handling" {
    grep -q '\-\-tailnet' "$REPO_ROOT/install.sh"
}

@test "scripts/deploy/start.sh contains --tailnet flag handling" {
    grep -q '\-\-tailnet' "$REPO_ROOT/scripts/deploy/start.sh"
}

@test "entry points source scripts/lib/tailnet.sh" {
    grep -q 'tailnet\.sh' "$REPO_ROOT/quickstart.sh"
    grep -q 'tailnet\.sh' "$REPO_ROOT/install.sh"
    grep -q 'tailnet\.sh' "$REPO_ROOT/scripts/deploy/start.sh"
}

@test "tn_print_summary outputs https URL and MCP reference" {
    run tn_print_summary "myhost.tail12345.ts.net"
    [ "$status" -eq 0 ]
    [[ "$output" == *"https://myhost.tail12345.ts.net"* ]]
    [[ "$output" == *"mcp"* ]] || [[ "$output" == *"MCP"* ]]
}

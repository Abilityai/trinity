#!/usr/bin/env bats
# AC-11: tn_check_tailscale

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

@test "succeeds when tailscale BackendState is Running" {
    seed_status_json "myhost.ts.net." "$WORK/status.json"
    make_mock_tailscale "$WORK/status.json"
    run tn_check_tailscale
    [ "$status" -eq 0 ]
}

@test "warns and returns nonzero when tailscale is missing" {
    run tn_check_tailscale
    [ "$status" -ne 0 ]
    [[ "$output" == *"WARNING"* ]] || [[ "$stderr" == *"WARNING"* ]]
}

@test "warns when BackendState is NeedsLogin" {
    cat > "$WORK/status.json" << 'EOF'
{"BackendState": "NeedsLogin", "Self": {"DNSName": ""}}
EOF
    make_mock_tailscale "$WORK/status.json"
    run tn_check_tailscale
    [ "$status" -ne 0 ]
}

@test "warns when BackendState is Stopped" {
    cat > "$WORK/status.json" << 'EOF'
{"BackendState": "Stopped", "Self": {"DNSName": ""}}
EOF
    make_mock_tailscale "$WORK/status.json"
    run tn_check_tailscale
    [ "$status" -ne 0 ]
}

@test "does not exit the calling shell on failure" {
    # Run tn_check_tailscale inside a script that checks it doesn't kill the shell
    run bash -c "
        . '$REPO_ROOT/scripts/lib/tailnet.sh'
        tn_check_tailscale || true
        echo 'still_running'
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"still_running"* ]]
}

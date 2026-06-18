#!/usr/bin/env bats
# AC-9/10: tn_configure_serve

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

@test "invokes tailscale serve" {
    make_mock_tailscale_serve 0
    tn_configure_serve
    [ -f "$WORK/tailscale_args" ]
    grep -q "serve" "$WORK/tailscale_args"
}

@test "proxies frontend to port 18080" {
    make_mock_tailscale_serve 0
    tn_configure_serve
    grep -q "18080" "$WORK/tailscale_args"
}

@test "configures MCP path on HTTPS serve" {
    make_mock_tailscale_serve 0
    tn_configure_serve
    grep -q "/mcp" "$WORK/tailscale_args"
    grep -q "18081" "$WORK/tailscale_args"
}

@test "uses https scheme for serve" {
    make_mock_tailscale_serve 0
    tn_configure_serve
    grep -qi "https" "$WORK/tailscale_args"
}

@test "does not crash the calling shell when tailscale serve fails" {
    make_mock_tailscale_serve 1
    run bash -c "
        PATH='$MOCK_BIN:\$PATH'
        source '$REPO_ROOT/scripts/lib/tailnet.sh'
        tn_configure_serve || true
        echo still_alive
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"still_alive"* ]]
}

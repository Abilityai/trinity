#!/usr/bin/env bats
# AC-3: tn_detect_hostname

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

@test "parses FQDN from tailscale status JSON" {
    seed_status_json "myhost.tail12345.ts.net." "$WORK/status.json"
    make_mock_tailscale "$WORK/status.json"
    run tn_detect_hostname
    [ "$status" -eq 0 ]
    [[ "$output" == *"myhost.tail12345.ts.net"* ]]
}

@test "strips trailing dot from FQDN" {
    seed_status_json "myhost.tail12345.ts.net." "$WORK/status.json"
    make_mock_tailscale "$WORK/status.json"
    run tn_detect_hostname
    [ "$status" -eq 0 ]
    [[ "$output" != *"." ]]
}

@test "--tailnet-hostname override wins over JSON" {
    seed_status_json "other.tail12345.ts.net." "$WORK/status.json"
    make_mock_tailscale "$WORK/status.json"
    run tn_detect_hostname --tailnet-hostname myoverride.example.com
    [ "$status" -eq 0 ]
    [ "$output" = "myoverride.example.com" ]
}

@test "TN_HOSTNAME env override wins over JSON" {
    seed_status_json "other.tail12345.ts.net." "$WORK/status.json"
    make_mock_tailscale "$WORK/status.json"
    TN_HOSTNAME="envhost.example.com" run tn_detect_hostname
    [ "$status" -eq 0 ]
    [ "$output" = "envhost.example.com" ]
}

@test "returns nonzero exit when tailscale unavailable" {
    run tn_detect_hostname
    [ "$status" -ne 0 ]
}

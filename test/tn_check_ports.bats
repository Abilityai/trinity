#!/usr/bin/env bats
# AC-6: tn_check_ports

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
    # Default ss mock: all ports free
    cat > "$MOCK_BIN/ss" << 'EOF'
#!/bin/bash
printf '%s\n' "${MOCK_SS_OUTPUT:-}"
EOF
    chmod +x "$MOCK_BIN/ss"
    cat > "$MOCK_BIN/lsof" << 'EOF'
#!/bin/bash
exit 1
EOF
    chmod +x "$MOCK_BIN/lsof"
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

@test "passes when all tailnet host ports are free" {
    run tn_check_ports
    [ "$status" -eq 0 ]
}

@test "fails when a tailnet host port is occupied" {
    MOCK_SS_OUTPUT="tcp   LISTEN   0   128   127.0.0.1:18000   0.0.0.0:*" \
        run tn_check_ports
    [ "$status" -ne 0 ]
}

@test "error message names the occupied port" {
    MOCK_SS_OUTPUT="tcp   LISTEN   0   128   127.0.0.1:18000   0.0.0.0:*" \
        run tn_check_ports
    [ "$status" -ne 0 ]
    [[ "$output" == *"18000"* ]]
}

@test "error message is actionable" {
    MOCK_SS_OUTPUT="tcp   LISTEN   0   128   0.0.0.0:18081   0.0.0.0:*" \
        run tn_check_ports
    [ "$status" -ne 0 ]
    [[ "$output" == *"Stop"* ]] || [[ "$output" == *"stop"* ]] || [[ "$output" == *"before"* ]]
}

@test "checks all ports in the tailnet remap set" {
    # Port 11313 is the last remapped port (health-check); verify it is checked
    MOCK_SS_OUTPUT="tcp   LISTEN   0   128   127.0.0.1:11313   0.0.0.0:*" \
        run tn_check_ports
    [ "$status" -ne 0 ]
    [[ "$output" == *"11313"* ]]
}

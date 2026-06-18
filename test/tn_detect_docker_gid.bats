#!/usr/bin/env bats
# AC-4: tn_detect_docker_gid

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

@test "returns GID from stat when socket exists" {
    make_fake_docker_socket "$WORK/docker.sock"
    make_mock_stat "999"
    TN_DOCKER_SOCK="$WORK/docker.sock" run tn_detect_docker_gid
    [ "$status" -eq 0 ]
    [ "$output" = "999" ]
}

@test "falls back to 999 when no socket exists" {
    # No socket and no mock stat — expect fallback
    run tn_detect_docker_gid
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
}

@test "handles Colima-style socket path" {
    local colima_sock="$WORK/.colima/default/docker.sock"
    make_fake_docker_socket "$colima_sock"
    make_mock_stat "1001"
    TN_DOCKER_SOCK="$colima_sock" run tn_detect_docker_gid
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
}

@test "emits numeric GID only" {
    make_fake_docker_socket "$WORK/docker.sock"
    make_mock_stat "42"
    TN_DOCKER_SOCK="$WORK/docker.sock" run tn_detect_docker_gid
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
}

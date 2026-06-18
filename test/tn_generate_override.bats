#!/usr/bin/env bats
# AC-2/4/7/8/12: tn_generate_override

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

_gen() {
    (cd "$WORK" && tn_generate_override "$@")
}

@test "creates docker-compose.override.yml" {
    _gen
    [ -f "$WORK/docker-compose.override.yml" ]
}

@test "backend binds to 127.0.0.1:18000" {
    _gen
    assert_binding "$WORK/docker-compose.override.yml" "127.0.0.1" "18000" "8000"
}

@test "frontend binds to 127.0.0.1:18080" {
    _gen
    assert_binding "$WORK/docker-compose.override.yml" "127.0.0.1" "18080" "80"
}

@test "mcp-server binds to 0.0.0.0:18081" {
    _gen
    assert_binding "$WORK/docker-compose.override.yml" "0.0.0.0" "18081" "8080"
}

@test "all non-MCP services bind to 127.0.0.1" {
    _gen
    local f="$WORK/docker-compose.override.yml"
    assert_binding "$f" "127.0.0.1" "16379" "6379"
    assert_binding "$f" "127.0.0.1" "18686" "8686"
    assert_binding "$f" "127.0.0.1" "14317" "4317"
    assert_binding "$f" "127.0.0.1" "14318" "4318"
    assert_binding "$f" "127.0.0.1" "18889" "8889"
    assert_binding "$f" "127.0.0.1" "11313" "13133"
}

@test "only mcp-server uses 0.0.0.0 binding" {
    _gen
    local count
    count=$(grep -c "0\.0\.0\.0:" "$WORK/docker-compose.override.yml")
    [ "$count" -eq 1 ]
}

@test "includes group_add with provided GID" {
    _gen --docker-gid 12345
    grep -q "12345" "$WORK/docker-compose.override.yml"
    grep -q "group_add" "$WORK/docker-compose.override.yml"
}

@test "uses !override tag for mcp-server ports" {
    _gen
    grep -q '!override' "$WORK/docker-compose.override.yml"
}

@test "all remapped services use !override so base ports are replaced" {
    _gen
    local count
    count=$(grep -c "ports: !override" "$WORK/docker-compose.override.yml")
    [ "$count" -eq 6 ]
}

@test "backs up existing override file before overwriting" {
    echo "original content" > "$WORK/docker-compose.override.yml"
    _gen
    [ -f "$WORK/docker-compose.override.yml.bak" ]
    grep -q "original content" "$WORK/docker-compose.override.yml.bak"
}

@test "is idempotent across two runs" {
    _gen
    local first
    first=$(cat "$WORK/docker-compose.override.yml")
    _gen
    local second
    second=$(cat "$WORK/docker-compose.override.yml")
    [ "$first" = "$second" ]
}

@test "output contains valid YAML structure" {
    _gen
    local f="$WORK/docker-compose.override.yml"
    grep -q "^services:" "$f"
    grep -q "  backend:" "$f"
    grep -q "  mcp-server:" "$f"
    grep -q "  frontend:" "$f"
}

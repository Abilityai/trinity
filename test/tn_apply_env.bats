#!/usr/bin/env bats
# AC-5: tn_apply_env

setup() {
    load 'helpers/common'
    tn_setup_sandbox
    load_tailnet
    seed_env
}

teardown() {
    rm -rf "$WORK" "$MOCK_BIN"
}

@test "rewrites FRONTEND_URL to https with tailnet hostname" {
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    grep -q "FRONTEND_URL=https://myhost.ts.net" "$WORK/.env"
}

@test "rewrites PUBLIC_CHAT_URL to https with tailnet hostname" {
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    grep -q "PUBLIC_CHAT_URL=https://myhost.ts.net" "$WORK/.env"
}

@test "rewrites EXTRA_CORS_ORIGINS to https with tailnet hostname" {
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    grep -q "EXTRA_CORS_ORIGINS=https://myhost.ts.net" "$WORK/.env"
}

@test "preserves other env keys unchanged" {
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    grep -q "SOME_OTHER_KEY=unchanged" "$WORK/.env"
}

@test "produces no duplicate keys" {
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    local count
    count=$(grep -c "^FRONTEND_URL=" "$WORK/.env")
    [ "$count" -eq 1 ]
}

@test "uses https scheme, never http for tailnet URLs" {
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    ! grep -E "^(FRONTEND_URL|PUBLIC_CHAT_URL|EXTRA_CORS_ORIGINS)=http://" "$WORK/.env"
}

@test "is idempotent across two runs" {
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    local first
    first=$(cat "$WORK/.env")
    tn_apply_env "myhost.ts.net" "$WORK/.env"
    local second
    second=$(cat "$WORK/.env")
    [ "$first" = "$second" ]
}

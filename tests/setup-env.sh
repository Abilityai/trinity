#!/usr/bin/env bash
# Source from run-*.sh — exports the env vars pytest needs from project .env.
# No-op if .env is missing (CI sets env vars directly).
#
# Why these specific vars:
# - TRINITY_TEST_PASSWORD ← ADMIN_PASSWORD: the api_client default ("password")
#   trips the per-account auth rate limiter (5 fails / 900s) and poisons the
#   whole suite with 429s before any test runs.
# - REDIS_BACKEND_PASSWORD: tests/security/test_redis_network_isolation.py
#   needs this; conftest does not auto-load it for the main suite.
# - INTERNAL_API_SECRET / SECRET_KEY: used by /api/internal/* tests for
#   scheduler/agent-server callback authentication.

DOTENV="$(dirname "$0")/../.env"
if [ -f "$DOTENV" ]; then
    ADMIN_PW="$(grep ^ADMIN_PASSWORD "$DOTENV" | cut -d= -f2-)"
    [ -n "$ADMIN_PW" ] && export TRINITY_TEST_PASSWORD="${TRINITY_TEST_PASSWORD:-$ADMIN_PW}"

    for v in REDIS_BACKEND_PASSWORD INTERNAL_API_SECRET SECRET_KEY; do
        val="$(grep "^$v=" "$DOTENV" | cut -d= -f2-)"
        # Indirect expansion via eval (portable across bash + zsh; ${!v} is bash-only).
        eval "current=\${$v-}"
        [ -n "$val" ] && [ -z "$current" ] && export "$v=$val"
    done
fi

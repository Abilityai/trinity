#!/bin/bash
# Trinity Quick Start — an alias for scripts/deploy/start.sh.
#
# Usage:
#   ./quickstart.sh              — same as ./scripts/deploy/start.sh
#   ./quickstart.sh --defaults   — same as ./scripts/deploy/start.sh --unattended
#   ./quickstart.sh --hosted     — every other flag reaches start.sh verbatim
#
# #2528: this used to be a second installer — its own .env seeding, its own
# `.env` reader, its own bare `docker compose up -d`. A second script that
# brings the stack up is a second place every guard has to be re-implemented,
# and the one that mattered never was: start.sh has refused to start the dev
# stack over a production bind mount since #2280, while this file predated the
# guard and walked straight past it — booting docker-compose.yml's empty
# `trinity-data` volume on a host whose real database sat in TRINITY_DATA_PATH,
# seeding a demo workspace into the wrong store, and reporting healthy. It had
# also never learned the Redis passwords #589 made mandatory, so it only worked
# on a host whose .env was already complete. HOST-006 forbids exactly this
# shape ("one install path, not a second script"); this file now honours it.
#
# `--defaults` was this script's own spelling of "don't prompt"; start.sh's is
# `--unattended`, so it is translated. Nothing else is interpreted here.

set -e

cd "$(dirname "$0")"

args=()
for arg in "$@"; do
    case "$arg" in
        --defaults) args+=(--unattended) ;;
        *)          args+=("$arg") ;;
    esac
done

exec ./scripts/deploy/start.sh "${args[@]}"

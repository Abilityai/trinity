#!/bin/bash

set -e

cd "$(dirname "$0")/../.."

echo "====================================="
echo "Trinity Agent Platform - Stopping"
echo "====================================="
echo ""

# #2280: which compose file the running stack came from, read from the stack
# itself rather than guessed. A hosted install runs `docker-compose.hosted.yml`
# by explicit `-f`, which disables compose's default file merge — so a bare
# `docker compose` in this directory loads the DEV file instead. Same project
# name, so it acts on the same containers, but it knows nothing about
# `cloudflared`, which the dev file does not define: the tunnel keeps running
# and the instance stays publicly reachable after this script has printed
# "All services stopped". That is exactly the hazard start.sh's
# `persist_compose_profile` was added to close, re-entering through the one
# entry point that change did not touch.
#
# The label is compose's own record of the files that created the project, so
# it cannot drift from reality the way a marker file or a heuristic ("is there
# a trinity.db at the bind path?") can. Missing container or missing label
# (nothing running, or a pre-#2280 stack) → the dev default, which is what this
# script has always done.
COMPOSE_FILES=()
HOSTED=0
_config_files=$(docker inspect --format \
    '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' \
    trinity-backend 2>/dev/null || true)
case "$_config_files" in
    *docker-compose.hosted.yml*)
        COMPOSE_FILES=(-f docker-compose.hosted.yml)
        HOSTED=1
        echo "Hosted install detected (docker-compose.hosted.yml)."
        ;;
esac

# `stop`, not `down`. `down` removes the platform containers and tears down
# `trinity-agent-network` — which every agent container is attached to — and it
# is the command start.sh's own closing summary tells operators NOT to run, in
# both the dev and the hosted branch. A script named `stop.sh` running the
# forbidden verb was a standing contradiction; `stop` is the reversible
# operation the summary documents, and `start.sh` brings the same containers
# back up.
docker compose "${COMPOSE_FILES[@]}" stop

echo ""
echo "✅ All services stopped"
echo ""
if [ "$HOSTED" = "1" ]; then
    echo "Start again with:  ./scripts/deploy/start.sh --hosted"
else
    echo "Start again with:  ./scripts/deploy/start.sh"
fi
echo ""

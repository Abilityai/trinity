#!/bin/bash
# Is the web container's PROXY-protocol listener live on this loopback port?
# (trinity-enterprise#873)
#
#   probe-proxy-listener.sh <port>     exit 0 = answered 200, anything else = no
#
# start.sh asks this before pointing Caddy at the protected listener. Pointing
# Caddy at a port that does not speak PROXY protocol — an older web image, a
# stack still starting, a port something else holds — takes the site down, so
# the answer must come from the real thing: a PROXY protocol v2 header (TCP over
# IPv4, 127.0.0.1 -> 127.0.0.1) followed by `GET /`, and an HTTP 200 back.
# An ordinary HTTP listener answers that byte sequence with a 400 or closes;
# nothing listening fails the connect. Bounded: the whole probe gives up after
# PROBE_TIMEOUT seconds (default 5).
#
# Pure bash: /dev/tcp and printf, no Python or netcat on the host.
set -u

port="${1:-}"
case "$port" in
    ''|*[!0-9]*) echo "probe-proxy-listener: port must be a number, got '${port}'" >&2; exit 2 ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo "probe-proxy-listener: port out of range: ${port}" >&2
    exit 2
fi
limit="${PROBE_TIMEOUT:-5}"

probe() {
    local status
    exec 3<>"/dev/tcp/127.0.0.1/$1" || return 1
    # PROXY v2: 12-byte signature, version 2 + PROXY command (0x21), TCP over
    # IPv4 (0x11), 12 address bytes: src 127.0.0.1, dst 127.0.0.1, src port
    # 0xC000 (49152), dst port 0x0050 (80). Then one plain HTTP/1.1 request.
    printf '\015\012\015\012\000\015\012\121\125\111\124\012\041\021\000\014\177\000\000\001\177\000\000\001\300\000\000\120' >&3
    printf 'GET / HTTP/1.1\r\nHost: localhost\r\nUser-Agent: trinity-proxy-probe\r\nConnection: close\r\n\r\n' >&3
    IFS= read -r -t "$2" status <&3 || return 1
    exec 3<&- 3>&-
    case "$status" in
        "HTTP/1.1 200 "*|"HTTP/1.0 200 "*) return 0 ;;
        *) return 1 ;;
    esac
}

if command -v timeout >/dev/null 2>&1; then
    # The connect itself has no timeout in bash; `timeout` bounds all of it.
    export -f probe
    timeout "$limit" bash -c 'probe "$1" "$2"' _ "$port" "$limit"
else
    probe "$port" "$limit"
fi

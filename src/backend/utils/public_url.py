"""The externally reachable origin for a URL Trinity hands to an outside caller
(abilityai/trinity#3215).

Why this is not `str(request.base_url)`. The x402 402 body carries a
`resource.url` that a buyer's token is minted and verified against, and the
facilitator compares origin+path — so an origin that is wrong by one scheme
character mints a token for a URL the client never calls. `request.base_url`
gets the scheme wrong on a standard Trinity deployment for two independent
reasons, and the second is why this helper reads the raw header itself:

1. `docker-compose.prod.yml` / `docker-compose.hosted.yml` override the image's
   `command:`, dropping the Dockerfile CMD's `--proxy-headers
   --forwarded-allow-ips=*`. Without that, uvicorn does not trust `X-Forwarded-*`
   from a non-loopback proxy at all, so `request.url.scheme` stays `http` behind
   ANY proxy — whether or not the frontend nginx is in the path. Restoring those
   flags is a trust change (it re-prices `request.client.host` for every per-IP
   limiter, and agents on the agent network reach `backend:8000` directly), so it
   is deliberately out of scope and this helper does not depend on it.
2. The frontend nginx forwards `X-Forwarded-Proto $scheme`, and `$scheme` is
   `http` on that hop because TLS terminates upstream — so it clobbers the
   upstream `https`. Fixed in `nginx.conf` alongside this helper.

The rules, in order:

* **A configured public URL wins, but only for the host the caller actually
  used.** The operator's declared origin (Settings `public_chat_url` →
  `PUBLIC_CHAT_URL` → `FRONTEND_URL`) is the one external consumers reach
  Trinity through. It is applied only when its host equals the request host (or
  there is no request host) because a caller that reached Trinity on a private
  or alternate host must not be sent to a public host whose tunnel may not
  route that path.
* **Otherwise the request's own host, with the scheme upgraded — never
  downgraded — by `X-Forwarded-Proto: https`.** Upgrade-only because the header
  is caller-controlled: a client that forges it changes only the origin of its
  own 402 (and of the verify built from the same helper), and can never change
  the host.

**One exception, `configured_wins=True`: the A2A agent card** (review I3). A
402 is minted FOR the caller that is holding it, so its origin must be the one
that caller used. A card is the opposite kind of document: whoever fetched it
publishes it to buyers elsewhere, so it must advertise the operator's declared
origin whatever host it was read on. The `get_agent_a2a_card` MCP tool proxies
the card route from `backend:8000`, and a card naming that host is unusable to
every external buyer — which is also the behaviour the card had before #3215.
The fallback when nothing is configured is the SAME request-host rule, not a
second copy of it, so the two surfaces can only differ where an origin was
actually declared.
"""

def _host_of(origin: str) -> str:
    """The netloc of an origin string, lowercased; "" if it has none."""
    if not origin:
        return ""
    rest = origin.split("://", 1)[-1]
    return rest.split("/", 1)[0].strip().lower()


def public_base_url(request, *, configured: str = "",
                    frontend_url: str = "",
                    configured_wins: bool = False) -> str:
    """The origin (`scheme://host[:port]`, no trailing slash) to put in a URL
    handed to an external caller.

    `configured` is the operator's declared public origin (Settings row then
    env); `frontend_url` is the second-best self-hosted fallback. Both are
    passed in rather than read here so `utils/` keeps importing nothing from
    `services/` (Invariant #1) and this stays testable without a DB.

    `configured_wins` is for a DECLARED document rather than a minted one — the
    A2A agent card, and only it (see the module docstring). Default False keeps
    every payment door same-host.

    Returns "" only when there is neither a configured origin nor a usable
    request host — the caller then omits the URL rather than emitting a broken
    one.
    """
    declared = (configured or frontend_url or "").rstrip("/")
    if declared and configured_wins:
        return declared

    request_host = ""
    request_scheme = ""
    url = getattr(request, "url", None) if request is not None else None
    if url is not None:
        request_host = (getattr(url, "netloc", "") or "").strip()
        request_scheme = (getattr(url, "scheme", "") or "").strip().lower()

    if declared and (not request_host
                     or _host_of(declared) == request_host.lower()):
        return declared

    if not request_host:
        return declared

    # Deliberately the RAW header, not `request.url.scheme`: uvicorn's
    # proxy-header trust is off on the prod/hosted compose command, so the
    # parsed scheme is http behind every proxy there.
    forwarded_proto = ""
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            forwarded_proto = (headers.get("x-forwarded-proto") or "").strip().lower()
        except Exception:  # noqa: BLE001 — a header mapping that misbehaves is not fatal
            forwarded_proto = ""
    # A proxy chain sends a comma-separated list; the client-facing hop is first.
    forwarded_proto = forwarded_proto.split(",")[0].strip()

    scheme = request_scheme or "http"
    if forwarded_proto == "https":
        scheme = "https"  # upgrade only
    return f"{scheme}://{request_host}"

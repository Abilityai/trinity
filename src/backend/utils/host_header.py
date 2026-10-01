"""Reject a Host header that could shift the parsed request URL (#3102).

Starlette rebuilds `request.url` from the Host header, so a Host carrying a
path, query or fragment delimiter makes `request.url.path` differ from the path
the router dispatched. The scope fences read `scope["path"]` and are safe on
their own; this guard is defense in depth for any code that reads `request.url`.

Pure ASGI (not `@app.middleware`) so it also covers WebSocket handshakes.
Empty or missing Host passes: HTTP/1.0 clients and some probes send none.
"""

_FORBIDDEN = frozenset("/?#\\@") | frozenset(chr(c) for c in range(33)) | {"\x7f"}


class HostHeaderGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = next((v for k, v in scope.get("headers") or [] if k == b"host"), b"")
            if any(ch in _FORBIDDEN for ch in host.decode("latin-1")):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Invalid Host header"})
                return
        await self.app(scope, receive, send)

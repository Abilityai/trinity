"""Execute the production lease scripts' Lua TEXT in unit tests (trinity-enterprise#695).

The suite's fakeredis has no Lua (`tests/requirements-test.txt`), and the
earlier emulators re-implemented the compare-and-set in Python while ignoring
the script body: a mutated script (the token compare dropped, `==` flipped,
two ARGV indices swapped) stayed green. This evaluator runs the script text
itself, for the small Lua subset those scripts use. A statement, token or
command outside that subset is REFUSED (`UnsupportedLua`) rather than guessed
at, and so is a chained comparison, whose grouping differs between Lua and
Python. An `if` tests Lua truth (only nil and false are false; 0 and '' are
true):

    if <expr> then <block> [else <block>] end
    return <expr>
    <expr> := redis.call('<cmd>', <expr>, ...) | ARGV[n] | KEYS[n]
              | <integer> | '<string>' | <expr> == <expr> | <expr> ~= <expr>

Redis semantics that matter here: a missing key's GET is false (never equal
to a token), every value is compared as BYTES (so a `decode_responses=False`
client and a str client see the same outcome, trinity#4028), and `SET ... NX`
answers OK or false. Not a test module (no `test_` prefix).

Two ways to put the evaluator behind a client (trinity#4028):

- `LeaseRedis` — a small Redis "server" with one store. Client commands and
  scripts act on the same store, but `race_on_get` fires only inside a
  CLIENT-side GET, never inside a script: Redis runs a script's body with
  nothing interleaved, so a hook that could fire mid-script would test an
  interleaving that cannot happen.
- `with_lua(client)` — gives a fakeredis client (which has no Lua) an
  executing `register_script`, the script's `redis.call`s running against
  that client's own data.
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\s*(redis\.call|ARGV|KEYS|~=|==|\d+|'[^']*'|[\[\](),])")


class UnsupportedLua(ValueError):
    """The script uses a construct this evaluator does not implement."""


def _expr(text: str) -> str:
    """One Lua expression -> the Python expression with the same meaning."""
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise UnsupportedLua(f"unsupported Lua in expression: {text[pos:]!r}")
        tok = m.group(1)
        out.append({"redis.call": "_call", "~=": "!="}.get(tok, tok))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if sum(tok in ("==", "!=") for tok in out) > 1:
        raise UnsupportedLua(f"chained comparison (Lua and Python group it differently): {text!r}")
    return " ".join(out)


def _truthy(value) -> bool:
    """Lua truth: only nil and false are false."""
    return value is not None and value is not False


def transpile(src: str) -> str:
    lines, depth = [], 1
    for raw in src.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("if ") and line.endswith(" then"):
            lines.append("    " * depth + f"if _truthy({_expr(line[3:-5])}):")
            depth += 1
        elif line == "else":
            lines.append("    " * (depth - 1) + "else:")
        elif line == "end":
            depth -= 1
            if depth < 1:
                raise UnsupportedLua("unbalanced 'end'")
        elif line.startswith("return "):
            lines.append("    " * depth + f"return {_expr(line[7:])}")
        else:
            raise UnsupportedLua(f"unsupported Lua statement: {line!r}")
    if depth != 1:
        raise UnsupportedLua("unterminated block")
    return "def _script(_call, KEYS, ARGV):\n" + "\n".join(lines) + "\n    return None\n"


def _b(value) -> bytes:
    """A value as Redis holds it: bytes."""
    if isinstance(value, bytes):
        return value
    return str(value).encode()


class ScriptError(Exception):
    """What Redis raises to the client when a script's command errors."""


def _set_opts(opts) -> tuple:
    """Parse SET's trailing options; only NX and EX are supported."""
    nx, ex, i = False, None, 0
    while i < len(opts):
        opt = _b(opts[i]).upper()
        if opt == b"NX":
            nx = True
        elif opt == b"EX":
            i += 1
            ex = int(opts[i])
            if ex <= 0:
                raise ScriptError("ERR invalid expire time in 'set' command")
        else:
            raise UnsupportedLua(f"unsupported SET option: {opt!r}")
        i += 1
    return nx, ex


class _DictBackend:
    """`redis.call` over a plain dict store (+ TTL map)."""

    def __init__(self, store: dict, ttls: dict, decode: bool = True) -> None:
        self.store, self.ttls, self.decode = store, ttls, decode

    def get(self, key):
        return _b(self.store[key]) if key in self.store else False

    def set(self, key, value, *opts):
        nx, ex = _set_opts(opts)
        if nx and key in self.store:
            return False
        self.store[key] = value.decode() if self.decode else value
        if ex is not None:
            self.ttls[key] = ex
        return {"ok": "OK"}

    def expire(self, key, ttl):
        if key not in self.store:
            return 0
        if int(ttl) <= 0:  # Redis deletes a key given a non-positive TTL
            self.delete(key)
            return 1
        self.ttls[key] = int(ttl)
        return 1

    def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0


class _ClientBackend:
    """`redis.call` over a real (fake)redis client's own commands."""

    def __init__(self, client) -> None:
        self.client = client

    def get(self, key):
        value = self.client.get(key)
        return False if value is None else _b(value)

    def set(self, key, value, *opts):
        nx, ex = _set_opts(opts)
        return {"ok": "OK"} if self.client.set(key, value, nx=nx, ex=ex) else False

    def expire(self, key, ttl):
        return 1 if self.client.expire(key, int(ttl)) else 0

    def delete(self, key):
        return int(self.client.delete(key))


def execute(src: str, backend, keys=None, args=None):
    """Run the script TEXT `src` with its `redis.call`s served by `backend`."""
    def _call(cmd, *a):
        cmd = cmd.lower()
        if cmd == "get":
            return backend.get(a[0])
        if cmd == "set":
            return backend.set(a[0], a[1], *a[2:])
        if cmd == "expire":
            return backend.expire(a[0], a[1])
        if cmd == "del":
            return backend.delete(a[0])
        raise UnsupportedLua(f"unsupported redis.call command: {cmd!r}")

    namespace: dict = {}
    exec(transpile(src), {"__builtins__": {}, "_truthy": _truthy}, namespace)  # noqa: S102 — our own test transpiler
    key_names = [None, *(k.decode() if isinstance(k, bytes) else str(k) for k in (keys or []))]
    argv = [None, *(_b(v) for v in (args or []))]
    return namespace["_script"](_call, key_names, argv)


def run(src: str, store: dict, ttls: dict, keys=None, args=None, decode: bool = True):
    """Run `src` against a plain key/value `store` (+ `ttls`), Redis-style."""
    return execute(src, _DictBackend(store, ttls, decode), keys, args)


def script_kind(src: str) -> str:
    """Name a lease script by what it can do, for command logs."""
    if "'del'" in src:
        return "release"
    if "'set'" in src:
        return "retake"
    return "refresh"


class LeaseRedis:
    """A Redis "server" for lock tests: SET NX EX / GET / EXPIRE / DELETE plus
    an EXECUTING `register_script`, all over ONE store.

    `commands` records client commands as `(cmd, key)` and script runs as
    `("script", kind, key)`, so a test can assert that a path issued no client
    GET at all. `race_on_get` fires once, inside the next CLIENT GET only.
    `fail` makes every command and script raise; `fail_scripts` only scripts.
    The script resolves its target from the `client=` kwarg at call time, so a
    script registered on one instance never runs against another.
    """

    def __init__(self, decode_responses: bool = True) -> None:
        self.store: dict = {}
        self.ttls: dict = {}
        self.commands: list = []
        self.race_on_get = None
        self.fail = False
        self.fail_scripts = False
        self.decode = decode_responses

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis down")

    def register_script(self, src: str):
        kind = script_kind(src)

        def _run(keys=None, args=None, client=None):
            target = client if client is not None else self
            if target.fail or target.fail_scripts:
                raise ConnectionError("redis down")
            target.commands.append(("script", kind, (keys or [None])[0]))
            return run(src, target.store, target.ttls, keys, args, decode=target.decode)

        return _run

    def _value(self, value):
        if self.decode:
            return value.decode() if isinstance(value, bytes) else value
        return _b(value)

    def set(self, key, value, nx=False, ex=None):
        self._check()
        self.commands.append(("set", key))
        if nx and key in self.store:
            return None
        self.store[key] = self._value(value)
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def get(self, key):
        self._check()
        self.commands.append(("get", key))
        value = self.store.get(key)
        if self.race_on_get is not None:
            hook, self.race_on_get = self.race_on_get, None
            hook()
        return value

    def expire(self, key, ttl):
        self._check()
        self.commands.append(("expire", key))
        if key not in self.store:
            return False
        self.ttls[key] = int(ttl)
        return True

    def delete(self, key):
        self._check()
        self.commands.append(("delete", key))
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0

    def exists(self, key):
        self._check()
        return 1 if key in self.store else 0


def with_lua(client):
    """Give a fakeredis `client` an executing `register_script` and return it.

    The script's `redis.call`s run against the client's own data, so fakeredis
    keeps its real data model (`exists`, TTLs) for everything else the test
    asserts. Use `LeaseRedis` instead when a test injects a race: here there is
    no hook, and a script runs to completion before anything else.
    """
    def register_script(src: str):
        def _run(keys=None, args=None, client=None, _owner=client):
            return execute(src, _ClientBackend(client if client is not None else _owner), keys, args)
        return _run

    client.register_script = register_script
    return client


def dict_register_script(store: dict, ttls=None, log=None):
    """An executing `register_script` over a test's own dict `store`, for a
    MagicMock client that models SET/GET/DELETE against that dict. Each script
    run is appended to `log` as `(kind, key)` when a list is given."""
    ttls = {} if ttls is None else ttls

    def register_script(src: str):
        kind = script_kind(src)

        def _run(keys=None, args=None, client=None):
            if log is not None:
                log.append((kind, (keys or [None])[0]))
            return run(src, store, ttls, keys, args)

        return _run

    return register_script

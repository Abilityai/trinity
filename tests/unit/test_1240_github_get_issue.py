"""
`GitHubService.get_issue` — the one read a project task's GitHub ref makes
(abilityai/trinity-enterprise#1240).

The enterprise projects module links a task to a GitHub issue or pull request
and shows its title and state. Trinity only ever reads: one
`GET /repos/{owner}/{repo}/issues/{n}` per item, which also answers for a PR.
These tests drive the real method over an `httpx.MockTransport`:

- the only method sent is GET, to the fixed API base;
- a renamed repo's 301 is followed exactly once, a second hop is an error;
- a redirect off the API origin (scheme, host, port) is refused unsent;
- a non-200 raises `GitHubError` with `status`, `reset` and `rate_limited`;
- an empty PAT sends no `Authorization` (public repos only);
- the token never appears in the error text, even on a network error.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.github_service import GitHubError, GitHubService  # noqa: E402

TOKEN = "ghp_TESTTOKEN1240"
BASE = GitHubService.API_BASE


def _run(coro):
    return asyncio.run(coro)


def _read(handler, pat=TOKEN, owner="octo", repo="hello", number=7):
    seen = []

    def spy(request: httpx.Request):
        seen.append(request)
        return handler(request)

    async def go():
        async with GitHubService.issue_client(transport=httpx.MockTransport(spy)) as client:
            return await GitHubService(pat).get_issue(owner, repo, number, client=client)

    return seen, go


class TestGetIssue:
    def test_200_returns_body_with_one_get(self):
        body = {"number": 7, "title": "Fix it", "state": "open", "html_url": "https://github.com/octo/hello/issues/7"}
        seen, go = _read(lambda r: httpx.Response(200, json=body))
        assert _run(go()) == body
        assert [(r.method, str(r.url)) for r in seen] == [("GET", f"{BASE}/repos/octo/hello/issues/7")]
        assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
        assert seen[0].headers["X-GitHub-Api-Version"] == GitHubService.API_VERSION

    def test_empty_pat_sends_no_authorization(self):
        seen, go = _read(lambda r: httpx.Response(200, json={}), pat="")
        _run(go())
        assert "Authorization" not in seen[0].headers

    def test_follows_exactly_one_redirect(self):
        moved = f"{BASE}/repos/octo/renamed/issues/7"

        def handler(r):
            if r.url.path == "/repos/octo/hello/issues/7":
                return httpx.Response(301, headers={"Location": moved})
            return httpx.Response(200, json={"html_url": "https://github.com/octo/renamed/issues/7"})

        seen, go = _read(handler)
        assert _run(go())["html_url"].endswith("/octo/renamed/issues/7")
        assert [r.method for r in seen] == ["GET", "GET"]

    def test_second_redirect_is_an_error(self):
        def handler(r):
            n = int(r.url.path.rsplit("/", 1)[-1])
            return httpx.Response(301, headers={"Location": f"{BASE}/repos/octo/hello/issues/{n + 1}"})

        seen, go = _read(handler)
        with pytest.raises(GitHubError) as exc:
            _run(go())
        assert exc.value.status is None
        assert len(seen) == 2

    def test_redirect_to_another_origin_is_refused_before_it_is_sent(self):
        """C1: a 301 off the API origin is an error and the second hop is never sent."""
        def handler(r):
            if r.url.host == "api.github.com":
                return httpx.Response(301, headers={"Location": "https://evil.example/repos/octo/hello/issues/7"})
            return httpx.Response(200, json={"title": "leak"})

        seen, go = _read(handler)
        with pytest.raises(GitHubError) as exc:
            _run(go())
        assert exc.value.status is None
        assert [str(r.url) for r in seen] == [f"{BASE}/repos/octo/hello/issues/7"]
        assert TOKEN not in str(exc.value)

    @pytest.mark.parametrize("location", [
        "http://api.github.com/repos/octo/hello/issues/7",
        "https://api.github.com:8443/repos/octo/hello/issues/7",
    ])
    def test_redirect_changing_scheme_or_port_is_refused(self, location):
        def handler(r):
            if str(r.url).startswith(BASE + "/"):
                return httpx.Response(301, headers={"Location": location})
            return httpx.Response(200, json={})

        seen, go = _read(handler)
        with pytest.raises(GitHubError):
            _run(go())
        assert len(seen) == 1

    def test_404_raises_with_status(self):
        _, go = _read(lambda r: httpx.Response(404, json={"message": "Not Found"}))
        with pytest.raises(GitHubError) as exc:
            _run(go())
        assert exc.value.status == 404
        assert exc.value.rate_limited is False

    def test_rate_limit_carries_reset(self):
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}
        _, go = _read(lambda r: httpx.Response(403, headers=headers, json={"message": "API rate limit exceeded"}))
        with pytest.raises(GitHubError) as exc:
            _run(go())
        assert exc.value.status == 403
        assert exc.value.rate_limited is True
        assert exc.value.reset == 1893456000

    def test_403_without_exhausted_budget_is_not_rate_limited(self):
        _, go = _read(lambda r: httpx.Response(403, headers={"x-ratelimit-remaining": "4999"}))
        with pytest.raises(GitHubError) as exc:
            _run(go())
        assert exc.value.rate_limited is False
        assert exc.value.reset is None

    def test_429_is_rate_limited(self):
        _, go = _read(lambda r: httpx.Response(429, headers={"retry-after": "60"}))
        with pytest.raises(GitHubError) as exc:
            _run(go())
        assert exc.value.rate_limited is True

    def test_network_error_never_carries_the_token(self):
        def handler(r):
            raise httpx.ConnectError(f"boom {r.headers.get('Authorization')}")

        _, go = _read(handler)
        with pytest.raises(GitHubError) as exc:
            _run(go())
        assert TOKEN not in str(exc.value)
        assert exc.value.status is None

    def test_number_must_be_positive_int(self):
        with pytest.raises(ValueError):
            _run(GitHubService("").get_issue("octo", "hello", 0))

    def test_without_client_uses_issue_client(self, monkeypatch):
        """The default path builds the same bounded client the batch uses."""
        made = []
        real = GitHubService.issue_client

        def spy(cls=None, transport=None):
            made.append(1)
            return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"n": 1})))

        monkeypatch.setattr(GitHubService, "issue_client", staticmethod(spy))
        assert _run(GitHubService("").get_issue("octo", "hello", 1)) == {"n": 1}
        assert made == [1]

    def test_issue_client_bounds(self):
        client = GitHubService.issue_client()
        try:
            assert client.follow_redirects is True
            assert client.max_redirects == 1
            assert client.timeout.read == 5.0
        finally:
            _run(client.aclose())

"""
`GitHubService.list_sub_issues` — the children of one GitHub issue
(abilityai/trinity-enterprise#1163).

An epic's native sub-issues are listed with
`GET /repos/{owner}/{repo}/issues/{n}/sub_issues`. Trinity only reads. These
tests drive the real method over an `httpx.MockTransport`:

- the only method sent is GET, to the fixed API base, 100 per page;
- pages are followed while GitHub sends `Link: rel="next"`, by page number
  (no URL from the response is ever requested), up to `limit` items;
- a non-200 raises `GitHubError` with `status`, `reset` and `rate_limited`,
  the same mapping `get_issue` uses;
- a redirect off the API origin is refused unsent, as for `get_issue`;
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

TOKEN = "ghp_TESTTOKEN1163"
BASE = GitHubService.API_BASE
PATH = "/repos/octo/hello/issues/7/sub_issues"


def _children(start, count):
    return [{"number": n, "title": f"Child {n}", "state": "open",
             "html_url": f"https://github.com/octo/hello/issues/{n}"} for n in range(start, start + count)]


def _list(handler, pat=TOKEN, limit=100, number=7):
    seen = []

    def spy(request: httpx.Request):
        seen.append(request)
        return handler(request)

    async def go():
        async with GitHubService.issue_client(transport=httpx.MockTransport(spy)) as client:
            return await GitHubService(pat).list_sub_issues("octo", "hello", number, client=client, limit=limit)

    return seen, lambda: asyncio.run(go())


def _paged(total, per=100, link=True):
    """A stub listing `total` children, `per` a page, with a next Link while more remain."""
    def handler(r):
        page = int(r.url.params.get("page", "1"))
        items = _children(1 + (page - 1) * per, max(0, min(per, total - (page - 1) * per)))
        headers = {}
        if link and page * per < total:
            headers["Link"] = f'<{BASE}{PATH}?per_page={per}&page={page + 1}>; rel="next"'
        return httpx.Response(200, json=items, headers=headers)
    return handler


class TestListSubIssues:
    def test_one_page_is_one_get_with_the_token(self):
        seen, go = _list(_paged(3))
        out = go()
        assert [c["number"] for c in out] == [1, 2, 3]
        assert [(r.method, r.url.path, r.url.params["per_page"], r.url.params["page"]) for r in seen] == [
            ("GET", PATH, "100", "1")]
        assert str(seen[0].url).startswith(BASE + PATH)
        assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
        assert seen[0].headers["X-GitHub-Api-Version"] == GitHubService.API_VERSION

    def test_empty_pat_sends_no_authorization(self):
        seen, go = _list(_paged(1), pat="")
        go()
        assert "Authorization" not in seen[0].headers

    def test_no_children_is_an_empty_list(self):
        _, go = _list(_paged(0))
        assert go() == []

    def test_follows_next_pages_in_order(self):
        seen, go = _list(_paged(250), limit=1000)
        out = go()
        assert [c["number"] for c in out] == list(range(1, 251))
        assert [r.url.params["page"] for r in seen] == ["1", "2", "3"]
        assert {r.method for r in seen} == {"GET"}

    def test_stops_at_the_limit(self):
        seen, go = _list(_paged(250), limit=101)
        out = go()
        assert len(out) == 101 and out[-1]["number"] == 101
        assert len(seen) == 2

    def test_no_next_link_means_no_second_page(self):
        seen, go = _list(_paged(250, link=False), limit=1000)
        assert len(go()) == 100
        assert len(seen) == 1

    def test_never_requests_a_url_from_the_link_header(self):
        def handler(r):
            page = int(r.url.params.get("page", "1"))
            items = _children(1 + (page - 1) * 100, 100 if page == 1 else 1)
            headers = {"Link": '<https://evil.example/steal?page=2>; rel="next"'} if page == 1 else {}
            return httpx.Response(200, json=items, headers=headers)

        seen, go = _list(handler, limit=1000)
        assert len(go()) == 101
        assert {r.url.host for r in seen} == {"api.github.com"}

    def test_redirect_to_another_origin_is_refused_before_it_is_sent(self):
        def handler(r):
            if r.url.host == "api.github.com":
                return httpx.Response(301, headers={"Location": "https://evil.example/x"})
            return httpx.Response(200, json=[])

        seen, go = _list(handler)
        with pytest.raises(GitHubError) as exc:
            go()
        assert exc.value.status is None
        assert len(seen) == 1
        assert TOKEN not in str(exc.value)

    @pytest.mark.parametrize("status", [401, 403, 404, 410, 500])
    def test_non_200_raises_with_status(self, status):
        _, go = _list(lambda r: httpx.Response(status, json={"message": "no"}))
        with pytest.raises(GitHubError) as exc:
            go()
        assert exc.value.status == status
        assert exc.value.rate_limited is False

    def test_rate_limit_carries_reset(self):
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}
        _, go = _list(lambda r: httpx.Response(403, headers=headers, json={}))
        with pytest.raises(GitHubError) as exc:
            go()
        assert exc.value.rate_limited is True and exc.value.reset == 1893456000

    def test_rate_limit_on_a_later_page_raises(self):
        def handler(r):
            if r.url.params.get("page") == "2":
                return httpx.Response(429, headers={"retry-after": "60"})
            return _paged(250)(r)

        _, go = _list(handler, limit=1000)
        with pytest.raises(GitHubError) as exc:
            go()
        assert exc.value.rate_limited is True

    def test_network_error_never_carries_the_token(self):
        def handler(r):
            raise httpx.ConnectError(f"boom {r.headers.get('Authorization')}")

        _, go = _list(handler)
        with pytest.raises(GitHubError) as exc:
            go()
        assert TOKEN not in str(exc.value)
        assert exc.value.status is None

    def test_a_non_list_body_is_an_error(self):
        _, go = _list(lambda r: httpx.Response(200, json={"message": "odd"}))
        with pytest.raises(GitHubError):
            go()

    @pytest.mark.parametrize("number,limit", [(0, 100), (True, 100), (7, 0)])
    def test_bad_arguments(self, number, limit):
        with pytest.raises(ValueError):
            asyncio.run(GitHubService("").list_sub_issues("octo", "hello", number, limit=limit))

    def test_without_client_uses_issue_client(self, monkeypatch):
        made = []
        real = GitHubService.issue_client

        def spy(cls=None, transport=None):
            made.append(1)
            return real(transport=httpx.MockTransport(_paged(2)))

        monkeypatch.setattr(GitHubService, "issue_client", staticmethod(spy))
        assert len(asyncio.run(GitHubService("").list_sub_issues("octo", "hello", 7))) == 2
        assert made == [1]

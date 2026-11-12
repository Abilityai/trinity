"""
GitHub API Service.

Handles all GitHub API interactions:
- Repository existence checking
- Organization detection
- Repository creation
- Token validation
- Issue / pull request reads (abilityai/trinity-enterprise#1240)
"""
import httpx
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class OwnerType(Enum):
    """Type of GitHub repository owner."""
    USER = "user"
    ORGANIZATION = "org"


@dataclass
class GitHubRepoInfo:
    """Information about a GitHub repository."""
    exists: bool
    full_name: str
    owner: str
    name: str
    owner_type: Optional[OwnerType] = None
    private: Optional[bool] = None
    default_branch: Optional[str] = None


@dataclass
class GitHubCreateResult:
    """Result of creating a GitHub repository."""
    success: bool
    repo_url: Optional[str] = None
    error: Optional[str] = None


class GitHubError(Exception):
    """Base exception for GitHub API errors.

    ``status`` carries the HTTP status when GitHub answered with one (#3725),
    so a caller can tell a token GitHub refused (401/403) from an outage
    without parsing the message. ``None`` for network errors and for raises
    that predate it; ``str(e)`` is unchanged either way.


    ``reset`` and ``rate_limited`` are set by ``get_issue`` (ent#1240): a
    403/429 with an exhausted budget is a rate limit, not a refusal, and
    ``reset`` is GitHub's ``x-ratelimit-reset`` (epoch seconds) when sent.
    """

    def __init__(
        self,
        *args,
        status: Optional[int] = None,
        reset: Optional[int] = None,
        rate_limited: bool = False,
    ):
        super().__init__(*args)
        self.status = status
        self.reset = reset
        self.rate_limited = rate_limited


def github_error_words(error_class: type, status: Optional[int]) -> str:
    """The words an answer gives for a ``GitHubError``: its class, GitHub's
    status when it answered one, and where the rest went.

    Built from the class and the status only. The status tells a refused token
    from an outage; the error's text stays in the log, because a network
    error's text is httpx's, and for a header-unsafe token httpx echoes the
    ``Authorization`` value there (abilityai/trinity#4284).
    """
    note = f", HTTP {status}" if status else ""
    return f"{error_class.__name__}{note} — details in backend logs"


class GitHubService:
    """
    Service for GitHub API interactions.

    Centralizes all GitHub REST API calls.
    """

    API_BASE = os.getenv("TRINITY_GIT_API_BASE", "https://api.github.com")
    API_VERSION = "2022-11-28"

    def __init__(self, pat: str):
        """
        Initialize GitHub service with a Personal Access Token.

        Args:
            pat: GitHub Personal Access Token. Empty string is allowed for
                read-only calls against public repos (no Authorization header
                is sent — an empty Bearer would 401).
        """
        self.pat = pat
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.API_VERSION
        }
        if pat:
            self._headers["Authorization"] = f"Bearer {pat}"

    async def _request(
        self,
        method: str,
        path: str,
        timeout: float = 10.0,
        **kwargs
    ) -> httpx.Response:
        """Make an authenticated request to GitHub API."""
        url = f"{self.API_BASE}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method, url,
                headers=self._headers,
                **kwargs
            )
            return response

    @staticmethod
    def issue_client(transport: Optional[httpx.AsyncBaseTransport] = None) -> httpx.AsyncClient:
        """The bounded client ``get_issue`` reads with: 5 s, one redirect.

        One redirect lets a renamed or transferred repo answer from its new
        home; a second hop is an error. A caller reading many items shares
        one of these across the batch (ent#1240).
        """
        return httpx.AsyncClient(
            timeout=5.0, follow_redirects=True, max_redirects=1, transport=transport,
        )

    async def get_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        client: Optional[httpx.AsyncClient] = None,
    ) -> dict:
        """Read one issue or pull request: ``GET /repos/{owner}/{repo}/issues/{n}``.

        The issues endpoint answers for a PR too, carrying
        ``pull_request.merged_at``. GET only — nothing here writes to GitHub.
        Raises ``GitHubError`` on a non-200 (``status``, ``reset``,
        ``rate_limited``) and on a network error (``status=None``, the
        message never carries httpx's text, which can echo the token, #4284).
        """
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("number must be a positive integer")
        url = f"{self.API_BASE}/repos/{owner}/{repo}/issues/{number}"
        own = client is None
        if own:
            client = self.issue_client()
        try:
            response = await client.get(url, headers=self._headers)
        except httpx.RequestError as e:
            raise GitHubError(f"Network error: {type(e).__name__}") from None
        finally:
            if own:
                await client.aclose()
        if response.status_code == 200:
            return response.json()
        status = response.status_code
        remaining = response.headers.get("x-ratelimit-remaining")
        limited = status == 429 or (
            status == 403 and (remaining == "0" or "retry-after" in response.headers)
        )
        reset_raw = response.headers.get("x-ratelimit-reset")
        reset = int(reset_raw) if reset_raw and reset_raw.isdigit() else None
        raise GitHubError(
            f"GitHub API error: {status}",
            status=status, reset=reset if limited else None, rate_limited=limited,
        )

    async def validate_token(self) -> Tuple[bool, Optional[str]]:
        """
        Validate the PAT and return the authenticated user.

        Returns:
            Tuple of (is_valid, username)
        """
        try:
            response = await self._request("GET", "/user")
            if response.status_code == 200:
                data = response.json()
                return True, data.get("login")
            elif response.status_code == 401:
                return False, None
            else:
                return False, None
        except Exception:
            return False, None

    async def validate_token_detailed(self) -> Tuple[str, Optional[str]]:
        """Validate the PAT, distinguishing an invalid token from an unreachable API.

        ``validate_token`` collapses both a rejected token and a network failure
        to ``(False, None)``, so a caller can only ever say "invalid" — which is a
        lie when GitHub was merely unreachable (ent#162 honest-validation). This
        variant separates the two:

        Returns ``(status, username)``:
          - ``("valid", login)``    — 200, the token authenticates
          - ``("invalid", None)``   — 401/403, GitHub rejected the token
          - ``("unreachable", None)`` — network error or unexpected status; the
            token may be fine, so callers must NOT report it as invalid.
        """
        try:
            response = await self._request("GET", "/user")
        except Exception as e:  # httpx transport / timeout / DNS
            logger.warning(f"GitHub token validation could not reach the API: {e}")
            return "unreachable", None

        if response.status_code == 200:
            return "valid", response.json().get("login")
        if response.status_code == 401:
            # 401 is GitHub's definitive bad/expired-token signal on GET /user.
            return "invalid", None
        # 403 on GET /user is not a scope problem (the endpoint needs no scope) —
        # it's almost always secondary rate-limiting, so treat it (and any other
        # non-200) as transient rather than telling the user their token is bad.
        logger.warning(
            f"GitHub token validation got non-200 status {response.status_code}"
        )
        return "unreachable", None

    async def get_owner_type(self, owner: str) -> Optional[OwnerType]:
        """
        Determine if an owner is a user or organization.

        Args:
            owner: GitHub username or organization name

        Returns:
            OwnerType or None if not found
        """
        try:
            response = await self._request("GET", f"/users/{owner}")
            if response.status_code == 200:
                data = response.json()
                if data.get("type") == "Organization":
                    return OwnerType.ORGANIZATION
                return OwnerType.USER
            return None
        except Exception:
            return None

    async def check_repo_exists(self, owner: str, name: str) -> GitHubRepoInfo:
        """
        Check if a repository exists.

        Args:
            owner: Repository owner (user or org)
            name: Repository name

        Returns:
            GitHubRepoInfo with existence and details
        """
        full_name = f"{owner}/{name}"
        try:
            response = await self._request("GET", f"/repos/{full_name}")
            if response.status_code == 200:
                data = response.json()
                return GitHubRepoInfo(
                    exists=True,
                    full_name=full_name,
                    owner=owner,
                    name=name,
                    private=data.get("private"),
                    default_branch=data.get("default_branch")
                )
            elif response.status_code == 404:
                return GitHubRepoInfo(
                    exists=False,
                    full_name=full_name,
                    owner=owner,
                    name=name
                )
            else:
                raise GitHubError(
                    f"GitHub API error: {response.status_code}",
                    status=response.status_code,
                )
        except httpx.RequestError as e:
            raise GitHubError(f"Network error: {e}")

    async def list_branches(self, repo: str, limit: int = 10) -> list:
        """List up to ``limit`` branches of ``repo`` as [{name, sha}].

        Empty list means the repository has no branches (a fresh repo with no
        commits). Raises GitHubError on API failure so callers can't mistake
        an outage for emptiness. (trinity-enterprise#93)
        """
        try:
            response = await self._request(
                "GET", f"/repos/{repo}/branches", params={"per_page": limit}
            )
        except httpx.RequestError as e:
            raise GitHubError(f"Network error listing branches: {e}")
        if response.status_code == 200:
            return [
                {"name": b.get("name"), "sha": (b.get("commit") or {}).get("sha")}
                for b in response.json()
            ]
        if response.status_code == 404:
            raise GitHubError(f"Repository '{repo}' not found")
        raise GitHubError(
            f"GitHub API error listing branches: {response.status_code}",
            status=response.status_code,
        )

    async def get_branch_sha(self, repo: str, branch: str) -> Optional[str]:
        """Head SHA of ``repo``'s ``branch``, or None if the branch is absent.

        Exact single-branch lookup (`GET /repos/{repo}/branches/{branch}`) —
        immune to pagination, unlike list_branches. Raises GitHubError on API
        failure. (trinity-enterprise#93)
        """
        try:
            response = await self._request("GET", f"/repos/{repo}/branches/{branch}")
        except httpx.RequestError as e:
            raise GitHubError(f"Network error reading branch: {e}")
        if response.status_code == 200:
            return (response.json().get("commit") or {}).get("sha")
        if response.status_code == 404:
            return None
        raise GitHubError(
            f"GitHub API error reading branch: {response.status_code}",
            status=response.status_code,
        )

    async def create_repository(
        self,
        owner: str,
        name: str,
        private: bool = True,
        description: Optional[str] = None,
        auto_init: bool = False
    ) -> GitHubCreateResult:
        """
        Create a new GitHub repository.

        Args:
            owner: Repository owner (user or org)
            name: Repository name
            private: Whether repo should be private
            description: Optional repo description
            auto_init: Whether to initialize with README

        Returns:
            GitHubCreateResult with success status
        """
        # Determine owner type
        owner_type = await self.get_owner_type(owner)
        if owner_type is None:
            return GitHubCreateResult(
                success=False,
                error=f"Owner '{owner}' not found on GitHub"
            )

        # Build payload
        payload = {
            "name": name,
            "private": private,
            "auto_init": auto_init
        }
        if description:
            payload["description"] = description

        # Use correct endpoint
        if owner_type == OwnerType.ORGANIZATION:
            path = f"/orgs/{owner}/repos"
        else:
            path = "/user/repos"

        try:
            response = await self._request(
                "POST", path,
                json=payload,
                timeout=30.0
            )

            if response.status_code == 201:
                data = response.json()
                return GitHubCreateResult(
                    success=True,
                    repo_url=data.get("html_url")
                )
            else:
                error_data = response.json()
                error_msg = error_data.get("message", "Unknown error")

                # Add helpful context
                if owner_type == OwnerType.ORGANIZATION:
                    error_msg += (
                        f" (Organization: {owner}. Ensure PAT has "
                        "'repo' scope and admin access to this organization)"
                    )
                else:
                    error_msg += (
                        " (For personal repos, PAT needs "
                        "'repo' or 'public_repo' scope)"
                    )

                logger.error(
                    f"GitHub API Error: Status={response.status_code}, "
                    f"Response={error_data}, OwnerType={owner_type}"
                )

                return GitHubCreateResult(
                    success=False,
                    error=error_msg
                )

        except httpx.RequestError as e:
            return GitHubCreateResult(
                success=False,
                error=f"Network error: {e}"
            )


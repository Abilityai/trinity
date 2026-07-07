"""
GitHub API Service.

Handles all GitHub API interactions:
- Repository existence checking
- Organization detection
- Repository creation
- Token validation
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
    """Base exception for GitHub API errors."""
    pass


class GitHubAuthError(GitHubError):
    """GitHub authentication failed."""
    pass


class GitHubPermissionError(GitHubError):
    """Insufficient permissions for operation."""
    pass


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
                raise GitHubError(f"GitHub API error: {response.status_code}")
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
            f"GitHub API error listing branches: {response.status_code}"
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
        raise GitHubError(f"GitHub API error reading branch: {response.status_code}")

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


# ============================================================================
# Factory Function
# ============================================================================

def get_github_service(pat: str) -> GitHubService:
    """
    Factory function to create a GitHubService.

    Args:
        pat: GitHub Personal Access Token

    Returns:
        GitHubService instance
    """
    return GitHubService(pat)

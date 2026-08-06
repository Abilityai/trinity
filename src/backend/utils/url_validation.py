"""
URL validation utilities for SSRF prevention.

Provides strict validation for URLs that the backend will connect to,
ensuring they point to allowed external hosts and not internal services.

Related: SEC-179 (pentest finding 3.2.2)
"""

import ipaddress
import socket
from urllib.parse import urlparse

# Allowed hostnames for skills library URLs
ALLOWED_SKILLS_LIBRARY_HOSTS = {"github.com", "www.github.com"}


class EmbeddedCredentialError(ValueError):
    """A URL carries userinfo (`https://<token>@host/...`)."""


def reject_embedded_credentials(url: str) -> None:
    """Refuse a URL embedding a token or password.

    `validate_skills_library_url` checks `parsed.hostname`, which IGNORES
    userinfo, and returns the URL unchanged — so a tokenized clone URL passes
    SSRF validation and is then persisted verbatim wherever the caller stores
    it. Pasting one is an easy mistake: it is the form GitHub hands you for
    scripted clones.

    Deliberately NOT folded into `validate_skills_library_url`: that helper
    serves the pre-ent#237 `skills_library_url` setting too, and an install
    relying on an embedded token for private-repo access would break on
    upgrade. Callers opt in on new surfaces.

    Lives here rather than in the router so it is importable without pulling in
    the whole `routers` package (which drags in the agent-service chain).
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.username or parsed.password:
        raise EmbeddedCredentialError(
            "Repository URL must not embed a token or password. It is stored "
            "and displayed in plain text. Configure a GitHub PAT in Settings "
            "for private repositories instead."
        )


def validate_skills_library_url(url: str) -> str:
    """
    Validate that a skills library URL points to github.com.

    Prevents SSRF by ensuring the URL:
    1. Uses https:// scheme
    2. Hostname is exactly github.com (no subdomains, no lookalikes)
    3. Does not resolve to internal/private IP ranges

    Args:
        url: The URL to validate

    Returns:
        The validated URL

    Raises:
        ValueError: If the URL is not a valid github.com URL
    """
    if not url or not url.strip():
        raise ValueError("Skills library URL cannot be empty")

    url = url.strip()

    # Reject non-http(s) schemes early
    if "://" in url and not url.startswith("https://") and not url.startswith("http://"):
        raise ValueError("Skills library URL must use HTTPS")

    # Handle shorthand format: "owner/repo" or "github.com/owner/repo"
    if not url.startswith("https://") and not url.startswith("http://"):
        # Could be "github.com/owner/repo" or "owner/repo"
        if url.startswith("github.com/"):
            url = f"https://{url}"
        elif "/" in url and not url.startswith(".") and not url.startswith("localhost"):
            # Assume "owner/repo" format — this is fine, will be prefixed with github.com later
            return url
        else:
            raise ValueError("Skills library URL must be a github.com repository URL")

    # Parse the URL
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL format")

    # Enforce HTTPS
    if parsed.scheme != "https":
        raise ValueError("Skills library URL must use HTTPS")

    # Extract hostname (strip port if present)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Skills library URL must have a valid hostname")

    # Strict hostname check — must be exactly github.com
    if hostname.lower() not in ALLOWED_SKILLS_LIBRARY_HOSTS:
        raise ValueError(
            f"Skills library URL must point to github.com (got: {hostname}). "
            "Only GitHub repositories are supported."
        )

    # Defense-in-depth: resolve hostname and reject private/internal IPs
    try:
        resolved_ips = socket.getaddrinfo(hostname, None)
        for family, type_, proto, canonname, sockaddr in resolved_ips:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                raise ValueError(
                    f"Skills library URL resolved to internal address ({ip}). "
                    "This is not allowed."
                )
    except socket.gaierror:
        # DNS resolution failed — allow it to fail later during git clone
        pass
    except ValueError:
        raise  # Re-raise our own ValueError

    return url

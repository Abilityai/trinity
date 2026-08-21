"""Authentication commands: login, logout, status, init."""

import json
from pathlib import Path

import click

from ..client import TrinityClient, TrinityAPIError
from ..config import (
    clear_auth, get_instance_url, get_user, load_config,
    profile_name_from_url, set_auth, set_profile_key, _resolve_profile_name,
)


def _require_access_token(result: dict) -> str:
    """Read the session out of a login response, or fail loudly (#2322).

    A login can succeed at the HTTP level and still issue no session: when a
    second factor is pending, the server answers 200 with a challenge and no
    `access_token`. Reading the field blind stored `None` in the profile and
    turned that into unexplained 401s on every later command, far from the
    cause. The CLI cannot complete a TOTP challenge, so this is terminal —
    but it must say so here.
    """
    token = (result or {}).get("access_token")
    if token:
        return token

    if (result or {}).get("mfa_required"):
        click.echo(
            "Login incomplete: this account requires a second factor, which the "
            "CLI cannot complete. Sign in through the web UI and use an MCP API "
            "key for CLI access (Settings -> MCP Keys).",
            err=True,
        )
    else:
        click.echo("Login failed: the server returned no access token.", err=True)
    raise SystemExit(1)


def _provision_mcp_key(client: TrinityClient, profile_name: str):
    """Ensure the user has an MCP API key and store it in the profile."""
    try:
        result = client.post("/api/mcp/keys/ensure-default")
        if result and result.get("api_key"):
            set_profile_key("mcp_api_key", result["api_key"], profile_name)
            click.echo(f"MCP API key provisioned and saved to profile")
            return result["api_key"]
    except TrinityAPIError:
        # Non-fatal — user can still use JWT auth
        pass
    return None


def _write_mcp_json(instance_url: str, mcp_api_key: str):
    """Write or merge Trinity MCP server config into .mcp.json in current directory."""
    mcp_path = Path.cwd() / ".mcp.json"
    config = {}
    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    servers = config.setdefault("mcpServers", {})
    # Derive MCP endpoint from instance URL (replace backend port with MCP port)
    mcp_url = instance_url.rstrip("/")
    if mcp_url.endswith(":8000"):
        mcp_url = mcp_url.replace(":8000", ":8080")
    elif ":" not in mcp_url.split("//")[-1]:
        # No port specified — assume /mcp path on same host
        mcp_url = mcp_url + ":8080"

    servers["trinity"] = {
        "type": "streamable-http",
        "url": f"{mcp_url}/mcp",
        "headers": {
            "Authorization": f"Bearer {mcp_api_key}"
        }
    }

    mcp_path.write_text(json.dumps(config, indent=2) + "\n")
    click.echo(f"MCP server config written to {mcp_path}")

    # Add .mcp.json to .gitignore if in a git repo (contains API key)
    cwd = Path.cwd()
    if (cwd / ".git").exists():
        gitignore = cwd / ".gitignore"
        marker = ".mcp.json"
        if gitignore.exists():
            content = gitignore.read_text()
            if marker not in content:
                with open(gitignore, "a") as f:
                    f.write(f"\n{marker}\n")
        else:
            gitignore.write_text(f"{marker}\n")


def _normalize_url(url: str) -> str:
    """Normalize a URL: add https:// if no scheme, strip trailing slash."""
    url = url.strip().rstrip("/")
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def _get_profile_name(ctx: click.Context) -> str | None:
    """Extract the --profile value from the root context."""
    root = ctx.find_root()
    return root.obj.get("profile") if root.obj else None


@click.command()
@click.option("--instance", help="Trinity instance URL (e.g. https://trinity.example.com)")
@click.option("--profile", "profile_opt", default=None,
              help="Profile name to store credentials under (default: hostname)")
@click.option("--admin", is_flag=True, help="Login as admin with password instead of email")
@click.pass_context
def login(ctx, instance, profile_opt, admin):
    """Log in to a Trinity instance.

    By default, uses email verification. With --admin, uses password login.
    """
    profile_name = profile_opt or _get_profile_name(ctx)
    url = instance or get_instance_url(profile_name)
    if not url:
        url = click.prompt("Trinity instance URL")
    url = _normalize_url(url)

    # Verify instance is reachable (with retry)
    for attempt in range(3):
        client = TrinityClient(base_url=url, token="none")
        try:
            client.get_unauthenticated("/api/auth/mode")
            break
        except Exception:
            click.echo(f"Cannot reach {url}.", err=True)
            if attempt < 2:
                url = _normalize_url(click.prompt("Try a different URL"))
            else:
                click.echo("Giving up after 3 attempts.", err=True)
                raise SystemExit(1)

    if admin:
        # Admin password login via /api/token (form-encoded OAuth2)
        password = click.prompt("Admin password", hide_input=True)
        try:
            result = client.post_form("/api/token", {
                "username": "admin",
                "password": password,
            })
        except TrinityAPIError as e:
            click.echo(f"Admin login failed: {e.detail}", err=True)
            raise SystemExit(1)

        token = _require_access_token(result)

        # Fetch user info with the new token
        authed_client = TrinityClient(base_url=url, token=token)
        try:
            user = authed_client.get("/api/users/me")
        except TrinityAPIError:
            user = {"username": "admin", "role": "admin"}

        target_profile = profile_name or profile_name_from_url(url)
        set_auth(url, token, user, profile_name=target_profile)
        click.echo(f"Logged in as admin [profile: {target_profile}]")

        _provision_mcp_key(authed_client, target_profile)
        return

    email = click.prompt("Email")

    # Request verification code
    try:
        client.post_unauthenticated("/api/auth/email/request", {"email": email})
    except TrinityAPIError as e:
        click.echo(f"Error requesting code: {e.detail}", err=True)
        raise SystemExit(1)

    click.echo(f"Verification code sent to {email}")
    code = click.prompt("Enter 6-digit code")

    # Verify code and get token
    try:
        result = client.post_unauthenticated("/api/auth/email/verify", {
            "email": email,
            "code": code,
        })
    except TrinityAPIError as e:
        click.echo(f"Verification failed: {e.detail}", err=True)
        raise SystemExit(1)

    token = _require_access_token(result)
    user = result.get("user")

    # Determine profile name: explicit > global flag > derive from URL
    target_profile = profile_name or profile_name_from_url(url)
    set_auth(url, token, user, profile_name=target_profile)
    name = user.get("name") or user.get("email") or user.get("username") if user else email
    click.echo(f"Logged in as {name} [profile: {target_profile}]")

    # Auto-provision MCP API key
    authed_client = TrinityClient(base_url=url, token=token)
    _provision_mcp_key(authed_client, target_profile)


@click.command()
@click.pass_context
def logout(ctx):
    """Clear stored credentials for the current profile."""
    profile_name = _get_profile_name(ctx)
    clear_auth(profile_name)
    resolved = _resolve_profile_name(profile_name)
    click.echo(f"Logged out [profile: {resolved}]")


@click.command()
@click.pass_context
def status(ctx):
    """Show current login status and instance info."""
    profile_name = _get_profile_name(ctx)
    resolved = _resolve_profile_name(profile_name)
    url = get_instance_url(profile_name)

    click.echo(f"Profile:  {resolved}")

    if not url:
        click.echo("Instance: Not configured. Run 'trinity init' or 'trinity login'.")
        return

    user = get_user(profile_name)
    config = load_config()
    profile_data = config.get("profiles", {}).get(resolved, {})

    click.echo(f"Instance: {url}")
    if user:
        click.echo(f"User:     {user.get('email') or user.get('username')}")
        click.echo(f"Role:     {user.get('role', 'unknown')}")
    elif profile_data.get("token"):
        click.echo("User:     (API key auth)")
    else:
        click.echo("User:     Not logged in")

    # Check connectivity
    try:
        client = TrinityClient(base_url=url, token=profile_data.get("token", "none"))
        client.get_unauthenticated("/api/auth/mode")
        click.echo("Status:   Connected")
    except Exception:
        click.echo("Status:   Unreachable")


@click.command()
@click.option("--profile", "profile_opt", default=None,
              help="Profile name (default: derived from instance hostname)")
@click.option("--admin", is_flag=True, help="Login as admin with password instead of email")
@click.pass_context
def init(ctx, profile_opt, admin):
    """Set up Trinity CLI: configure instance, request access, and log in.

    One command to go from zero to authenticated. Creates a named profile
    for the instance (defaults to hostname).

    If you don't have an instance URL, leave it blank for access-request
    instructions. Use --admin to authenticate with admin password instead of email.
    """
    url = click.prompt(
        "Trinity instance URL (leave blank to request access)", default="", show_default=False
    )
    url = _normalize_url(url)

    # No URL — user needs to request instance access
    if not url:
        click.echo(
            "\nNo instance URL set.\n\n"
            "To request access, email access@ability.ai with the subject "
            "'Trinity access request'.\n"
            "Once you receive your instance URL, run 'trinity init' again with "
            "the URL to complete setup."
        )
        return

    # Verify instance is reachable (with retry)
    for attempt in range(3):
        client = TrinityClient(base_url=url, token="none")
        try:
            client.get_unauthenticated("/api/auth/mode")
            click.echo(f"Connected to {url}")
            break
        except Exception:
            click.echo(f"Cannot reach {url}.", err=True)
            if attempt < 2:
                url = _normalize_url(click.prompt("Try a different URL"))
            else:
                click.echo("Giving up after 3 attempts.", err=True)
                raise SystemExit(1)

    # Determine profile name
    profile_name = profile_opt or _get_profile_name(ctx) or profile_name_from_url(url)

    if admin:
        # Admin password login
        password = click.prompt("Admin password", hide_input=True)
        try:
            result = client.post_form("/api/token", {
                "username": "admin",
                "password": password,
            })
        except TrinityAPIError as e:
            click.echo(f"Admin login failed: {e.detail}", err=True)
            raise SystemExit(1)

        token = _require_access_token(result)
        authed_client = TrinityClient(base_url=url, token=token)
        try:
            user = authed_client.get("/api/users/me")
        except TrinityAPIError:
            user = {"username": "admin", "role": "admin"}
    else:
        email = click.prompt("Email")

        # Request access (auto-approve endpoint)
        try:
            client.post_unauthenticated("/api/access/request", {"email": email})
            click.echo("Access granted")
        except TrinityAPIError as e:
            if e.status_code == 409:
                click.echo("Already registered")
            else:
                click.echo(f"Access request failed: {e.detail}", err=True)
                raise SystemExit(1)

        # Send verification code
        try:
            client.post_unauthenticated("/api/auth/email/request", {"email": email})
        except TrinityAPIError as e:
            click.echo(f"Error requesting code: {e.detail}", err=True)
            raise SystemExit(1)

        click.echo(f"Verification code sent to {email}")
        code = click.prompt("Enter 6-digit code")

        # Verify and get token
        try:
            result = client.post_unauthenticated("/api/auth/email/verify", {
                "email": email,
                "code": code,
            })
        except TrinityAPIError as e:
            click.echo(f"Verification failed: {e.detail}", err=True)
            raise SystemExit(1)

        token = _require_access_token(result)
        user = result.get("user")

    set_auth(url, token, user, profile_name=profile_name)
    name = user.get("name") or user.get("email") or user.get("username") if user else "admin"
    click.echo(f"Logged in as {name} [profile: {profile_name}]")

    # Auto-provision MCP API key and write .mcp.json
    if not admin:
        authed_client = TrinityClient(base_url=url, token=token)
    mcp_key = _provision_mcp_key(authed_client, profile_name)
    if mcp_key:
        _write_mcp_json(url, mcp_key)

    click.echo(f"\nTrinity CLI is ready. Try 'trinity agents list'.")

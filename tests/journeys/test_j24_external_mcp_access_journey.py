"""Journey J24: my fresh install does not serve MCP to the outside until I turn it on (abilityai/trinity-enterprise#1101).

**Promise:** a Trinity I have just installed does not answer an MCP client on
another machine. That client is refused with a reason it can act on: a 403,
`X-Trinity-Refusal: external_mcp_access_off`, JSON-RPC error -32003, and a
message that names Settings → MCP Keys. Once I turn External MCP access on as
the admin, the same client connects and sees tools. When I turn it off again,
the client is refused again within the gate's cache window.

**How it is driven.** This harness dials `MCP_URL` from the host the stack runs
on. The MCP server sees that as an OFF-BOX peer: a published port arrives from
a Docker bridge gateway, never from inside the agent network. That makes it the
outside client this promise is about. The agents' own path to
`http://mcp-server:8080/mcp` on `trinity-agent-network` is never refused, and it
is NOT dialled from here. The host cannot reach it, and J10's agent-key calls
are what exercise it. The switch is flipped through the admin API, exactly as
the Settings page flips it. Every test puts it back the way it found it, so the
session-wide `external_mcp_access_on` fixture (conftest) is unaffected whatever
the order.

No catalog invariant covers "the perimeter is closed by default" yet. The
record carries an empty list, not an invented id.
"""
import pytest

from .conftest import (
    EXTERNAL_ACCESS_REFUSAL,
    MCP_URL,
    McpSession,
    clear_external_access,
    external_access_refusal_problem,
    external_access_state,
    mcp_initialize,
    restore_external_access,
    set_external_access,
    wait_for_mcp_initialize,
)

pytestmark = pytest.mark.journey


def _settle(client, api_key: str, prior: dict) -> None:
    """Restore the switch and wait until an outside client sees it, so the
    next test (or J10's session fixture) does not inherit a cached verdict."""
    restore_external_access(client, prior)
    wait_for_mcp_initialize(
        api_key, refused=not prior.get("enabled"),
        describe="restoring External MCP access to how this test found it never "
                 "reached an outside client",
    )


def test_a_fresh_install_refuses_an_outside_mcp_client_with_a_named_reason(
    journey_client, journey_mcp_user_key,
):
    prior = external_access_state(journey_client)
    try:
        state = prior
        if state.get("source") == "settings":
            # Turned on by an earlier fixture, or backfilled on an upgraded
            # install. With the row cleared, the switch is what a fresh install has.
            state = clear_external_access(journey_client)
        assert state.get("source") == "default" and state.get("enabled") is False, (
            f"with no Settings row, External MCP access resolves to "
            f"enabled={state.get('enabled')!r} from {state.get('source')!r}. A fresh "
            f"install is off by default, so this stack opts in through "
            f"MCP_EXTERNAL_ACCESS and cannot show the fresh-install promise."
        )
        resp = wait_for_mcp_initialize(
            journey_mcp_user_key, refused=True,
            describe=f"External MCP access is off from its default, but {MCP_URL} "
                     f"never refused an outside client with {EXTERNAL_ACCESS_REFUSAL}",
        )
        # The same check the poll applied, restated as the user-visible promise.
        assert external_access_refusal_problem(resp) is None
        message = resp.json()["error"]["message"]
        assert "Settings → MCP Keys" in message, (
            f"the refusal must tell the client where an admin turns access on; "
            f"it said: {message[:200]}"
        )
        # The refusal comes before authentication: an outside client with no
        # key at all gets the same named reason, not a 401.
        assert external_access_refusal_problem(mcp_initialize()) is None
    finally:
        _settle(journey_client, journey_mcp_user_key, prior)


def test_after_an_admin_turns_external_mcp_access_on_the_same_client_connects(
    journey_client, journey_mcp_user_key,
):
    prior = external_access_state(journey_client)
    try:
        set_external_access(journey_client, True)
        wait_for_mcp_initialize(
            journey_mcp_user_key, refused=False,
            describe=f"an admin turned External MCP access on, but {MCP_URL} never "
                     f"answered the outside client's initialize with 200",
        )
        session = McpSession(journey_mcp_user_key)
        assert session.tools, (
            "the outside client connected but the server advertised no tools to "
            "its user-scoped key"
        )

        set_external_access(journey_client, False)
        resp = wait_for_mcp_initialize(
            journey_mcp_user_key, refused=True,
            describe=f"an admin turned External MCP access off again, but {MCP_URL} "
                     f"kept serving the outside client past the gate's cache window",
        )
        assert external_access_refusal_problem(resp) is None
    finally:
        _settle(journey_client, journey_mcp_user_key, prior)

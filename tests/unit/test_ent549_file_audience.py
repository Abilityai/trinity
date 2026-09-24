"""A shared file is for the person the turn was for (trinity-enterprise#549).

`agent_shared_files` was scoped by agent alone, so the Workspace Files tab
listed every active share of an agent to everyone on its roster: a file made in
one person's chat showed up — download link included — in another person's tab.
Third occurrence of one class (asks ent#428, reports ent#365): a table scoped
by an owning entity gains a per-person dimension, and every reader that
predates the column keeps disclosing until it is taught to narrow.

What these tests are FOR, in the order that matters:

1. **Round trips.** Every share test in this repo mocked `_persist_and_register`
   and every listing test seeded rows by INSERT, so nothing crossed write → read:
   an addressee dropped anywhere in the three keyword hops into
   `ops.create().values(...)` left the whole suite green. These drive the real
   `create_share` / `create_share_from_bytes` into a real SQLite file and read
   back through the real `portal_documents`; only the container extraction and
   the disk write are stubbed.

2. **The failure direction.** Deciding who a turn was for needs POSITIVE
   evidence. A web-terminal `claude` session holds the agent's MCP key and has
   no execution row, so "the agent has exactly one running turn — take it"
   would hand an operator's file to whichever client happened to be
   mid-conversation. No evidence ⇒ owner-only, always.

3. **A stale id proves the conversation, never the person.** A resumed session
   cites execution ids from its own history (observed live: the id of a turn 22
   minutes old). The person comes from the turn that is running NOW.

Emails are `example.com` and the phone number is from the 555-01xx fiction
range: this is a public repository.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

AGENT = "atlas"
OTHER_AGENT = "borealis"
OWNER = "alice@example.com"      # owns atlas
ADA = "ada@example.com"          # a client of atlas
BOB = "bob@example.com"          # another client of atlas
STRANGER = "stranger@example.net"
WA_NUMBER = "whatsapp:+15555550142"


# --------------------------------------------------------------------------- #
# Fixture: a real SQLite file over exactly the tables the share path touches
# --------------------------------------------------------------------------- #

@pytest.fixture()
def audience_db(tmp_path, monkeypatch):
    """Real SQLite, seeded idempotently (the PostgreSQL tier shares ONE database
    across tests — the #2242 lesson — so an `insert(id=1)` must tolerate a row
    that is already there)."""
    db_file = tmp_path / "trinity-549.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m, agent_sharing, agent_ownership, users, system_settings,
        agent_shared_files, portal_file_dismissals, schedule_executions,
        idempotency_keys,
    )
    m.create_all(get_engine(), tables=[
        agent_sharing, agent_ownership, users, system_settings,
        agent_shared_files, portal_file_dismissals, schedule_executions,
        idempotency_keys,
    ])

    from sqlalchemy import delete, insert, select

    agents = [AGENT, OTHER_AGENT]

    def _clear(conn):
        conn.execute(delete(agent_sharing).where(agent_sharing.c.agent_name.in_(agents)))
        conn.execute(delete(agent_shared_files).where(agent_shared_files.c.agent_name.in_(agents)))
        conn.execute(delete(portal_file_dismissals).where(portal_file_dismissals.c.agent_name.in_(agents)))
        conn.execute(delete(schedule_executions).where(schedule_executions.c.agent_name.in_(agents)))
        conn.execute(delete(idempotency_keys))

    with get_engine().begin() as conn:
        if not conn.execute(select(users.c.id).where(users.c.id == 1)).first():
            conn.execute(insert(users).values(
                id=1, username="alice", role="creator", email=OWNER,
                created_at="t", updated_at="t"))
        for agent in agents:
            if not conn.execute(select(agent_ownership.c.agent_name)
                                .where(agent_ownership.c.agent_name == agent)).first():
                conn.execute(insert(agent_ownership).values(
                    agent_name=agent, owner_id=1, created_at="t", is_system=0,
                    deleted_at=None, file_sharing_enabled=1))
        _clear(conn)
        for client in (ADA, BOB):
            conn.execute(insert(agent_sharing).values(
                agent_name=AGENT, shared_with_email=client, shared_by_id=1, created_at="t"))

    yield get_engine()

    with get_engine().begin() as conn:
        _clear(conn)


@pytest.fixture()
def share_service(audience_db, tmp_path, monkeypatch):
    """The real share service with ONLY the container read and the disk root
    replaced. `_persist_and_register`, the effect guard, the DB layer and the
    resolver all run for real — that is the point of this file."""
    from services import agent_shared_files_service as svc
    from client_portal import service as portal

    async def fake_extract(agent_name, container_path):
        name = container_path.rsplit("/", 1)[-1]
        return (f"bytes-of-{name}".encode(), name)

    monkeypatch.setattr(svc, "extract_from_agent", fake_extract)
    monkeypatch.setattr(svc, "STORAGE_ROOT", str(tmp_path / "agent-files"))
    monkeypatch.setattr(svc, "check_disk_space", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_public_chat_url", lambda: "https://chat.example.com")
    monkeypatch.setattr(portal, "get_portal_base_url", lambda: "")
    return svc


def _turn(*, agent=AGENT, client=ADA, channel="portal", chat="sess-ada", **extra):
    """A turn exactly as the platform creates it: the row is BORN running."""
    from database import db as core_db
    return core_db.create_task_execution(
        agent_name=agent, message="hi", triggered_by="public",
        source_user_email=client, source_channel=channel,
        source_channel_chat_id=chat, source_channel_client=client, **extra).id


def _finish(execution_id):
    from db.engine import get_engine
    from db.tables import schedule_executions
    from sqlalchemy import update
    with get_engine().begin() as conn:
        conn.execute(update(schedule_executions)
                     .where(schedule_executions.c.id == execution_id)
                     .values(status="success", completed_at="2099-01-01T00:00:00Z"))


def _files_tab(email, *, platform=False, agent=AGENT):
    from client_portal import service as portal
    return {d["filename"] for d in
            portal.portal_documents(agent, email, include_owned=platform)["documents"]}


def _row(file_id):
    from database import db as core_db
    return core_db.get_agent_shared_file(file_id)


# --------------------------------------------------------------------------- #
# 1. Round trips — write through the real service, read through the real tab
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_file_shared_in_adas_chat_is_in_adas_tab_and_nobody_elses(share_service):
    """AC1 — and the operator's repro in unit form: before the fix Bob's tab
    listed Ada's file, link included."""
    turn = _turn(client=ADA)

    result = await share_service.create_share(
        AGENT, "report.pdf", execution_id=turn, actor_is_agent=True)

    assert _files_tab(ADA) == {"report.pdf"}
    assert _files_tab(BOB) == set()
    row = _row(result["file_id"])
    assert row["addressed_to_email"] == ADA
    assert row["audience_source"] == "turn"
    assert result["visible_to_requester"] is True


@pytest.mark.asyncio
async def test_a_terminal_share_never_lands_in_the_client_who_happens_to_be_chatting(share_service):
    """The over-share both independent reviews found. A web-terminal `claude`
    session holds the agent's key and has NO execution row, so it cites no id.
    Ada's turn is the agent's only running execution. It is not evidence."""
    _turn(client=ADA)

    result = await share_service.create_share(AGENT, "ops-notes.txt", actor_is_agent=True)

    assert _files_tab(ADA) == set()
    row = _row(result["file_id"])
    assert row["addressed_to_email"] is None
    assert row["audience_source"] == "ambiguous"
    # Honest status: the agent is told, and told what to do about it.
    assert result["visible_to_requester"] is False
    assert "audience_email" in result["visibility_note"]


@pytest.mark.asyncio
async def test_a_human_calling_the_route_has_no_turn_at_all(share_service):
    """An owner or a user-scoped key passes `assert_agent_owner` on the share
    route. Whatever id they cite, they are not an agent in a turn."""
    turn = _turn(client=ADA)

    result = await share_service.create_share(
        AGENT, "by-hand.txt", execution_id=turn, actor_is_agent=False)

    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "none"


@pytest.mark.asyncio
async def test_a_stale_id_proves_the_conversation_and_the_running_turn_names_the_person(share_service):
    """Observed live: a resumed Claude session cited the id of a turn 22 minutes
    old. Same conversation, exactly one turn running in it ⇒ that turn's person."""
    stale = _turn(client=ADA, chat="sess-ada")
    _finish(stale)
    _turn(client=ADA, chat="sess-ada")            # the turn the call is really in
    _turn(client=BOB, chat="sess-bob")            # somebody else, concurrently

    result = await share_service.create_share(
        AGENT, "chart.png", execution_id=stale, actor_is_agent=True)

    assert _files_tab(ADA) == {"chart.png"}
    assert _files_tab(BOB) == set()
    assert _row(result["file_id"])["audience_source"] == "turn"


@pytest.mark.asyncio
async def test_in_a_room_a_stale_id_from_one_human_does_not_send_the_file_to_them(share_service):
    """A room shares ONE Claude session across its humans, so the model citing
    another participant's old id is the common case there. The file goes to the
    person whose turn is running, not to the person the stale id belonged to."""
    adas_old_turn = _turn(client=ADA, channel="room", chat="room-7")
    _finish(adas_old_turn)
    _turn(client=BOB, channel="room", chat="room-7")

    await share_service.create_share(
        AGENT, "minutes.md", execution_id=adas_old_turn, actor_is_agent=True)

    assert _files_tab(BOB) == {"minutes.md"}
    assert _files_tab(ADA) == set()


@pytest.mark.asyncio
async def test_a_stale_id_with_no_running_turn_in_its_conversation_is_not_evidence(share_service):
    stale = _turn(client=ADA, chat="sess-ada")
    _finish(stale)
    _turn(client=BOB, chat="sess-bob")            # running, but a DIFFERENT conversation

    result = await share_service.create_share(
        AGENT, "late.txt", execution_id=stale, actor_is_agent=True)

    assert _files_tab(ADA) == set() and _files_tab(BOB) == set()
    assert _row(result["file_id"])["audience_source"] == "ambiguous"


@pytest.mark.asyncio
async def test_two_turns_running_in_one_conversation_and_a_stale_id_is_ambiguous(share_service):
    stale = _turn(client=ADA, channel="room", chat="room-7")
    _finish(stale)
    _turn(client=ADA, channel="room", chat="room-7")
    _turn(client=BOB, channel="room", chat="room-7")

    result = await share_service.create_share(
        AGENT, "which.txt", execution_id=stale, actor_is_agent=True)

    assert _row(result["file_id"])["audience_source"] == "ambiguous"


@pytest.mark.asyncio
async def test_an_id_from_another_agent_cannot_import_that_agents_client(share_service):
    """The id must belong to the CALLING agent (the learnings-ledger rule for a
    path that stops asking a remote authority: bind every identifier)."""
    foreign = _turn(agent=OTHER_AGENT, client=ADA)

    result = await share_service.create_share(
        AGENT, "x.txt", execution_id=foreign, actor_is_agent=True)

    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "ambiguous"


@pytest.mark.asyncio
async def test_a_schedule_or_operator_turn_has_no_person_so_the_file_is_the_owners(share_service):
    """AC3. The owner reads it in the Workspace too (the ruling: what an owner
    sees there is what is addressed to them PLUS what is owner-only)."""
    from database import db as core_db
    turn = core_db.create_task_execution(agent_name=AGENT, message="nightly",
                                         triggered_by="schedule").id

    result = await share_service.create_share(
        AGENT, "nightly.csv", execution_id=turn, actor_is_agent=True)

    assert _row(result["file_id"])["audience_source"] == "none"
    assert _files_tab(ADA) == set() and _files_tab(BOB) == set()
    assert _files_tab(OWNER, platform=True) == {"nightly.csv"}
    assert result["visible_to_requester"] is None      # no requester ⇒ no claim


@pytest.mark.asyncio
async def test_an_agent_to_agent_child_inherits_a_persons_context_but_the_file_is_the_owners(share_service):
    """AC3, the A2A row. The child row carries the parent's channel context
    (that is how completion reports find their way back); `source_channel_agent`
    names the agent the context really belongs to, and that is the tell."""
    child = _turn(client=ADA, source_channel_agent=OTHER_AGENT)

    result = await share_service.create_share(
        AGENT, "sub-task.txt", execution_id=child, actor_is_agent=True)

    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "none"


@pytest.mark.asyncio
async def test_the_owners_own_workspace_chat_addresses_the_file_to_the_owner(share_service):
    turn = _turn(client=OWNER, chat="sess-owner")

    await share_service.create_share(AGENT, "mine.txt", execution_id=turn, actor_is_agent=True)

    assert _files_tab(OWNER, platform=True) == {"mine.txt"}
    assert _files_tab(ADA) == set()


def test_an_owner_on_a_portal_token_cannot_open_the_tab_at_all(share_service):
    """ent#358, unchanged: a magic-link portal token carries no platform identity
    to own anything with, and Trinity refuses a self-share — so the roster gate
    404s before any file is read. (Named for what it asserts: this used to claim
    "reads only what is theirs" while never reaching a read.)"""
    from client_portal.service import ClientPortalError
    with pytest.raises(ClientPortalError) as exc:
        _files_tab(OWNER, platform=False)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_platform_user_who_is_not_the_owner_never_reads_owner_only_files(share_service):
    """THE guard of "the owner also reads what is addressed to nobody". A signed-in
    platform user the agent is merely SHARED with — an internal colleague, an
    admin with a share — is a viewer. Owner-only means a schedule's output, an
    operator chat's, every could-not-tell and every pre-column row, download
    token included. `include_owner_only=bool(include_owned)` passed every other
    test in this file until this one existed."""
    from database import db as core_db
    sched = core_db.create_task_execution(agent_name=AGENT, message="n", triggered_by="schedule").id
    await share_service.create_share(AGENT, "payroll.csv", execution_id=sched, actor_is_agent=True)
    await share_service.create_share(AGENT, "could-not-tell.txt", actor_is_agent=True)

    assert _files_tab(ADA, platform=True) == set()
    assert _files_tab(OWNER, platform=True) == {"payroll.csv", "could-not-tell.txt"}


@pytest.mark.asyncio
async def test_a_room_style_mixed_case_email_still_reaches_its_person(share_service):
    """Rooms stamp a raw `current_user.email` and `users.email` is never
    normalised; portal identities are always lower-cased. One normaliser."""
    turn = _turn(client="Ada@Example.COM", channel="room", chat="room-9")

    await share_service.create_share(AGENT, "caps.txt", execution_id=turn, actor_is_agent=True)

    assert _files_tab(ADA) == {"caps.txt"}


@pytest.mark.asyncio
async def test_an_unverified_channel_user_is_an_address_not_an_email(share_service):
    """`source_user_email` is `verified_email OR the channel-native id`, so an
    unverified user carries `telegram:<bot>:<id>` there. The resolver never reads
    that column on a channel turn: the owner's panel shows the channel address."""
    from database import db as core_db
    turn = core_db.create_task_execution(
        agent_name=AGENT, message="hi", triggered_by="telegram",
        source_user_email="telegram:9001:424242", source_channel="telegram",
        source_channel_chat_id="424242").id

    result = await share_service.create_share(AGENT, "t.txt", execution_id=turn, actor_is_agent=True)

    row = _row(result["file_id"])
    assert row["addressed_to_email"] is None
    assert row["addressed_to_channel"] == "telegram:424242"
    # Addressed to a channel identity ≠ owner-only: it is not the owner's file
    # either, so it stays off the owner's Workspace tab (the Sharing panel has it).
    assert _files_tab(OWNER, platform=True) == set()


@pytest.mark.asyncio
async def test_a_verified_channel_user_finds_the_file_in_their_tab(share_service):
    """AC2 — the file reaches the email the channel binding maps to."""
    from database import db as core_db
    turn = core_db.create_task_execution(
        agent_name=AGENT, message="hi", triggered_by="whatsapp",
        source_user_email=ADA, source_channel="whatsapp",
        source_channel_chat_id=WA_NUMBER,
        source_channel_client=ADA).id          # the router: a verified speaker, one-to-one

    result = await share_service.create_share(AGENT, "w.txt", execution_id=turn, actor_is_agent=True)

    row = _row(result["file_id"])
    assert row["addressed_to_email"] == ADA
    assert row["addressed_to_channel"] == WA_NUMBER       # already prefixed — not doubled
    assert _files_tab(ADA) == {"w.txt"}


@pytest.mark.asyncio
async def test_a_group_turn_never_lands_in_the_unlockers_files_tab(share_service):
    """In a channel GROUP the verified email is the UNLOCKER's — set once per
    group, not per speaker — and `source_user_email` carries it. MEM-001 refuses
    group mode for exactly this reason. A file shared in a turn somebody ELSE in
    the group asked for must not be listed for the person who unlocked the bot."""
    from database import db as core_db
    turn = core_db.create_task_execution(
        agent_name=AGENT, message="can you make the chart?", triggered_by="telegram",
        source_user_email=ADA,                   # the unlocker — NOT the speaker
        source_channel="telegram", source_channel_chat_id="-1001234567890").id

    result = await share_service.create_share(AGENT, "chart.png", execution_id=turn, actor_is_agent=True)

    row = _row(result["file_id"])
    assert row["addressed_to_email"] is None
    assert row["addressed_to_channel"] == "telegram:-1001234567890"
    assert _files_tab(ADA) == set()


def test_channel_media_is_addressed_by_the_platform_caller(share_service):
    """AC2's other half: WhatsApp media and voice notes are created by platform
    code that already holds the recipient — no turn resolution, no agent."""
    result = share_service.create_share_from_bytes(
        AGENT, b"OggS-voice", display_name="voice.ogg",
        addressed_to_email=BOB, addressed_to_channel=WA_NUMBER)

    assert _files_tab(BOB) == {"voice.ogg"}
    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "channel"


def test_channel_media_for_an_unverified_number_is_in_nobodys_tab(share_service):
    """The defect the issue names: `voice-reply.ogg` in every client's tab."""
    share_service.create_share_from_bytes(
        AGENT, b"OggS-voice", display_name="voice-reply.ogg",
        addressed_to_channel=WA_NUMBER)

    assert _files_tab(ADA) == set() and _files_tab(BOB) == set()
    assert _files_tab(OWNER, platform=True) == set()


@pytest.mark.asyncio
async def test_an_agent_tasking_itself_is_still_an_agent_to_agent_call(share_service):
    """`source_channel_agent` is set by inheritance and by nothing else, so
    non-NULL MEANS inherited — whoever it names. For a self-task, and for
    A→B→A, it names the EXECUTING agent; a `!=` comparison waved both through and
    addressed the file to a client picked via an agent-typed parent id."""
    child = _turn(client=ADA, source_channel_agent=AGENT)

    result = await share_service.create_share(
        AGENT, "self-task.txt", execution_id=child, actor_is_agent=True)

    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "none"
    assert result["visible_to_requester"] is None


@pytest.mark.asyncio
async def test_delegated_children_do_not_vote_on_who_the_conversation_is_with(share_service):
    """A turn that fanned out to itself still shares to its person while the
    children run: they are not turns OF the conversation."""
    parent = _turn(client=ADA, chat="sess-ada")
    _turn(client=ADA, chat="sess-ada", source_channel_agent=AGENT)
    _turn(client=ADA, chat="sess-ada", source_channel_agent=AGENT)

    await share_service.create_share(AGENT, "summary.md", execution_id=parent, actor_is_agent=True)

    assert _files_tab(ADA) == {"summary.md"}


@pytest.mark.asyncio
async def test_a_zombie_row_in_a_room_is_not_a_second_opinion_it_is_doubt(share_service):
    """A crash leaves Ada's room turn `running` until the stale sweep. The room's
    one Claude session hands the model her id while BOB's turn is the live one. A
    cited id proves the conversation and never the person — live or not."""
    adas_zombie = _turn(client=ADA, channel="room", chat="room-7")
    _turn(client=BOB, channel="room", chat="room-7")

    result = await share_service.create_share(
        AGENT, "minutes.md", execution_id=adas_zombie, actor_is_agent=True)

    assert _files_tab(ADA) == set() and _files_tab(BOB) == set()
    assert _row(result["file_id"])["audience_source"] == "ambiguous"
    assert result["visible_to_requester"] is False


@pytest.mark.asyncio
async def test_a_zombie_of_the_same_person_changes_nothing(share_service):
    """One-to-one, the zombie and the live turn are the same person: they agree."""
    zombie = _turn(client=ADA, chat="sess-ada")
    _turn(client=ADA, chat="sess-ada")

    await share_service.create_share(AGENT, "ok.txt", execution_id=zombie, actor_is_agent=True)

    assert _files_tab(ADA) == {"ok.txt"}


@pytest.mark.asyncio
async def test_another_agents_turn_in_the_same_room_is_not_this_agents_conversation(share_service):
    """A room holds several agents. This agent cites its own finished turn while
    the only RUNNING turn in the room is another agent's, for Bob. The lookup is
    bound to the calling agent; without that predicate Bob would be handed a file
    from an agent he was not talking to."""
    mine = _turn(client=ADA, channel="room", chat="room-7")
    _finish(mine)
    _turn(agent=OTHER_AGENT, client=BOB, channel="room", chat="room-7")

    result = await share_service.create_share(
        AGENT, "not-bobs.txt", execution_id=mine, actor_is_agent=True)

    assert _files_tab(BOB) == set()
    assert _row(result["file_id"])["audience_source"] == "ambiguous"


@pytest.mark.asyncio
async def test_a_revoked_or_expired_file_leaves_the_tab(share_service, audience_db):
    """Same lifecycle as the owner's list. An owner who revokes a file does not
    expect it to stay — link and all — in the addressee's tab."""
    from database import db as core_db
    from db.tables import agent_shared_files
    from sqlalchemy import update
    turn = _turn(client=ADA)
    kept = await share_service.create_share(AGENT, "kept.txt", execution_id=turn, actor_is_agent=True)
    gone = await share_service.create_share(AGENT, "revoked.txt", execution_id=turn, actor_is_agent=True)
    old = await share_service.create_share(AGENT, "expired.txt", execution_id=turn, actor_is_agent=True)
    assert _files_tab(ADA) == {"kept.txt", "revoked.txt", "expired.txt"}

    core_db.revoke_agent_shared_file(gone["file_id"])
    with audience_db.begin() as conn:
        conn.execute(update(agent_shared_files).where(agent_shared_files.c.id == old["file_id"])
                     .values(expires_at="2000-01-01T00:00:00+00:00"))

    assert _files_tab(ADA) == {"kept.txt"}
    assert kept["file_id"]


@pytest.mark.asyncio
async def test_the_viewer_is_matched_however_their_address_is_cased(share_service):
    """One normaliser on BOTH sides. Portal principals arrive lower-cased today;
    the reader does not depend on that staying true."""
    turn = _turn(client=ADA)
    await share_service.create_share(AGENT, "case.txt", execution_id=turn, actor_is_agent=True)

    assert _files_tab("  Ada@Example.COM ") == {"case.txt"}


@pytest.mark.asyncio
async def test_a_person_with_no_tab_is_not_told_the_file_is_in_their_tab(share_service):
    """A verified channel user need not be someone the agent is shared with. The
    row is theirs — it appears the day the owner shares the agent with them — but
    the tab 404s for them today, so `visible_to_requester` makes no claim rather
    than have the agent say "it is in your Files tab"."""
    from database import db as core_db
    turn = core_db.create_task_execution(
        agent_name=AGENT, message="hi", triggered_by="whatsapp", source_user_email=STRANGER,
        source_channel="whatsapp", source_channel_chat_id=WA_NUMBER, source_channel_client=STRANGER).id

    result = await share_service.create_share(AGENT, "w.txt", execution_id=turn, actor_is_agent=True)

    assert _row(result["file_id"])["addressed_to_email"] == STRANGER
    assert result["visible_to_requester"] is None
    assert result["visibility_note"] is None


# --------------------------------------------------------------------------- #
# 1b. The slot for a platform-injected id (#2392)
# --------------------------------------------------------------------------- #
# Nothing passes `platform_execution_id` today — every headless turn's process
# already carries `TRINITY_EXECUTION_ID`, and the day that reaches the backend it
# answers "which turn?" with no cooperation from the model. A parameter nothing
# exercises is how a slot rots, so its contract is pinned now.

@pytest.mark.asyncio
async def test_a_platform_injected_id_needs_no_cooperation_from_the_model(share_service):
    turn = _turn(client=ADA)

    result = await share_service.create_share(          # the model cited nothing at all
        AGENT, "injected.txt", actor_is_agent=True, platform_execution_id=turn)

    assert _files_tab(ADA) == {"injected.txt"}
    assert _row(result["file_id"])["audience_source"] == "turn"


@pytest.mark.asyncio
async def test_a_platform_injected_id_outranks_the_one_the_model_typed(share_service):
    adas = _turn(client=ADA, chat="sess-ada")
    bobs = _turn(client=BOB, chat="sess-bob")

    await share_service.create_share(
        AGENT, "whose.txt", actor_is_agent=True, execution_id=bobs, platform_execution_id=adas)

    assert _files_tab(ADA) == {"whose.txt"} and _files_tab(BOB) == set()


@pytest.mark.asyncio
async def test_a_platform_injected_id_is_still_bound_to_the_calling_agent(share_service):
    """Platform-injected means robust against an honest model's mistakes — the header
    still arrives from the agent's container. Another agent's execution is not
    this agent's turn, whoever says so; the evidence rule then decides."""
    foreign = _turn(agent=OTHER_AGENT, client=ADA)

    result = await share_service.create_share(
        AGENT, "x.txt", actor_is_agent=True, platform_execution_id=foreign)

    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "ambiguous"


@pytest.mark.asyncio
async def test_a_platform_injected_id_means_nothing_from_a_human_caller(share_service):
    turn = _turn(client=ADA)

    result = await share_service.create_share(
        AGENT, "h.txt", actor_is_agent=False, platform_execution_id=turn)

    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "none"


# --------------------------------------------------------------------------- #
# 2. The override — an agent may name a rostered person, and only one
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_agent_may_address_a_different_rostered_person(share_service):
    turn = _turn(client=ADA)

    result = await share_service.create_share(
        AGENT, "for-bob.pdf", execution_id=turn, actor_is_agent=True, audience_email=BOB)

    assert _files_tab(BOB) == {"for-bob.pdf"}
    assert _files_tab(ADA) == set()
    assert _row(result["file_id"])["audience_source"] == "override"
    assert result["addressed_to"] == BOB


@pytest.mark.asyncio
async def test_an_off_roster_address_is_refused_by_name_and_nothing_is_stored(share_service):
    from fastapi import HTTPException
    from database import db as core_db
    turn = _turn(client=ADA)

    with pytest.raises(HTTPException) as exc:
        await share_service.create_share(
            AGENT, "leak.pdf", execution_id=turn, actor_is_agent=True, audience_email=STRANGER)

    assert exc.value.status_code == 400
    assert "AUDIENCE_NOT_ON_ROSTER" in exc.value.detail
    assert core_db.list_active_shared_files_for_agent(AGENT) == []


@pytest.mark.asyncio
async def test_an_unreadable_roster_refuses_rather_than_sharing(share_service, monkeypatch):
    from fastapi import HTTPException
    from client_portal import service as portal
    from database import db as core_db

    def boom(*a, **kw):
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr(portal, "agent_on_roster", boom)

    with pytest.raises(HTTPException) as exc:
        await share_service.create_share(AGENT, "x.pdf", actor_is_agent=True, audience_email=BOB)

    assert exc.value.status_code == 503
    assert core_db.list_active_shared_files_for_agent(AGENT) == []


@pytest.mark.asyncio
async def test_the_owner_can_be_named_too(share_service):
    """The reader shows an owner the files addressed to them, so refusing the
    owner's address would make the one person who can always see the file the
    only one who cannot be named."""
    await share_service.create_share(AGENT, "to-owner.txt", actor_is_agent=True, audience_email=OWNER)

    assert _files_tab(OWNER, platform=True) == {"to-owner.txt"}


@pytest.mark.asyncio
async def test_readdressing_the_same_file_in_one_turn_is_a_second_share_not_a_replay(share_service):
    """The effect key was {filename, content}: the second call REPLAYED the
    first snapshot, reported success, and Bob's row never existed."""
    turn = _turn(client=ADA)

    first = await share_service.create_share(
        AGENT, "deck.pdf", execution_id=turn, actor_is_agent=True)
    second = await share_service.create_share(
        AGENT, "deck.pdf", execution_id=turn, actor_is_agent=True, audience_email=BOB)

    assert first["file_id"] != second["file_id"]
    assert _files_tab(ADA) == {"deck.pdf"} and _files_tab(BOB) == {"deck.pdf"}


@pytest.mark.asyncio
async def test_the_idempotency_table_never_holds_an_address(share_service, audience_db):
    """The effect key hashes its args, and the replay snapshot is stored WITHOUT
    the echo — so an address an agent typed does not sit in `idempotency_keys`
    for a day. The replay still returns it: `audience_email` is part of the key,
    so the replaying call carries the same address by construction."""
    from db.tables import idempotency_keys
    from sqlalchemy import select
    turn = _turn(client=ADA)

    first = await share_service.create_share(
        AGENT, "deck.pdf", execution_id=turn, actor_is_agent=True, audience_email=BOB)
    replay = await share_service.create_share(
        AGENT, "deck.pdf", execution_id=turn, actor_is_agent=True, audience_email=BOB)

    assert replay["file_id"] == first["file_id"]            # it WAS a replay
    assert first["addressed_to"] == BOB and replay["addressed_to"] == BOB
    with audience_db.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(idempotency_keys)).mappings()]
    assert rows, "the guard stored nothing — this test would pass vacuously"
    assert all(BOB not in str(v) for row in rows for v in row.values())


@pytest.mark.asyncio
async def test_a_rerun_of_the_same_turn_still_replays(share_service):
    """#1084 is untouched: same turn, same file, same person ⇒ the original URL."""
    turn = _turn(client=ADA)

    first = await share_service.create_share(AGENT, "once.pdf", execution_id=turn, actor_is_agent=True)
    again = await share_service.create_share(AGENT, "once.pdf", execution_id=turn, actor_is_agent=True)

    assert first["file_id"] == again["file_id"]
    assert first["url"] == again["url"]


# --------------------------------------------------------------------------- #
# 3. Existing rows, and the reader's own edges
# --------------------------------------------------------------------------- #

def test_a_row_from_before_the_column_is_the_owners_and_no_clients(audience_db, monkeypatch):
    """AC6. Its recipient is unknowable; it expires within seven days."""
    from client_portal import service as portal
    from db.tables import agent_shared_files
    from sqlalchemy import insert
    monkeypatch.setattr(portal, "get_portal_base_url", lambda: "")
    with audience_db.begin() as conn:
        conn.execute(insert(agent_shared_files).values(
            id="legacy", agent_name=AGENT, filename="old.pdf", stored_filename="legacy",
            size_bytes=1, mime_type="application/pdf", download_token="tok-legacy",
            created_by=AGENT, created_at="t", expires_at="2099-01-01T00:00:00Z",
            download_count=0))

    assert _files_tab(ADA) == set()
    assert _files_tab(OWNER, platform=True) == {"old.pdf"}


def test_an_unidentifiable_viewer_matches_nothing_rather_than_everything(share_service):
    """`column == None` compiles to `IS NULL`, so an unguarded comparison hands a
    caller nobody could identify EVERY unaddressed row — the owner's files. Rows
    of both kinds are seeded first: against an empty table this passes whatever
    the query says (it did, until a mutation run showed it)."""
    from database import db as core_db
    share_service.create_share_from_bytes(AGENT, b"x", display_name="owners.txt")
    share_service.create_share_from_bytes(AGENT, b"y", display_name="adas.txt", addressed_to_email=ADA)
    assert len(core_db.list_active_shared_files_for_agent(AGENT)) == 2

    assert core_db.list_active_shared_files_for_viewer(AGENT, "") == []
    assert core_db.list_active_shared_files_for_viewer(AGENT, None) == []


# --------------------------------------------------------------------------- #
# 4. The pure rule — every row of the issue's table
# --------------------------------------------------------------------------- #

class _Exec:
    def __init__(self, **kw):
        self.agent_name = AGENT
        self.status = "running"
        for k in ("source_channel", "source_channel_chat_id", "source_channel_client",
                  "source_channel_agent", "source_user_email", "triggered_by"):
            setattr(self, k, kw.get(k))


@pytest.mark.parametrize("fields, email, channel, source", [
    # a Workspace chat with Ada → Ada
    (dict(source_channel="portal", source_channel_client=ADA), ADA, None, "turn"),
    # a room turn sent by Ada → Ada
    (dict(source_channel="room", source_channel_client=ADA), ADA, None, "turn"),
    # a portal row from before `source_channel_client` existed → fail closed
    (dict(source_channel="portal", source_user_email=ADA), None, None, "none"),
    # a verified speaker in a one-to-one WhatsApp chat — the router stamped her
    (dict(source_channel="whatsapp", source_channel_chat_id=WA_NUMBER, source_user_email=ADA,
          source_channel_client=ADA), ADA, WA_NUMBER, "turn"),
    # a GROUP turn: `source_user_email` is the UNLOCKER's address, never the speaker's
    (dict(source_channel="telegram", source_channel_chat_id="-100123", source_user_email=ADA),
     None, "telegram:-100123", "turn"),
    # an unverified Slack user — the channel-native id is NOT an email
    (dict(source_channel="slack", source_channel_chat_id="C012", source_user_email="slack:T1:U9"),
     None, "slack:C012", "turn"),
    # …not even if a channel-native id somehow reached the stamped column
    (dict(source_channel="slack", source_channel_chat_id="C012", source_channel_client="slack:T1:U9"),
     None, "slack:C012", "turn"),
    # schedule / operator chat / mcp / loop — no person
    (dict(triggered_by="schedule"), None, None, "none"),
    (dict(triggered_by="chat", source_user_email=OWNER), None, None, "none"),
    # a voice post-session turn stamps no source_channel
    (dict(triggered_by="voice", source_user_email=ADA), None, None, "none"),
    # a channel nobody has taught this table about makes NO claim (allow-list)
    (dict(source_channel="carrier-pigeon", source_channel_client=ADA), None, None, "none"),
    # inherited context (agent-to-agent child) → the owner only
    (dict(source_channel="portal", source_channel_client=ADA, source_channel_agent=OTHER_AGENT),
     None, None, "none"),
    # …including an agent tasking ITSELF, and A→B→A: the column names the executing agent
    (dict(source_channel="portal", source_channel_client=ADA, source_channel_agent=AGENT),
     None, None, "none"),
    (dict(source_channel="room", source_channel_client=BOB, source_channel_agent=AGENT),
     None, None, "none"),
])
def test_who_a_turn_was_for(fields, email, channel, source):
    from services.turn_audience import audience_of
    got = audience_of(_Exec(**fields))
    assert (got.email, got.channel, got.source) == (email, channel, source)


def test_no_execution_is_no_person():
    from services.turn_audience import audience_of
    got = audience_of(None)
    assert (got.email, got.channel, got.source) == (None, None, "none")


@pytest.mark.parametrize("raw, expected", [
    ("  Ada@Example.COM ", ADA),
    (ADA, ADA),
    ("", None), (None, None), ("   ", None),
    ("telegram:9001:424242", None),
    ("slack:T1:U9", None),
    ("not an email", None),
    ("two words@example.com", None),
])
def test_one_normaliser_for_every_writer_and_the_reader(raw, expected):
    from services.turn_audience import normalize_addressee_email
    assert normalize_addressee_email(raw) == expected


def _both_audience_models(raw):
    """The two request models that carry an `audience_email` — the ent365
    minimal `ReportCreate` constructor and the share request."""
    from models import ReportCreate, ShareFileMcpRequest
    return (
        lambda: ShareFileMcpRequest(filename="x.txt", audience_email=raw),
        lambda: ReportCreate(report_type="recon.leads", title="Leads",
                             payload={"rows": []}, audience_email=raw),
    )


@pytest.mark.parametrize("raw", [
    "  Ada@Example.COM ",          # case + surrounding spaces
    "\tada@example.com\t",         # surrounding tabs — strip() takes them
    ADA,
    "a@b@c.com",
    "no-at",
    "two words@example.com",
    "a\tb@example.com",            # interior tab — `isspace`, not `" "`
    "@example.com",                # empty local part
    "user@",                       # empty domain
])
def test_the_validator_and_the_resolver_agree_on_every_addressee(raw):
    """#2955 — one rule, two callers. The expectation is AGREEMENT, computed
    from the resolver, never a literal: the validator raises exactly where the
    resolver answers None for a non-blank input, and returns the resolver's
    value everywhere else. (AC4 forbids changing the resolver now; it should
    not forbid tightening it later, so `a@b@c.com` is not promoted to a
    promise here — the resolver's own decisions stay pinned above.)"""
    from pydantic import ValidationError
    from services.turn_audience import normalize_addressee_email

    expected = normalize_addressee_email(raw)
    for build in _both_audience_models(raw):
        if expected is None:
            with pytest.raises(ValidationError) as exc:
                build()
            assert "audience_email must be an email address" in str(exc.value)
        else:
            assert build().audience_email == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_blank_is_absent_at_every_boundary(raw):
    """Regression pin (green before #2955, by design — not part of the red
    set): "absent" has one spelling. Blank never reaches the raise at either
    boundary, and the resolver says None for it and for a non-`str`."""
    from services.turn_audience import normalize_addressee_email
    for build in _both_audience_models(raw):
        assert build().audience_email is None
    assert normalize_addressee_email(raw) is None
    assert normalize_addressee_email(42) is None      # resolver only — a validator never sees a non-str


def test_the_one_normaliser_has_one_home():
    """#2955 — the rule is defined ONCE, in a leaf, and both validators call
    it through one boundary wrapper. AST, not a line scan: a re-inlined
    `not "@" in v` passes a text grep and fails this."""
    import ast
    import os
    import models
    import utils.addressee
    import services.turn_audience

    # (i) a re-export, never a second copy (Invariant #1(a))
    assert services.turn_audience.normalize_addressee_email is utils.addressee.normalize_addressee_email

    # (ii) the leaf stays a leaf: `typing` is its only import
    leaf = ast.parse(open(utils.addressee.__file__).read())
    imported = set()
    for node in ast.walk(leaf):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"typing"}, imported

    # (iii) models.py still imports no `services.*` (the contract module must
    # not need the runtime config to validate a request)
    tree = ast.parse(open(models.__file__).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("services"), ast.dump(node)
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("services") for a in node.names), ast.dump(node)

    # (iv) both `_normalize_audience` bodies: one call to the wrapper, zero comparisons
    bodies = {}
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name in {"ReportCreate", "ShareFileMcpRequest"}:
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "_normalize_audience":
                    bodies[cls.name] = fn
    assert set(bodies) == {"ReportCreate", "ShareFileMcpRequest"}
    for name, fn in bodies.items():
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "_validate_audience_email"]
        compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
        assert len(calls) == 1, (name, len(calls))
        assert compares == [], (name, [ast.dump(c) for c in compares])


@pytest.mark.parametrize("channel, chat_id, expected", [
    ("whatsapp", WA_NUMBER, WA_NUMBER),                 # Twilio's `From` is already prefixed
    ("telegram", "424242", "telegram:424242"),
    ("slack", "C012", "slack:C012"),
    ("telegram", None, None), ("telegram", "", None),
])
def test_a_channel_address_is_prefixed_exactly_once(channel, chat_id, expected):
    from services.turn_audience import channel_address
    assert channel_address(channel, chat_id) == expected


# --------------------------------------------------------------------------- #
# 5. The owner's list — driven through the ROUTE, both fields
# --------------------------------------------------------------------------- #

class _Principal:
    def __init__(self, **kw):
        self.username = "alice"
        self.role = "creator"
        self.id = 1
        self.agent_name = kw.pop("agent_name", None)
        for k, v in kw.items():
            setattr(self, k, v)


# Every field of an owner-list row, classified (#2955). The withholding tests
# compare a key row to the JWT row with the audience fields nulled — which on
# its own would let a FOURTH audience field leak (it is equal in both rows).
# Pinning the key set turns a new `SharedFileInfo` field red here, so whoever
# adds it has to say which side of the line it is on.
_AUDIENCE_FIELDS = {"addressed_to", "addressed_to_channel", "audience_source"}
_OWNER_ROW_FIELDS = {
    "file_id", "filename", "size_bytes", "mime_type", "url", "created_at",
    "expires_at", "download_count", "last_downloaded_at",
} | _AUDIENCE_FIELDS


async def _owner_list(principal, monkeypatch):
    from routers import agent_files
    monkeypatch.setattr(agent_files, "assert_agent_owner", lambda *a, **kw: None)
    listing = await agent_files.list_agent_shared_files(AGENT, current_user=principal)
    return [f.model_dump() for f in listing.files]


@pytest.mark.asyncio
async def test_the_owner_sees_who_each_file_is_for(share_service, monkeypatch):
    """AC4. A JWT human (`mcp_scope is None`) is the UI."""
    share_service.create_share_from_bytes(
        AGENT, b"x", display_name="v.ogg",
        addressed_to_email=BOB, addressed_to_channel=WA_NUMBER)

    [row] = await _owner_list(_Principal(mcp_scope=None), monkeypatch)

    assert row["addressed_to"] == BOB
    assert row["addressed_to_channel"] == WA_NUMBER
    assert row["audience_source"] == "channel"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["agent", "user", "system", "connector", "a-scope-invented-tomorrow"])
async def test_a_key_authenticated_caller_is_told_nobodys_address(share_service, monkeypatch, scope):
    """An agent-scoped key resolves to its OWNER and passes the owner gate on
    this route, so without the strip any agent could read who every file was for
    — an email AND a phone number — nor whether it was addressed at all (#2955).
    Allow-list, as `is_interactive_principal` is.

    The WHOLE row is compared against what the JWT sees, with the three
    audience fields nulled, and the row's key set is pinned
    (`_OWNER_ROW_FIELDS`): a fourth audience field added to `SharedFileInfo`
    tomorrow turns this red and has to be classified, instead of leaking to
    every key while a hand-written list of `is None`s stays green."""
    share_service.create_share_from_bytes(
        AGENT, b"x", display_name="v.ogg",
        addressed_to_email=BOB, addressed_to_channel=WA_NUMBER)

    [jwt_row] = await _owner_list(_Principal(mcp_scope=None), monkeypatch)
    [key_row] = await _owner_list(_Principal(mcp_scope=scope, agent_name=AGENT), monkeypatch)

    assert set(jwt_row) == _OWNER_ROW_FIELDS, set(jwt_row) ^ _OWNER_ROW_FIELDS
    assert all(jwt_row[f] is not None for f in _AUDIENCE_FIELDS)   # not vacuous
    assert key_row == {
        **jwt_row,
        "addressed_to": None,
        "addressed_to_channel": None,
        "audience_source": None,
    }
    assert key_row["filename"] == "v.ogg"              # the row itself is still theirs to see


@pytest.mark.asyncio
async def test_a_principal_with_no_scope_attribute_fails_closed(share_service, monkeypatch):
    share_service.create_share_from_bytes(
        AGENT, b"x", display_name="v.ogg", addressed_to_email=BOB)

    [jwt_row] = await _owner_list(_Principal(mcp_scope=None), monkeypatch)
    [row] = await _owner_list(_Principal(), monkeypatch)       # no `mcp_scope` at all

    assert set(jwt_row) == _OWNER_ROW_FIELDS, set(jwt_row) ^ _OWNER_ROW_FIELDS
    assert jwt_row["addressed_to"] == BOB and jwt_row["audience_source"] is not None
    assert row == {
        **jwt_row,
        "addressed_to": None,
        "addressed_to_channel": None,
        "audience_source": None,
    }


# --------------------------------------------------------------------------- #
# 6. The two platform-side callers — through their CALL SITES
# --------------------------------------------------------------------------- #
# The service-level tests above prove `create_share_from_bytes` stores what it is
# given. These prove the adapter and the voice path GIVE it: drop the recipient
# at either call site and the media is back in nobody's-or-everybody's tab with
# every other test here still green.

def _wa_binding():
    return {"id": 7, "account_sid": "AC0000", "from_number": "whatsapp:+15555550100",
            "messaging_service_sid": None}


@pytest.mark.asyncio
async def test_whatsapp_media_is_addressed_to_the_number_the_reply_goes_to(share_service, monkeypatch):
    from unittest.mock import AsyncMock
    import adapters.whatsapp_adapter as wa
    from adapters.base import ChannelResponse, OutboundFile
    from database import db as core_db

    monkeypatch.setattr(core_db, "get_whatsapp_binding", lambda agent: _wa_binding())
    monkeypatch.setattr(core_db, "get_whatsapp_verified_email",
                        lambda binding_id, phone: BOB if (binding_id, phone) == (7, WA_NUMBER) else None)
    monkeypatch.setattr(wa.WhatsAppAdapter, "_send_message", AsyncMock(return_value={"sid": "SM1"}))

    await wa.WhatsAppAdapter().send_response(WA_NUMBER, ChannelResponse(
        text="here you go",
        files=[OutboundFile(filename="export.csv", content=b"a,b\n1,2\n", language="csv")],
        metadata={"bot_token": "AC0000:authtoken", "agent_name": AGENT},
    ))

    assert _files_tab(BOB) == {"export.csv"}
    assert _files_tab(ADA) == set()
    [row] = core_db.list_active_shared_files_for_agent(AGENT)
    assert (row["addressed_to_email"], row["addressed_to_channel"], row["audience_source"]) == (
        BOB, WA_NUMBER, "channel")


@pytest.mark.asyncio
async def test_a_whatsapp_voice_note_is_addressed_to_its_listener(share_service, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import adapters.whatsapp_adapter as wa
    from services import voice_reply_service
    from database import db as core_db

    monkeypatch.setattr(core_db, "get_whatsapp_binding", lambda agent: _wa_binding())
    monkeypatch.setattr(core_db, "get_whatsapp_auth_token", lambda agent: "authtoken")
    monkeypatch.setattr(core_db, "get_whatsapp_verified_email", lambda binding_id, phone: None)
    monkeypatch.setattr(wa.WhatsAppAdapter, "_send_message", AsyncMock(return_value={"sid": "SM1"}))
    tts = SimpleNamespace(synthesize_voice_note=AsyncMock(return_value=b"OggS" + b"\x00" * 64))

    await voice_reply_service._deliver_whatsapp(AGENT, WA_NUMBER, "hello", "voice-1", tts)

    # An unverified number: the note is in NOBODY's Files tab — not Ada's, not
    # Bob's, not the owner's — and the owner's panel names the number.
    assert _files_tab(ADA) == set() and _files_tab(BOB) == set()
    assert _files_tab(OWNER, platform=True) == set()
    [row] = core_db.list_active_shared_files_for_agent(AGENT)
    assert (row["addressed_to_email"], row["addressed_to_channel"]) == (None, WA_NUMBER)


@pytest.mark.asyncio
async def test_the_share_route_tells_the_service_whether_the_caller_is_the_agent(share_service, monkeypatch):
    """The route is where `actor_is_agent` is decided, and it is the whole
    difference between "a turn" and "a person with an API key"."""
    from routers import agent_files
    from models import ShareFileMcpRequest
    monkeypatch.setattr(agent_files, "assert_agent_owner", lambda *a, **kw: None)
    turn = _turn(client=ADA)

    # the agent's own key, in Ada's turn
    await agent_files.share_agent_file(
        AGENT, ShareFileMcpRequest(filename="a.txt", execution_id=turn),
        current_user=_Principal(mcp_scope="agent", agent_name=AGENT))
    # the owner, by hand, citing the very same turn
    await agent_files.share_agent_file(
        AGENT, ShareFileMcpRequest(filename="b.txt", execution_id=turn),
        current_user=_Principal(mcp_scope=None))

    assert _files_tab(ADA) == {"a.txt"}


@pytest.mark.asyncio
async def test_the_share_route_carries_the_override_to_the_service(share_service, monkeypatch):
    from routers import agent_files
    from models import ShareFileMcpRequest
    monkeypatch.setattr(agent_files, "assert_agent_owner", lambda *a, **kw: None)

    response = await agent_files.share_agent_file(
        AGENT, ShareFileMcpRequest(filename="c.txt", audience_email="  Bob@Example.com "),
        current_user=_Principal(mcp_scope="agent", agent_name=AGENT))

    assert _files_tab(BOB) == {"c.txt"}
    assert response.addressed_to == BOB


def test_a_malformed_override_is_refused_at_the_boundary_by_name():
    from pydantic import ValidationError
    from models import ShareFileMcpRequest
    with pytest.raises(ValidationError) as exc:
        ShareFileMcpRequest(filename="x.txt", audience_email="not-an-address")
    assert "audience_email must be an email address" in str(exc.value)
    assert ShareFileMcpRequest(filename="x.txt", audience_email="   ").audience_email is None


# --------------------------------------------------------------------------- #
# 7. Both migration tracks (Invariant #9) — run, not read
# --------------------------------------------------------------------------- #

_AUDIENCE_COLUMNS = {"addressed_to_email", "addressed_to_channel", "audience_source"}

_TABLE_BEFORE_THE_COLUMNS = """
    CREATE TABLE agent_shared_files (
        id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, filename TEXT NOT NULL,
        stored_filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, mime_type TEXT,
        download_token TEXT UNIQUE NOT NULL, created_by TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
        one_time INTEGER DEFAULT 0, consumed_at TEXT, download_count INTEGER DEFAULT 0,
        last_downloaded_at TEXT
    )
"""


def test_an_existing_install_upgrades_and_its_rows_become_the_owners(tmp_path):
    """AC6, on the SQLite track: the real migration function over a table of the
    old shape with a row in it. Nullable, no default, no backfill — and running
    it twice is a no-op (two workers boot at once: #456)."""
    import sqlite3
    from db import migrations

    conn = sqlite3.connect(tmp_path / "legacy.db")
    cur = conn.cursor()
    cur.execute(_TABLE_BEFORE_THE_COLUMNS)
    cur.execute("INSERT INTO agent_shared_files (id, agent_name, filename, stored_filename, size_bytes,"
                " download_token, created_by, created_at, expires_at) VALUES"
                " ('old', 'atlas', 'old.pdf', 'old', 1, 'tok', 'atlas', 't', 't')")
    conn.commit()

    migrations._migrate_agent_shared_files_audience(cur, conn)
    migrations._migrate_agent_shared_files_audience(cur, conn)      # idempotent

    columns = {r[1] for r in cur.execute("PRAGMA table_info(agent_shared_files)")}
    assert _AUDIENCE_COLUMNS <= columns
    assert cur.execute("SELECT addressed_to_email, addressed_to_channel, audience_source"
                       " FROM agent_shared_files WHERE id='old'").fetchone() == (None, None, None)


def test_a_fresh_install_and_the_orm_agree_with_the_migration():
    """Three sources of truth for one table: the migration (upgrades), the DDL
    (`schema.py`, fresh installs) and the metadata (`tables.py`, every query)."""
    from db import migrations, schema
    from db.tables import agent_shared_files

    assert any(name == "agent_shared_files_audience" for name, _fn in migrations.MIGRATIONS)
    assert all(f"{c} TEXT" in schema.TABLES["agent_shared_files"] for c in _AUDIENCE_COLUMNS)
    assert _AUDIENCE_COLUMNS <= set(agent_shared_files.c.keys())


def test_the_postgres_revision_adds_the_same_three_columns(monkeypatch):
    """The PostgreSQL track is Alembic's, and SQLite never runs it — so this runs
    the revision's own `upgrade()` against a recording `op`. Its place on the
    version line is `scripts/ci/check_alembic_heads.py`'s to guard, not a string
    pinned here: that parent moves whenever another revision lands first."""
    import importlib.util
    import sys
    import types
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "src" / "backend" / "migrations" / "versions"
    [path] = list(versions.glob("*_agent_shared_files_audience.py"))

    executed = []
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = types.SimpleNamespace(execute=executed.append)
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)
    spec = importlib.util.spec_from_file_location("_rev_ent549", path)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)

    rev.upgrade()

    assert rev.revision == path.stem
    added = {sql.split("ADD COLUMN IF NOT EXISTS ")[1].split()[0] for sql in executed}
    assert added == _AUDIENCE_COLUMNS
    assert all(sql.startswith("ALTER TABLE agent_shared_files ") and sql.endswith(" TEXT") for sql in executed)



# --------------------------------------------------------------------------- #
# 8. The channel router states who a turn is for — and only when it knows
# --------------------------------------------------------------------------- #

async def _router_stamp(monkeypatch, *, verified_email, is_group):
    """Drive the router's real `_run_agent_task` and return what it told
    `execute_task` about this turn."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    import adapters.message_router as mr

    execute = AsyncMock(return_value=SimpleNamespace(
        status="success", response="ok", execution_id="exec-1", error=None, cost=0.0))
    monkeypatch.setattr(mr, "get_task_execution_service", lambda: MagicMock(execute_task=execute))
    monkeypatch.setattr(mr, "build_public_channel_caller_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "build_voice_capability_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "_get_channel_allowed_tools", lambda: ["WebSearch"])
    monkeypatch.setattr(mr.db, "get_public_channel_model", lambda agent: None)
    monkeypatch.setattr(mr.db, "get_or_create_public_user_memory", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "format_user_memory_block", lambda m: None)

    adapter = MagicMock()
    adapter.get_source_identifier.return_value = "telegram:9001:424242"
    message = SimpleNamespace(channel_id="424242", thread_id=None, files=[], metadata={})
    router = mr.ChannelMessageRouter.__new__(mr.ChannelMessageRouter)

    await router._run_agent_task(
        adapter, message, AGENT, "bot-token", "telegram", is_group,
        container=None, upload_dir=None, context_prompt="hi",
        verified_email=verified_email, image_data=[])
    return execute.await_args.kwargs


@pytest.mark.asyncio
async def test_the_router_names_a_verified_speaker_in_a_one_to_one_chat(monkeypatch):
    kwargs = await _router_stamp(monkeypatch, verified_email=ADA, is_group=False)
    assert kwargs["source_channel_client"] == ADA


@pytest.mark.asyncio
async def test_the_router_names_nobody_in_a_group_even_though_an_email_is_verified(monkeypatch):
    kwargs = await _router_stamp(monkeypatch, verified_email=ADA, is_group=True)
    assert kwargs["source_channel_client"] is None
    assert kwargs["source_user_email"] == ADA            # unchanged: MEM-001 / attribution still read it


@pytest.mark.asyncio
async def test_the_router_names_nobody_for_an_unverified_user(monkeypatch):
    kwargs = await _router_stamp(monkeypatch, verified_email=None, is_group=False)
    assert kwargs["source_channel_client"] is None
    assert kwargs["source_user_email"] == "telegram:9001:424242"

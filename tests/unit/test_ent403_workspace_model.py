"""ent#403 — the Workspace composer's model dropdown.

Five of these tests exist because a green suite would otherwise have shipped a
feature that lies about itself:

  * `model_used` is written ONLY at row creation, and BOTH portal turn paths
    pre-create the row. A model threaded as a turn kwarg alone reaches the agent
    and never reaches the row a client can see — AC 7 half-done, silently. That
    is the #2426 class one column over, so it gets the same treatment: assert
    the PERSISTED KWARGS, at both sites, not that a value was "passed".
  * One turn can produce TWO rows. `run_resumable_turn`'s cold retry strips
    `execution_id` and re-runs, so `execute_task` creates a second row and
    stamps it from the forwarded `model`. The two must agree.
  * `""` is the control's own default-option value, so a validator that runs
    before normalisation 422s EVERY default turn on day one.
  * The #894 rung now applies to a non-platform principal too — a deliberate
    behaviour change, pinned so a later "surely this should be platform-only"
    has to argue with a test instead of quietly reverting it.
  * The AUTH/BILLING copy must NOT change when a model was chosen. There is no
    "model unavailable" code in the #2320 ladder, so rewording that branch would
    blame the model for an exhausted subscription.

PATCHING RULE — the same one `test_2196_roster_availability.py` states, and for
the same reason: `client_portal.service` reaches its dependencies through
function-local `from services.x import y`, which resolves `sys.modules` at call
time. Patch the seam on `client_portal.service`, or take the module object out
of `sys.modules`; never `monkeypatch.setattr("services.x.y", ...)`.
"""
from __future__ import annotations

import asyncio
import types

import pytest

pytestmark = pytest.mark.unit

AGENT = "scout"
EMAIL = "bob@example.com"
SESSION = "ps_1"
OPUS = "claude-opus-5"
HAIKU = "claude-haiku-4-5-20251001"
# public-channel-selectable but deliberately NOT workspace-selectable — the one
# id that proves "the inherited value is not re-laundered through the composer's
# narrower allow-list".
LEGACY_PUBLIC = "claude-opus-4-7"


def _services_module(name: str):
    import importlib
    import sys

    importlib.import_module(f"services.{name}")
    return sys.modules[f"services.{name}"]


def _run(coro):
    return asyncio.run(coro)


def _platform_default(monkeypatch, model_id: str):
    """Pin `settings_service.get_platform_default_model` — the ladder's LAST rung
    since the 2026-09-08 review, so a test about the first two must fix it or it
    reads a real setting."""
    monkeypatch.setattr(_services_module("settings_service"),
                        "get_platform_default_model", lambda: model_id, raising=False)


@pytest.fixture()
def svc():
    from client_portal import service as m
    return m


# ---------------------------------------------------------------------------
# 1. The ladder
# ---------------------------------------------------------------------------

def test_the_ladder_prefers_the_users_pick_then_the_agents_override(svc, monkeypatch):
    """requested → `public_channel_model` → the PLATFORM DEFAULT.

    The last rung resolves to a concrete id rather than stopping at `None`
    (review, 2026-09-08). `model_used` is written only at row creation and both
    portal paths pre-create the row, so `execute_task`'s own stamp never runs
    here — a `None` last rung recorded NULL on the commonest turn there is while
    the turn ran on the platform default a moment later. Same function
    (`settings_service.get_platform_default_model`), so the row and the turn
    agree by construction instead of holding two opinions.
    """
    import database

    _platform_default(monkeypatch, "claude-sonnet-4-6")
    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: HAIKU,
                        raising=False)
    assert svc.resolve_turn_model(AGENT, OPUS) == OPUS      # the pick wins
    assert svc.resolve_turn_model(AGENT, None) == HAIKU     # then the #894 override

    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: None,
                        raising=False)
    # …then the platform default, as a CONCRETE id — this is what lands on the row.
    assert svc.resolve_turn_model(AGENT, None) == "claude-sonnet-4-6"


def test_a_failing_override_read_never_fails_the_turn(svc, monkeypatch):
    """The override is a preference, not a gate — a DB hiccup falls THROUGH to
    the platform default rather than refusing a turn the user is waiting on, and
    rather than short-circuiting the last rung with it."""
    import database

    def _boom(agent_name):
        raise RuntimeError("db down")

    _platform_default(monkeypatch, "claude-sonnet-4-6")
    monkeypatch.setattr(database.db, "get_public_channel_model", _boom, raising=False)
    assert svc.resolve_turn_model(AGENT, None) == "claude-sonnet-4-6"
    # …and an explicit pick is not lost to someone else's failure.
    assert svc.resolve_turn_model(AGENT, OPUS) == OPUS


def test_an_unreadable_platform_default_degrades_to_none_never_a_failed_turn(svc, monkeypatch):
    """The last rung is best-effort too. Degrading to `None` is the pre-ent#403
    behaviour — the row goes back to NULL and `execute_task` resolves the
    default itself. Worse than a stamped row, better than a refused turn."""
    import database

    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: None,
                        raising=False)

    def _boom():
        raise RuntimeError("settings down")

    monkeypatch.setattr(_services_module("settings_service"),
                        "get_platform_default_model", _boom, raising=False)
    assert svc.resolve_turn_model(AGENT, None) is None


def test_a_non_platform_principal_resolves_the_same_ladder(svc, monkeypatch):
    """THE deliberate behaviour change (ent#403 §D6), pinned.

    The #894 rung applies to EVERY portal turn — an external portal-token client
    and a headless ent#83 consumer included — not only a platform user's. The
    issue calls its absence the defect: a Workspace turn dispatches as
    `triggered_by="public"`, so the owner's public-channel override was expected
    here and never applied.

    Gating the rung on the principal would leave the streaming route and the
    synchronous route resolving differently, which is exactly the "two sources
    silently disagree" AC 5 exists to kill. `resolve_turn_model` takes no
    principal AT ALL, which is what makes that true by construction — so this
    test asserts the signature as well as the value.
    """
    import inspect
    import database

    _platform_default(monkeypatch, "claude-sonnet-4-6")
    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: HAIKU,
                        raising=False)
    assert svc.resolve_turn_model(AGENT, None) == HAIKU

    params = set(inspect.signature(svc.resolve_turn_model).parameters)
    assert params == {"agent_name", "requested"}, (
        "the ladder must not be able to see WHO is asking — a principal "
        "parameter here is how the two routes start disagreeing"
    )


# ---------------------------------------------------------------------------
# 2. Normalisation, authorisation, allow-list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_blank_means_inherit_and_is_normalised_before_validation(svc, raw):
    """The control's default option has value `""`. Validate the RAW field and
    every default turn 422s on day one — the whole reason normalisation is
    first. Whitespace is folded the same way, copying the #894 PUT's idiom."""
    assert svc.validate_requested_model(raw, is_platform=True) is None
    # And the same for a principal with no control: blank is not a choice, so
    # it must not trip the 403 either.
    assert svc.validate_requested_model(raw, is_platform=False) is None


def test_a_curated_model_is_accepted(svc):
    assert svc.validate_requested_model(OPUS, is_platform=True) == OPUS
    assert svc.validate_requested_model(f"  {OPUS}  ", is_platform=True) == OPUS


def test_an_uncurated_model_is_refused_422_with_a_string_detail(svc):
    """String, not a dict.

    `portalUtils.js::deliveryFailureReason` returns `detail` only when it is a
    string and degrades anything else to "The message wasn't delivered (error
    422)" — which would drop the model name, the reason and the remedy on the
    one surface this message exists for.
    """
    from client_portal.service import ClientPortalError

    with pytest.raises(ClientPortalError) as exc:
        svc.validate_requested_model("claude-opus-999-not-a-model", is_platform=True)

    assert exc.value.status_code == 422
    assert isinstance(exc.value.detail, str)
    assert "claude-opus-999-not-a-model" in exc.value.detail


def test_the_refused_value_is_bounded_before_it_is_echoed(svc):
    """The 422 REFLECTS the rejected value, so it must be bounded first.

    `PortalChatRequest.model` is deliberately unvalidated at the payload layer
    (a length rule there would refuse before the 403 that says this principal
    has no control at all), and every sibling field on that model IS bounded —
    `message` is `max_length=8000`. Without the truncation an authenticated
    caller posts a megabyte-long `model` and has it echoed verbatim into the
    error body.
    """
    from client_portal.service import ClientPortalError

    with pytest.raises(ClientPortalError) as exc:
        svc.validate_requested_model("x" * 100_000, is_platform=True)

    assert exc.value.status_code == 422
    # Bounded, and still says WHAT was refused rather than dropping it.
    assert len(exc.value.detail) < 200
    assert "xxxx" in exc.value.detail


def test_a_public_channel_only_model_is_still_refused_at_the_composer(svc):
    """The composer's allow-list is NARROWER than #894's, deliberately — a
    client-facing surface offering "Claude Opus 4.7 — Legacy" is the operator
    combobox this issue reacts against. An id may be inheritable and still not
    pickable."""
    from client_portal.service import ClientPortalError
    from services.model_catalog import PUBLIC_CHANNEL_MODELS, WORKSPACE_MODELS

    assert LEGACY_PUBLIC in PUBLIC_CHANNEL_MODELS and LEGACY_PUBLIC not in WORKSPACE_MODELS

    with pytest.raises(ClientPortalError) as exc:
        svc.validate_requested_model(LEGACY_PUBLIC, is_platform=True)
    assert exc.value.status_code == 422


def test_a_principal_without_the_control_is_refused_403_not_ignored(svc):
    """403 rather than a silent drop: ignoring the field would run the turn on
    something other than what was asked for and say nothing. The gate is
    `is_platform` — the SAME bit the roster renders the control on, so the door
    and the UI cannot disagree. A ent#163 delegated principal holds a portal
    session, so it arrives here `is_platform=False`."""
    from client_portal.service import ClientPortalError

    with pytest.raises(ClientPortalError) as exc:
        svc.validate_requested_model(OPUS, is_platform=False)
    assert exc.value.status_code == 403


def test_the_allow_list_is_a_closed_set_not_a_prefix_rule(svc):
    """The value reaches the agent as a `--model` argv element, so an arbitrary
    string is argv/flag-smuggling surface against the agent runtime — and the
    Workspace sends no model today, so this field CREATES that surface. A
    prefix or regex check would admit every one of these."""
    from client_portal.service import ClientPortalError

    for hostile in (f"{OPUS} --dangerously-skip-permissions", f"--{OPUS}",
                    f"{OPUS}\n--allowedTools=Bash", "claude-opus-5x",
                    f"{OPUS};rm -rf /", "claude-"):
        with pytest.raises(ClientPortalError) as exc:
            svc.validate_requested_model(hostile, is_platform=True)
        assert exc.value.status_code == 422, hostile


# ---------------------------------------------------------------------------
# 3. AC 7 — the row. BOTH creation sites.
# ---------------------------------------------------------------------------

def _fake_core_db(captured, monkeypatch, *, override=None):
    """Swap `database.db` for a recorder, WITHOUT poisoning a lazy importer.

    `services.settings_service` binds `db` at MODULE level (`from database
    import db`), so whichever test first imports it decides what that name
    points at for the rest of the session — and conftest's #762 restore skips a
    key that had no baseline, i.e. a module not yet loaded when conftest ran.
    First-importing it under the fake therefore leaves every LATER test reading
    settings through a stand-in with no `get_setting_value`, which surfaces as
    an empty `ModelContext` in an unrelated test three sections down. Loading it
    here, before the swap, is what keeps this file's own ordering honest.
    """
    import importlib

    importlib.import_module("services.settings_service")

    class _FakeDb:
        def get_agent_subscription_id(self, agent_name):
            return "sub_1"

        def get_public_channel_model(self, agent_name):
            return override

        def create_task_execution(self, **kwargs):
            captured.append(kwargs)
            return types.SimpleNamespace(id=f"exec_{len(captured)}")

    import database
    monkeypatch.setattr(database, "db", _FakeDb(), raising=False)


def test_the_sync_pre_creation_stamps_the_model_on_the_row(svc, monkeypatch):
    """AC 7, site 1 of 2.

    `execute_task` persists `model_used` only inside `if not execution_id:`, and
    this function has already made the row and handed the id over — so that
    branch never runs and the column stays NULL no matter what the turn ran on.
    Asserting the PERSISTED KWARGS is the point: a test that only proved the
    value was "passed to the turn" would have been green throughout the bug.
    """
    captured = []
    _fake_core_db(captured, monkeypatch)

    out = svc._precreate_sync_execution(AGENT, "hello", EMAIL, SESSION, OPUS)

    assert out == "exec_1"
    assert captured[0]["model_used"] == OPUS
    # The #2426 stamps are untouched by this change.
    assert captured[0]["triggered_by"] == "public"
    assert captured[0]["source_channel_chat_id"] == SESSION


def test_the_inherit_path_stamps_the_platform_default_not_null(svc, monkeypatch):
    """AC 7 on the COMMONEST turn there is: no explicit pick, no agent override.

    This test asserted the opposite until the 2026-09-08 review. The reasoning
    for NULL was that writing the platform default "records a value this layer
    did not decide" — but `resolve_turn_model` reads it from
    `settings_service.get_platform_default_model()`, the same function
    `execute_task:1044` calls, so it is that layer's decision either way. What
    NULL actually bought was a blank audit row: `model_used` is written ONLY at
    row creation, both portal paths pre-create the row, so `execute_task`'s own
    `model_used=model` (inside `if not execution_id:`) never runs for a portal
    turn. The default state of every agent therefore recorded nothing while the
    turn ran on the platform default — and the execution page AC 7 pairs with
    would have read blank for most Workspace turns.
    """
    import database

    captured = []
    _fake_core_db(captured, monkeypatch)
    _platform_default(monkeypatch, "claude-sonnet-4-6")
    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: None,
                        raising=False)

    resolved = svc.resolve_turn_model(AGENT, None)
    svc._precreate_sync_execution(AGENT, "hello", EMAIL, SESSION, resolved)
    assert captured[0]["model_used"] == "claude-sonnet-4-6"


def test_the_row_still_takes_none_when_the_ladder_could_not_resolve(svc, monkeypatch):
    """The stamp is whatever the ladder handed over, including `None`. The
    pre-creation site does not second-guess it — one resolution per turn is the
    whole #2426 lesson, and a second lookup here could disagree with the row's
    own turn."""
    captured = []
    _fake_core_db(captured, monkeypatch)

    svc._precreate_sync_execution(AGENT, "hello", EMAIL, SESSION, None)
    assert captured[0]["model_used"] is None


def test_the_streaming_dispatch_stamps_the_model_on_the_row(svc, monkeypatch):
    """AC 7, site 2 of 2 — the path the Workspace actually uses.

    Also pins the ORDER the plan is specific about: the model is resolved
    BEFORE the row is created, so the row and the turn carry the same value.
    """
    captured = []
    _fake_core_db(captured, monkeypatch, override=HAIKU)
    seen = {}

    async def _ready(name):
        return "ready"

    async def _fake_chat(agent_name, message, email, **kw):
        seen.update(kw)
        return {"response": "ok", "cost": 0.0, "session_id": SESSION}

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_agent_availability", _ready)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: s or SESSION)
    monkeypatch.setattr(svc, "mark_turn_inflight", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "clear_turn_inflight", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "clear_turn_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "portal_chat", _fake_chat)
    sts = _services_module("session_turn_service")
    monkeypatch.setattr(sts, "resolve_turn_timeout", lambda a: 600)

    async def _go():
        out = await svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION, model=OPUS)
        while svc._INFLIGHT_TURNS:
            await asyncio.sleep(0.01)
        return out

    _run(_go())

    assert captured[0]["model_used"] == OPUS
    assert captured[0]["triggered_by"] == "public"      # dossier trap 5
    # The trusted value travels WITH the pick, so `portal_chat` never re-resolves.
    assert seen["resolved_model"] == OPUS
    assert seen["model"] == OPUS


def test_an_inherited_override_reaches_the_row_too(svc, monkeypatch):
    """The rung, end to end: no pick, an owner override set, and the row records
    what the turn will actually run on — not NULL, and not the platform
    default."""
    captured = []
    _fake_core_db(captured, monkeypatch, override=LEGACY_PUBLIC)

    async def _ready(name):
        return "ready"

    async def _fake_chat(agent_name, message, email, **kw):
        return {"response": "ok", "cost": 0.0, "session_id": SESSION}

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_agent_availability", _ready)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: s or SESSION)
    monkeypatch.setattr(svc, "mark_turn_inflight", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "clear_turn_inflight", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "clear_turn_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "portal_chat", _fake_chat)
    sts = _services_module("session_turn_service")
    monkeypatch.setattr(sts, "resolve_turn_timeout", lambda a: 600)

    async def _go():
        out = await svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)
        while svc._INFLIGHT_TURNS:
            await asyncio.sleep(0.01)
        return out

    _run(_go())
    # An inherited value legitimately sits OUTSIDE the curated set — which is
    # why it must not be re-laundered through `validate_requested_model`.
    assert captured[0]["model_used"] == LEGACY_PUBLIC


def test_a_caller_with_a_row_and_a_pick_must_hand_over_what_it_stamped(svc, monkeypatch):
    """The `or` that would have made the row and the turn disagree.

    `resolved_model or resolve_turn_model(...)` would let a caller that already
    stamped a row silently RE-resolve — and the override can change between the
    two reads. This path fails loud instead; no request can reach it.
    """
    async def _ready(name):
        return "ready"

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_agent_availability", _ready)

    with pytest.raises(ValueError, match="resolved_model"):
        _run(svc.portal_chat(AGENT, "hi", EMAIL, session_id=SESSION,
                             execution_id="exec_x", model=OPUS))


# ---------------------------------------------------------------------------
# 3b. One turn, two rows (the cold retry)
# ---------------------------------------------------------------------------

def test_the_cold_retry_row_carries_the_same_model_as_the_first():
    """F1b — one turn can produce TWO rows.

    `run_resumable_turn` strips `execution_id` from the retry kwargs and re-runs,
    so `execute_task` CREATES a second row and stamps it from the forwarded
    `model`. If `model` were dropped from the retry the two rows would disagree
    about one turn — and the second, the one that actually answered, would be
    the wrong one.
    """
    sts = _services_module("session_turn_service")

    calls = []

    class _Result:
        status = "failed"
        error = "No conversation found with session ID: abc"
        session_id = None

    class _Ok:
        status = "success"
        error = None
        session_id = "uuid-2"

    class _Service:
        async def execute_task(self, **kw):
            calls.append(kw)
            return _Result() if len(calls) == 1 else _Ok()

    class _NoLock:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    # `get_task_execution_service` is imported function-locally inside
    # `run_resumable_turn`, so it resolves `sys.modules["services.task_execution_service"]`
    # at call time — patch it THERE, not as an attribute of this module.
    tes = _services_module("task_execution_service")
    originals = (tes.get_task_execution_service, sts.ResumeLock, sts.supports_session_resume)
    tes.get_task_execution_service = lambda: _Service()
    sts.ResumeLock = _NoLock
    sts.supports_session_resume = lambda a: True
    try:
        _run(sts.run_resumable_turn(
            agent_name=AGENT, session_key=SESSION, message="hi",
            cold_message="hi (cold)", cached_uuid="uuid-1", triggered_by="public",
            execution_id="exec_1", model=OPUS,
        ))
    finally:
        (tes.get_task_execution_service, sts.ResumeLock,
         sts.supports_session_resume) = originals

    assert len(calls) == 2, "the cold retry did not fire — the fixture no longer models it"
    assert calls[0]["execution_id"] == "exec_1"     # attempt 1 adopts the pre-created row
    assert "execution_id" not in calls[1]           # attempt 2 makes its OWN row…
    assert calls[0]["model"] == calls[1]["model"] == OPUS   # …on the same model


# ---------------------------------------------------------------------------
# 4. The failure ladder: honest, and never a lie about billing
# ---------------------------------------------------------------------------

def _failed_turn(svc, monkeypatch, *, code, error=""):
    """Drive `portal_chat` to one terminal, with everything else stubbed out."""
    result = types.SimpleNamespace(status="failed", error=error, error_code=code,
                                   response="", cost=None, session_id=None)
    turn = types.SimpleNamespace(result=result, real_uuid=None)

    async def _ready(name):
        return "ready"

    async def _run_turn(*a, **kw):
        return turn

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_agent_availability", _ready)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: s or SESSION)
    monkeypatch.setattr(svc, "_persist_user_turn", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_run_sync_turn_and_clear_marker", _run_turn)
    monkeypatch.setattr(svc, "_precreate_sync_execution", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_title_plan", lambda *a, **kw: None)

    async def _inbox(agent_name, email, message):
        return ([], [], [])

    monkeypatch.setattr(svc, "_collect_inbox_for_turn", _inbox)
    return svc


def test_a_generic_failure_names_the_chosen_model_and_asks_for_the_clear(svc, monkeypatch):
    """The only branch that mentions the model, and it self-heals.

    `invalid_model` is what the client keys the clear on — so the sentence
    "switched back to the agent's default" is TRUE on the next turn, instead of
    looping the person into the same failure on every retry and every reload.
    A copy-only degradation would keep sending the same model forever.
    """
    from client_portal.service import ClientPortalError

    _failed_turn(svc, monkeypatch, code=None, error="something broke")

    with pytest.raises(ClientPortalError) as exc:
        _run(svc.portal_chat(AGENT, "hi", EMAIL, session_id=SESSION, model=OPUS))

    assert exc.value.category == "invalid_model"
    assert "Claude Opus 5" in exc.value.detail          # the LABEL, not the id
    assert "agent's default" in exc.value.detail.lower()
    assert exc.value.category in svc.PORTAL_FAILURE_CATEGORIES


def test_the_generic_failure_is_unchanged_when_no_model_was_chosen(svc, monkeypatch):
    """A default turn's failure copy must not change at all — most turns are
    default turns, and this is the branch they land in."""
    from client_portal.service import ClientPortalError

    _failed_turn(svc, monkeypatch, code=None, error="something broke")

    with pytest.raises(ClientPortalError) as exc:
        _run(svc.portal_chat(AGENT, "hi", EMAIL, session_id=SESSION))

    assert exc.value.category == "agent_error"
    assert "Opus" not in exc.value.detail


@pytest.mark.parametrize("code", ["AUTH", "BILLING"])
def test_a_usage_limit_never_blames_the_model(svc, monkeypatch, code):
    """THE regression guard on the tempting version of this feature.

    There is no "this model is unavailable" code in the #2320 ladder
    (`_PULL_ERROR_CODES` has no model member), and AUTH/BILLING merge into ONE
    "reached its usage limit" answer with a true and specific cause. Rewording
    that branch whenever a model was chosen would tell a person their model
    choice broke a turn that an exhausted subscription broke — and would send
    them round the dropdown looking for a model that works.
    """
    from client_portal.service import ClientPortalError

    _failed_turn(svc, monkeypatch, code=code)

    with pytest.raises(ClientPortalError) as exc:
        _run(svc.portal_chat(AGENT, "hi", EMAIL, session_id=SESSION, model=OPUS))

    assert exc.value.category == "auth"
    assert "usage limit" in exc.value.detail
    assert "Opus" not in exc.value.detail


@pytest.mark.parametrize("code,expected", [("CAPACITY", "capacity"), ("TIMEOUT", "timeout")])
def test_capacity_and_timeout_are_untouched_too(svc, monkeypatch, code, expected):
    """Same reason as the pair above: each has a true, specific cause that has
    nothing to do with the model."""
    from client_portal.service import ClientPortalError

    _failed_turn(svc, monkeypatch, code=code)

    with pytest.raises(ClientPortalError) as exc:
        _run(svc.portal_chat(AGENT, "hi", EMAIL, session_id=SESSION, model=OPUS))

    assert exc.value.category == expected
    assert "Opus" not in exc.value.detail


# ---------------------------------------------------------------------------
# 5. The card's capability bit
# ---------------------------------------------------------------------------

def _card(svc, *, is_platform=True, runtime="claude-code", override=None, ctx=None):
    row = {"agent_name": AGENT, "public_channel_model": override}
    return svc._row_to_card(row, False, None, availability="ready",
                            is_platform=is_platform, runtime=runtime,
                            model_context=ctx if ctx is not None else svc._model_context())


def test_the_control_is_absent_for_a_client_portal_principal(svc):
    """The roster payload is the ONLY capability channel an external client has
    (#2128). A UI gate written against `GET /api/settings/feature-flags` is
    `get_current_user`-gated and returns empty for exactly that audience, so it
    would be dead where it matters."""
    assert _card(svc, is_platform=False).model_default is None


def test_the_control_is_absent_on_a_non_claude_runtime(svc):
    """The curated set is Claude-only and the platform passes no `--model` to
    the Codex runtime at all, so the control there would promise something and
    change nothing."""
    assert _card(svc, runtime="codex").model_default is None
    assert _card(svc, runtime="gemini").model_default is None


def test_an_unreadable_runtime_fails_open_to_the_claude_default(svc):
    """Fail OPEN here, matching `get_agent_runtime`'s own documented fallback.
    Codex is the exception; hiding a working control on EVERY Claude agent
    because one Docker read hiccuped is the #2196 inversion."""
    assert svc._DEFAULT_RUNTIME == "claude-code"
    assert _card(svc, runtime=svc._DEFAULT_RUNTIME).model_default is not None


def test_the_card_reports_the_agents_own_override_when_one_is_set(svc):
    card = _card(svc, override=HAIKU)
    assert card.model_default.model == HAIKU
    assert card.model_default.source == "agent"
    assert card.model_default.label == "Claude Haiku 4.5"


def test_a_stale_override_degrades_on_the_CARD_exactly_as_it_does_at_TURN_time(svc):
    """The label and the turn must not disagree about one stale value.

    `db.get_public_channel_model` already degrades an allow-list-absent stored
    value to "unset" (#1080). The card runs the same check, so an agent whose
    override was retired reads "Agent's default (<platform default>)" — which is
    what the turn will actually do.
    """
    card = _card(svc, override="claude-model-that-was-retired")
    assert card.model_default.source == "platform"


def test_an_off_catalog_platform_default_still_builds_the_roster(svc):
    """`platform_default_model` is written through the generic
    `PUT /api/settings/{key}` with NO catalog check, and free-text ids like
    `claude-sonnet-4-6[1m]` are in legitimate circulation. A bare catalog lookup
    would 500 the roster — this surface's front door — over a display string."""
    free_text = "claude-sonnet-4-6[1m]"
    ctx = svc.ModelContext(options=svc.workspace_model_options(),
                           platform_default=free_text,
                           platform_label=svc.catalog_label(free_text))

    card = _card(svc, ctx=ctx)
    assert card.model_default.model == free_text
    assert card.model_default.label == free_text     # degrades to the id, never blank
    assert svc.catalog_label(free_text) == free_text


def test_an_empty_option_list_renders_no_control(svc):
    """Fail closed from the other side: a control with nothing to choose is a
    dead one, and an empty list is what a failed catalog read produces."""
    empty = svc.ModelContext(options=[], platform_default="claude-sonnet-4-6",
                             platform_label="Claude Sonnet 4.6")
    assert _card(svc, ctx=empty).model_default is None


def test_the_option_list_is_derived_from_the_one_catalog(svc):
    """Never a second hand-typed list — the drift #2086 exists to prevent."""
    from services.model_catalog import MODEL_CATALOG

    options = svc.workspace_model_options()
    assert [o.id for o in options] == [m.id for m in MODEL_CATALOG if m.workspace]
    assert all(o.tier and o.label for o in options)


def test_row_to_cards_capability_arguments_have_no_defaults(svc):
    """A default would let a call site keep compiling while silently serving the
    wrong card — and the safe value differs per call site rather than being a
    property of the function."""
    import inspect

    params = inspect.signature(svc._row_to_card).parameters
    for name in ("is_platform", "runtime", "model_context"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is inspect.Parameter.empty, name


# ---------------------------------------------------------------------------
# 6. The router: the door, and the idempotency scope
# ---------------------------------------------------------------------------

def test_both_turn_routes_validate_the_model():
    """Enforcement at BOTH entry points. `POST /chat` runs an inline path and is
    ent#83's documented headless surface AND the browser's fallback when
    streaming fails — a check on one route only is a gap that opens exactly when
    the other one is in use."""
    import inspect
    from client_portal import router as r

    for fn in (r.portal_chat, r.portal_chat_stream):
        assert "validate_requested_model" in inspect.getsource(fn), fn.__name__


def test_the_idempotency_scope_carries_the_requested_model():
    """Invariant #18. Same key + a DIFFERENT model must not replay the previous
    turn's snapshot — that would silently ignore the change the user just made.

    The REQUESTED value and not the resolved one, deliberately: an owner editing
    `public_channel_model` between two genuine retries of ONE request must not
    fork the scope and turn a replay into a second billed turn.
    """
    import inspect
    from client_portal import router as r

    src = inspect.getsource(r.portal_chat_stream)
    assert 'scope = f"portal_stream:{agent_name}:{email}:{requested_model or \'-\'}"' in src
    # …and the validation happens BEFORE the scope is built, so a refused model
    # never consumes the key.
    assert src.index("validate_requested_model") < src.index("scope = f")

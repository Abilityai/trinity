"""
Tests for Per-User Persistent Memory for Public Link Agents (MEM-001).

Memory is scoped to (agent_name, user_email), persists cross-session,
and is injected into the system prompt for email-verified sessions.
Background summarization via claude-haiku fires every 5 messages.

#895 split storage: memory_text is a JSON blob with two named sections —
``agent_notes`` (written by the write_user_memory MCP tool) and
``conversation_summary`` (written by the background summarizer). Each
writer touches only its own section. The block injected into the system
prompt renders both sections when present.

#895 channel injection: the channel adapter path (Slack/Telegram/WhatsApp)
mirrors the web path — memory is fetched, formatted, and passed as
``system_prompt`` to ``execute_task``, gated on ``verified_email and not
is_group``. Group mode is excluded to prevent PII leak (the unlocker's
memory would be injected into replies addressed to other group members).

Run with: pytest tests/test_public_user_memory.py -v
Feature: Issue #147, #895
"""

import json
import os
import pytest
import sqlite3
import httpx

BASE_URL = "http://localhost:8000"


@pytest.fixture(scope="module")
def auth_headers():
    """Get auth headers for authenticated requests."""
    password = os.getenv("TRINITY_TEST_PASSWORD", "password")
    response = httpx.post(
        f"{BASE_URL}/api/token",
        data={"username": "admin", "password": password}
    )
    if response.status_code != 200:
        pytest.skip("Could not authenticate — check admin credentials")
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ============================================================================
# DB Table Existence
# ============================================================================

class TestPublicUserMemoryTable:
    """Verify public_user_memory table and index were created by migration."""

    @pytest.mark.smoke
    def test_table_exists(self):
        """public_user_memory table exists in the database."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='public_user_memory'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None, "public_user_memory table not found — migration may not have run"

    @pytest.mark.smoke
    def test_table_schema(self):
        """public_user_memory has the expected columns."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(public_user_memory)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        expected = {"id", "agent_name", "user_email", "memory_text", "message_count", "created_at", "updated_at"}
        assert expected == columns, f"Schema mismatch. Got: {columns}"

    @pytest.mark.smoke
    def test_unique_constraint_on_agent_email(self):
        """UNIQUE(agent_name, user_email) constraint exists."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='public_user_memory'")
        ddl = cursor.fetchone()[0]
        conn.close()

        assert "UNIQUE" in ddl.upper(), "Expected UNIQUE constraint in table DDL"

    @pytest.mark.smoke
    def test_lookup_index_exists(self):
        """idx_public_user_memory_lookup index exists."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_public_user_memory_lookup'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None, "idx_public_user_memory_lookup index not found"


# ============================================================================
# Anonymous Session — Memory Not Created
# ============================================================================

class TestAnonymousSessionMemoryIsolation:
    """Anonymous sessions must not create memory records."""

    @pytest.mark.smoke
    def test_invalid_link_returns_not_found(self):
        """Sanity check: public chat with invalid token returns 404."""
        response = httpx.post(
            f"{BASE_URL}/api/public/chat/nonexistent-token-xyz",
            json={"message": "Hello", "session_id": "anon-test-001"}
        )
        assert response.status_code == 404

    @pytest.mark.smoke
    def test_anonymous_chat_does_not_create_memory(self, auth_headers):
        """Anonymous chat sessions don't create public_user_memory rows."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        # Count memory rows before
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM public_user_memory")
        count_before = cursor.fetchone()[0]
        conn.close()

        # Create a temporary agent + anonymous public link
        agent_name = "mem-test-anon-001"
        try:
            r = httpx.post(
                f"{BASE_URL}/api/agents",
                json={"name": agent_name},
                headers=auth_headers
            )
            if r.status_code not in (200, 201):
                pytest.skip("Could not create test agent")

            link_r = httpx.post(
                f"{BASE_URL}/api/agents/{agent_name}/public-links",
                json={"name": "anon-test-link", "require_email": False},
                headers=auth_headers
            )
            if link_r.status_code != 200:
                pytest.skip("Could not create public link")

            token = link_r.json()["token"]

            # Fire anonymous chat (agent likely not running → 503, that's fine)
            httpx.post(
                f"{BASE_URL}/api/public/chat/{token}",
                json={"message": "Hello", "session_id": "anon-session-no-memory"},
                timeout=10.0
            )

        finally:
            httpx.delete(f"{BASE_URL}/api/agents/{agent_name}", headers=auth_headers)

        # Count memory rows after — must not have increased
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM public_user_memory")
        count_after = cursor.fetchone()[0]
        conn.close()

        assert count_after == count_before, (
            f"Anonymous session created {count_after - count_before} unexpected memory row(s)"
        )


# ============================================================================
# Memory DB Operations via API Layer
# ============================================================================

class TestMemoryDatabaseOperations:
    """Verify the DB helper methods work correctly via direct SQLite access."""

    @pytest.mark.smoke
    def test_get_or_create_memory_creates_empty_row(self):
        """Direct DB: get_or_create inserts row with empty memory_text."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        import secrets
        agent = f"test-mem-ops-{secrets.token_hex(4)}"
        email = f"mem-test-{secrets.token_hex(4)}@example.com"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            # Simulate what get_or_create_user_memory does
            import datetime
            now = datetime.datetime.utcnow().isoformat()
            memory_id = secrets.token_urlsafe(16)
            cursor.execute("""
                INSERT INTO public_user_memory
                (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                VALUES (?, ?, ?, '', 0, ?, ?)
            """, (memory_id, agent, email.lower(), now, now))
            conn.commit()

            # Verify the row
            cursor.execute(
                "SELECT * FROM public_user_memory WHERE agent_name=? AND user_email=?",
                (agent, email.lower())
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["memory_text"] == ""
            assert row["message_count"] == 0

        finally:
            cursor.execute(
                "DELETE FROM public_user_memory WHERE agent_name=? AND user_email=?",
                (agent, email.lower())
            )
            conn.commit()
            conn.close()

    @pytest.mark.smoke
    def test_update_memory_text(self):
        """Direct DB: update_user_memory writes new text."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        import secrets, datetime
        agent = f"test-mem-upd-{secrets.token_hex(4)}"
        email = f"mem-upd-{secrets.token_hex(4)}@example.com"
        now = datetime.datetime.utcnow().isoformat()
        memory_id = secrets.token_urlsafe(16)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO public_user_memory
                (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                VALUES (?, ?, ?, '', 0, ?, ?)
            """, (memory_id, agent, email.lower(), now, now))

            # Update memory text
            cursor.execute("""
                UPDATE public_user_memory SET memory_text=?, updated_at=?
                WHERE agent_name=? AND user_email=?
            """, ("- Name: Alice\n- Prefers Python", now, agent, email.lower()))
            conn.commit()

            cursor.execute(
                "SELECT memory_text FROM public_user_memory WHERE agent_name=? AND user_email=?",
                (agent, email.lower())
            )
            row = cursor.fetchone()
            assert row["memory_text"] == "- Name: Alice\n- Prefers Python"

        finally:
            cursor.execute(
                "DELETE FROM public_user_memory WHERE agent_name=? AND user_email=?",
                (agent, email.lower())
            )
            conn.commit()
            conn.close()

    @pytest.mark.smoke
    def test_unique_constraint_per_agent_and_email(self):
        """Direct DB: UNIQUE(agent_name, user_email) prevents duplicates."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        import secrets, datetime
        agent = f"test-mem-uniq-{secrets.token_hex(4)}"
        email = f"uniq-{secrets.token_hex(4)}@example.com"
        now = datetime.datetime.utcnow().isoformat()

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO public_user_memory
                (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                VALUES (?, ?, ?, '', 0, ?, ?)
            """, (secrets.token_urlsafe(16), agent, email.lower(), now, now))
            conn.commit()

            # Second insert for same (agent, email) must fail
            with pytest.raises(sqlite3.IntegrityError):
                cursor.execute("""
                    INSERT INTO public_user_memory
                    (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                    VALUES (?, ?, ?, '', 0, ?, ?)
                """, (secrets.token_urlsafe(16), agent, email.lower(), now, now))
                conn.commit()

        finally:
            conn.rollback()
            cursor.execute(
                "DELETE FROM public_user_memory WHERE agent_name=? AND user_email=?",
                (agent, email.lower())
            )
            conn.commit()
            conn.close()

    @pytest.mark.smoke
    def test_memory_scoped_per_agent(self):
        """Direct DB: different agents get separate memory rows for the same email."""
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        import secrets, datetime
        email = f"shared-{secrets.token_hex(4)}@example.com"
        agent_a = f"test-mem-a-{secrets.token_hex(4)}"
        agent_b = f"test-mem-b-{secrets.token_hex(4)}"
        now = datetime.datetime.utcnow().isoformat()
        id_a = secrets.token_urlsafe(16)
        id_b = secrets.token_urlsafe(16)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO public_user_memory
                (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                VALUES (?, ?, ?, 'memory-a', 0, ?, ?)
            """, (id_a, agent_a, email.lower(), now, now))
            cursor.execute("""
                INSERT INTO public_user_memory
                (id, agent_name, user_email, memory_text, message_count, created_at, updated_at)
                VALUES (?, ?, ?, 'memory-b', 0, ?, ?)
            """, (id_b, agent_b, email.lower(), now, now))
            conn.commit()

            cursor.execute(
                "SELECT id FROM public_user_memory WHERE agent_name=? AND user_email=?",
                (agent_a, email.lower())
            )
            row_a = cursor.fetchone()
            cursor.execute(
                "SELECT id FROM public_user_memory WHERE agent_name=? AND user_email=?",
                (agent_b, email.lower())
            )
            row_b = cursor.fetchone()

            assert row_a[0] != row_b[0], "Expected separate rows for different agents"

        finally:
            cursor.execute(
                "DELETE FROM public_user_memory WHERE agent_name IN (?, ?) AND user_email=?",
                (agent_a, agent_b, email.lower())
            )
            conn.commit()
            conn.close()


# ============================================================================
# #895 split storage — parser/encoder behavior
# ============================================================================

def _parse_memory_blob(memory_text):
    """Inline mirror of db.public_links._parse_memory_blob (#895).

    Mirrors the production implementation to keep these tests independent
    of the backend import chain in the test environment.
    """
    if not memory_text:
        return {"agent_notes": "", "conversation_summary": ""}
    try:
        data = json.loads(memory_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"agent_notes": "", "conversation_summary": memory_text}
    if not isinstance(data, dict):
        return {"agent_notes": "", "conversation_summary": memory_text}
    return {
        "agent_notes": str(data.get("agent_notes") or ""),
        "conversation_summary": str(data.get("conversation_summary") or ""),
    }


def _encode_memory_blob(agent_notes, conversation_summary):
    return json.dumps(
        {
            "agent_notes": agent_notes or "",
            "conversation_summary": conversation_summary or "",
        },
        ensure_ascii=False,
    )


class TestParseMemoryBlob:
    """The parser must round-trip JSON, gracefully handle legacy plaintext,
    and never crash on malformed input."""

    @pytest.mark.smoke
    def test_none_returns_empty_sections(self):
        assert _parse_memory_blob(None) == {
            "agent_notes": "", "conversation_summary": ""
        }

    @pytest.mark.smoke
    def test_empty_string_returns_empty_sections(self):
        assert _parse_memory_blob("") == {
            "agent_notes": "", "conversation_summary": ""
        }

    @pytest.mark.smoke
    def test_legacy_plaintext_becomes_conversation_summary(self):
        # Rows written before #895 are raw text from the old summarizer.
        # Surface them as conversation_summary so existing memory keeps
        # working transparently after deploy.
        blob = "- User likes pizza\n- Lives in PST"
        parsed = _parse_memory_blob(blob)
        assert parsed == {
            "agent_notes": "",
            "conversation_summary": blob,
        }

    @pytest.mark.smoke
    def test_json_with_both_keys_round_trips(self):
        encoded = _encode_memory_blob("Name: Alice", "Prefers Python")
        parsed = _parse_memory_blob(encoded)
        assert parsed == {
            "agent_notes": "Name: Alice",
            "conversation_summary": "Prefers Python",
        }

    @pytest.mark.smoke
    def test_json_missing_key_defaults_empty(self):
        parsed = _parse_memory_blob(json.dumps({"agent_notes": "X"}))
        assert parsed == {"agent_notes": "X", "conversation_summary": ""}

    @pytest.mark.smoke
    def test_json_null_values_become_empty_strings(self):
        parsed = _parse_memory_blob(
            json.dumps({"agent_notes": None, "conversation_summary": "Y"})
        )
        assert parsed == {"agent_notes": "", "conversation_summary": "Y"}

    @pytest.mark.smoke
    def test_non_dict_json_treated_as_plaintext(self):
        # JSON array / scalar — fall through to plaintext path to avoid
        # accidentally interpreting structured data as memory.
        parsed = _parse_memory_blob("[1, 2, 3]")
        assert parsed == {
            "agent_notes": "",
            "conversation_summary": "[1, 2, 3]",
        }


# ============================================================================
# #895 split storage — DB write helpers touch only their own section
# ============================================================================

class TestSplitStorageWrites:
    """The agent_notes writer must never modify conversation_summary
    (and vice versa). Verified at the JSON-storage layer so it does not
    depend on a running backend."""

    @pytest.mark.smoke
    def test_agent_notes_writer_preserves_summary(self):
        # Existing row already has a summary (written by the background
        # summarizer). The agent-deliberate writer arrives next.
        existing = _encode_memory_blob("", "Summary from haiku")
        current = _parse_memory_blob(existing)
        current["agent_notes"] = "Name: Alice"
        new_blob = _encode_memory_blob(**current)

        re_parsed = _parse_memory_blob(new_blob)
        assert re_parsed == {
            "agent_notes": "Name: Alice",
            "conversation_summary": "Summary from haiku",
        }

    @pytest.mark.smoke
    def test_summary_writer_preserves_agent_notes(self):
        # Agent has already written deliberate notes. Summarizer runs
        # next; it must not clobber the agent's content.
        existing = _encode_memory_blob("Name: Alice", "")
        current = _parse_memory_blob(existing)
        current["conversation_summary"] = "Refined by summarizer"
        new_blob = _encode_memory_blob(**current)

        re_parsed = _parse_memory_blob(new_blob)
        assert re_parsed == {
            "agent_notes": "Name: Alice",
            "conversation_summary": "Refined by summarizer",
        }

    @pytest.mark.smoke
    def test_legacy_plaintext_upgrades_to_summary_then_split_writes_work(self):
        # First read of a legacy row surfaces as conversation_summary.
        # When the write_user_memory tool fires after deploy, agent_notes
        # gets populated alongside the legacy text without losing it.
        legacy = "- User likes pizza"
        current = _parse_memory_blob(legacy)
        assert current["conversation_summary"] == legacy

        current["agent_notes"] = "Name: Alice"
        upgraded = _encode_memory_blob(**current)

        re_parsed = _parse_memory_blob(upgraded)
        assert re_parsed["agent_notes"] == "Name: Alice"
        assert re_parsed["conversation_summary"] == legacy


# ============================================================================
# Platform Prompt Service — format_user_memory_block (#895 multi-section)
# ============================================================================

def _format_user_memory_block(memory_record):
    """Inline mirror of services.platform_prompt_service.format_user_memory_block.

    Kept in sync with the production implementation; isolated here so the
    tests don't pull the full backend import chain.
    """
    if not isinstance(memory_record, dict):
        return None
    agent_notes = (memory_record.get("agent_notes") or "").strip()
    summary = (memory_record.get("conversation_summary") or "").strip()
    if not agent_notes and not summary:
        return None
    lines = ["## What you know about this user", ""]
    if agent_notes:
        lines.extend(["### Agent notes", "", agent_notes, ""])
    if summary:
        lines.extend(["### Conversation summary", "", summary, ""])
    lines.append("---")
    return "\n".join(lines)


class TestFormatUserMemoryBlock:
    """Output contract of the formatter — see #895 'Read path' section."""

    @pytest.mark.smoke
    def test_returns_none_for_empty_record(self):
        assert _format_user_memory_block(None) is None
        assert _format_user_memory_block({}) is None
        assert _format_user_memory_block(
            {"agent_notes": "", "conversation_summary": ""}
        ) is None
        # Whitespace-only sections also yield None so callers can skip
        # the system-prompt injection entirely.
        assert _format_user_memory_block(
            {"agent_notes": "  ", "conversation_summary": "\n\n"}
        ) is None

    @pytest.mark.smoke
    def test_block_contains_header(self):
        block = _format_user_memory_block(
            {"agent_notes": "- Name: Alice", "conversation_summary": ""}
        )
        assert "## What you know about this user" in block

    @pytest.mark.smoke
    def test_agent_notes_only_omits_summary_section(self):
        block = _format_user_memory_block(
            {"agent_notes": "- Name: Alice", "conversation_summary": ""}
        )
        assert "### Agent notes" in block
        assert "- Name: Alice" in block
        assert "### Conversation summary" not in block

    @pytest.mark.smoke
    def test_summary_only_omits_agent_notes_section(self):
        block = _format_user_memory_block(
            {"agent_notes": "", "conversation_summary": "Likes coffee"}
        )
        assert "### Conversation summary" in block
        assert "Likes coffee" in block
        assert "### Agent notes" not in block

    @pytest.mark.smoke
    def test_both_sections_render_with_agent_notes_first(self):
        # Agent-deliberate notes are higher signal than the auto-summary,
        # so they appear first in the system prompt.
        block = _format_user_memory_block(
            {
                "agent_notes": "Name: Alice",
                "conversation_summary": "Prefers Python",
            }
        )
        assert "### Agent notes" in block
        assert "### Conversation summary" in block
        assert block.index("### Agent notes") < block.index("### Conversation summary")

    @pytest.mark.smoke
    def test_block_ends_with_separator(self):
        block = _format_user_memory_block(
            {"agent_notes": "X", "conversation_summary": ""}
        )
        assert block.strip().endswith("---")


# ============================================================================
# #895 Channel injection — gating logic
# ============================================================================

def _should_inject_memory(verified_email, is_group):
    """Inline mirror of the gating check used in
    ``adapters.message_router._handle_message_inner``.

    Memory is injected only when there is a verified email AND the
    session is not a group chat. Group mode is excluded because
    ``verified_email`` there is the unlocker's email, set once per
    group — injecting their memory into replies addressed to other
    group members would leak PII across users.
    """
    return bool(verified_email) and not is_group


class TestChannelInjectionGating:
    """The new channel-injection guard must allow exactly the DM-with-
    verified-email path. Everything else (anonymous, group, observe
    mode) must skip injection."""

    @pytest.mark.smoke
    def test_dm_with_verified_email_injects(self):
        assert _should_inject_memory("alice@example.com", is_group=False) is True

    @pytest.mark.smoke
    def test_group_with_verified_unlocker_skips(self):
        # The verified email here belongs to the user who unlocked the
        # group, not the current speaker — must not inject.
        assert _should_inject_memory("alice@example.com", is_group=True) is False

    @pytest.mark.smoke
    def test_anonymous_dm_skips(self):
        assert _should_inject_memory(None, is_group=False) is False
        assert _should_inject_memory("", is_group=False) is False

    @pytest.mark.smoke
    def test_anonymous_group_skips(self):
        assert _should_inject_memory(None, is_group=True) is False
        assert _should_inject_memory("", is_group=True) is False

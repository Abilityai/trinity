"""Reserved-prefix registry for platform alert kinds (abilityai/trinity-enterprise#1111, T4).

`operator_queue_service._RESERVED_ID_PREFIXES` is an OSS literal tuple with no
registration function, so a module registered outside this repo (an enterprise
emitter) had no way to reserve the prefix its `platform_alerts.Kind` mints rows
under — and an unreserved prefix is one an agent can author, which means it can
pre-create the id of the alert ABOUT ITSELF and swallow it through
`create_item`'s ON CONFLICT DO NOTHING (the #1632 / #2744 class).

What this pins:
  * `reserve_id_prefix` makes a prefix reserved at every reader seam — the two
    ingest guards, `is_platform_minted`, the `raise_ask` refusal, and the
    cap/exclusion reads — because they all read the accessor, not the literal;
  * the literal tuple itself is NEVER mutated (`db.migrations`'s frozen heal
    list is pinned against it by `test_2372_platform_alert_ack.py`, and a paid
    feature's name does not belong in an OSS literal);
  * re-reserving is idempotent, and a malformed prefix is refused;
  * `platform_alerts.register(Kind)` verifies the kind's prefix is reserved —
    it did not, so an enterprise kind could ship with an agent-authorable
    prefix and nothing would say so.

The ent#1111 consumer is the spare-hand-over failure alert
(`member_spare_unusable`, prefix `member-spare-`): its reservation call lives
in the enterprise module, so the prefix is exercised here through the seam it
will use.
"""
import pytest

from services import operator_queue_service as oqs
from services import platform_alerts as pa

# The prefix the ent#1111 spare-failure alert reserves through this seam.
SPARE_ALERT_PREFIX = "member-spare-"


@pytest.fixture
def clean_registry():
    """Reserving mutates a module global — restore it, and `KINDS`, after."""
    saved_extra = oqs._EXTRA_RESERVED_ID_PREFIXES
    saved_kinds = dict(pa.KINDS)
    try:
        yield
    finally:
        oqs._EXTRA_RESERVED_ID_PREFIXES = saved_extra
        pa.KINDS.clear()
        pa.KINDS.update(saved_kinds)


class TestReserveIdPrefix:
    def test_the_literal_tuple_is_never_mutated(self, clean_registry):
        before = tuple(oqs._RESERVED_ID_PREFIXES)
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        assert oqs._RESERVED_ID_PREFIXES == before
        assert SPARE_ALERT_PREFIX not in oqs._RESERVED_ID_PREFIXES
        assert SPARE_ALERT_PREFIX in oqs.reserved_id_prefixes()

    def test_accessor_carries_the_literal_first(self, clean_registry):
        assert oqs.reserved_id_prefixes()[:len(oqs._RESERVED_ID_PREFIXES)] == \
            tuple(oqs._RESERVED_ID_PREFIXES)

    def test_reserving_is_idempotent(self, clean_registry):
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        first = oqs.reserved_id_prefixes()
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        assert oqs.reserved_id_prefixes() == first
        assert oqs.reserved_id_prefixes().count(SPARE_ALERT_PREFIX) == 1

    def test_reserving_an_already_literal_prefix_is_a_no_op(self, clean_registry):
        oqs.reserve_id_prefix("poison-")
        assert oqs._EXTRA_RESERVED_ID_PREFIXES == ()
        assert oqs.reserved_id_prefixes().count("poison-") == 1

    @pytest.mark.parametrize("bad", [
        "member-spare",      # no terminator: would reserve a bare word's family
        "",
        "   ",
        "-",                 # terminator only — reserves every id starting '-'
        "_",
        "Member-Spare-",     # readers lower-case the candidate: would never match
        "member spare-",     # not id-shaped
        "member/spare-",
        None,
        123,
    ])
    def test_a_malformed_prefix_is_refused(self, clean_registry, bad):
        with pytest.raises(ValueError):
            oqs.reserve_id_prefix(bad)
        assert oqs._EXTRA_RESERVED_ID_PREFIXES == ()


class TestEveryReaderSeamHonoursTheRegistry:
    """A reservation that only some seams read is not a reservation."""

    def test_is_platform_minted(self, clean_registry):
        row = {"request_id": f"{SPARE_ALERT_PREFIX}spare-1"}
        assert oqs.is_platform_minted(row) is False
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        assert oqs.is_platform_minted(row) is True

    def test_raise_ask_refuses_a_registered_prefix(self, clean_registry):
        from services import ask_service

        ask = {"request_id": f"{SPARE_ALERT_PREFIX}x", "title": "t", "type": "alert"}
        # Before: the id is the agent's to author.
        ask_service._validated_ask(dict(ask), oqs, raised_by="agent")
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        with pytest.raises(ask_service.AskRejected) as exc:
            ask_service._validated_ask(dict(ask), oqs, raised_by="agent")
        assert exc.value.code == "reserved_request_id"

    def test_no_reader_outside_the_registry_reads_the_literal(self, clean_registry):
        """The literal has exactly one live consumer family: the registry
        itself (and the frozen migration heal list it is pinned against)."""
        import ast
        from pathlib import Path

        backend = Path(__file__).resolve().parents[2] / "src" / "backend"
        allowed = {
            # the registry's own home
            "services/operator_queue_service/__init__.py",
        }
        offenders = []
        for path in backend.rglob("*.py"):
            rel = path.relative_to(backend).as_posix()
            if rel in allowed or rel.startswith("enterprise/"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "_RESERVED_ID_PREFIXES":
                    offenders.append(f"{rel}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    if any(a.name == "_RESERVED_ID_PREFIXES" for a in node.names):
                        offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, f"readers still bypass reserved_id_prefixes(): {offenders}"


class TestRegisterVerifiesThePrefix:
    def test_a_kind_whose_prefix_is_unreserved_is_refused(self, clean_registry):
        kind = pa.Kind("ent1111_probe", SPARE_ALERT_PREFIX, pa.LIFETIME_DEFAULT,
                       pa.CLEARS_HOOK)
        with pytest.raises(ValueError, match="reserved"):
            pa.register(kind)
        assert "ent1111_probe" not in pa.KINDS

    def test_a_kind_registers_once_its_prefix_is_reserved(self, clean_registry):
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        kind = pa.Kind("ent1111_probe", SPARE_ALERT_PREFIX, pa.LIFETIME_DEFAULT,
                       pa.CLEARS_HOOK, person_ending_is_final=True)
        pa.register(kind)
        assert pa.KINDS["ent1111_probe"] is kind

    def test_a_longer_id_under_a_reserved_family_is_accepted(self, clean_registry):
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        pa.register(pa.Kind("ent1111_sub", f"{SPARE_ALERT_PREFIX}docker-",
                            pa.LIFETIME_DEFAULT, pa.CLEARS_HOOK))
        assert "ent1111_sub" in pa.KINDS

    def test_a_duplicate_name_is_still_refused(self, clean_registry):
        oqs.reserve_id_prefix(SPARE_ALERT_PREFIX)
        kind = pa.Kind("ent1111_probe", SPARE_ALERT_PREFIX, pa.LIFETIME_DEFAULT,
                       pa.CLEARS_HOOK)
        pa.register(kind)
        with pytest.raises(ValueError, match="already registered"):
            pa.register(kind)

    def test_every_builtin_kind_still_registers_under_the_literal(self):
        """The verification must not have broken the kinds that ship today."""
        reserved = oqs.reserved_id_prefixes()
        loose = {k.name: k.prefix for k in pa.KINDS.values()
                 if not any(k.prefix.startswith(p) for p in reserved)}
        assert not loose, loose

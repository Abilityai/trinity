"""Capability executor port with a closed, template-controlled JSONL mapping."""
from __future__ import annotations

import hashlib
import json
import threading
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .adapter import (
    JsonLinesExchangeFactory,
    PortExchangeError,
    _claim_fresh_channel,
)
from .contracts import MAX_MESSAGE_BYTES, ProposedAction
from .ledger import EffectResult, LedgerValidationError


class CapabilityNotInstalledError(RuntimeError):
    """Raised before invocation when an action names no installed capability."""


class CapabilityExecutorPort(Protocol):
    """Model-owned boundary for one already-reserved capability action."""

    def execute(self, action: ProposedAction) -> EffectResult:
        """Return one sanitized completed or ambiguous result."""
        ...


class JsonLinesCapabilityExecutor:
    """Outer model bridge using fresh template-installed JSON Lines channels.

    DeliveryConductorTick never owns or calls this bridge. The approved model
    handoff invokes it only after run() returns one reserved ProposedAction,
    then passes its sanitized result to accept_result().
    """

    def __init__(
        self,
        installed_exchanges: Mapping[str, JsonLinesExchangeFactory],
    ) -> None:
        exchanges: dict[str, JsonLinesExchangeFactory] = {}
        for capability_name, exchange_factory in installed_exchanges.items():
            _validate_capability_name(capability_name)
            if not callable(exchange_factory):
                raise TypeError("installed exchange factory must be callable")
            exchanges[capability_name] = exchange_factory
        self._installed_exchanges = MappingProxyType(exchanges)
        self._used_channels: list[tuple[object, object]] = []
        self._channel_lock = threading.Lock()

    def execute(self, action: ProposedAction) -> EffectResult:
        if not isinstance(action, ProposedAction):
            raise TypeError("action must be a ProposedAction")
        exchange_factory = self._installed_exchanges.get(action.capability_name)
        if exchange_factory is None:
            raise CapabilityNotInstalledError("capability is not installed by the template")
        request_line = _serialize_action(action)
        try:
            exchange = exchange_factory()
            if not hasattr(exchange, "exchange"):
                raise PortExchangeError(
                    "exchange factory did not provide a JSON Lines port"
                )
            _claim_fresh_channel(exchange, self._used_channels, self._channel_lock)
            response_line = exchange.exchange(request_line)
        except PortExchangeError:
            return _ambiguous_result(action, "executor-exchange-ambiguous")
        try:
            return _parse_effect_result(response_line, expected_action_key=action.action_key)
        except (ValueError, LedgerValidationError, TypeError):
            return _ambiguous_result(action, "invalid-executor-result")


def _serialize_action(action: ProposedAction) -> str:
    value = {
        "schema_version": 1,
        "capability_name": action.capability_name,
        "action_key": action.action_key,
        "payload": json.loads(action.payload_json),
        "target_revision": action.target_revision,
        "invalidation_class": action.invalidation_class,
    }
    message = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("executor request exceeds 1 MiB")
    return message


def _parse_effect_result(message: str, *, expected_action_key: str) -> EffectResult:
    value = json.loads(message, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "action_key",
        "status",
        "result_sha256",
        "reason_code",
    }:
        raise ValueError("executor result must use the closed version-1 schema")
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise ValueError("unsupported executor result schema version")
    if value["action_key"] != expected_action_key:
        raise ValueError("executor result action key does not match the request")
    return EffectResult(
        status=value["status"],
        result_sha256=value["result_sha256"],
        reason_code=value["reason_code"],
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("executor result contains a duplicate key")
        value[key] = item
    return value


def _ambiguous_result(action: ProposedAction, reason_code: str) -> EffectResult:
    digest = hashlib.sha256(
        f"{action.action_key}:{action.target_revision}:{reason_code}".encode("utf-8")
    ).hexdigest()
    return EffectResult("ambiguous", digest, reason_code)


def _validate_capability_name(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not all(character.isalnum() or character in "._:-" for character in value)
        or not value[0].isalnum()
        or not value.isascii()
    ):
        raise ValueError("capability name must be a sanitized identifier")

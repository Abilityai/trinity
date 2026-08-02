"""Read-only policy adapter port and bounded local JSON Lines transport."""
from __future__ import annotations

import threading
from typing import BinaryIO, Protocol

from .contracts import (
    MAX_MESSAGE_BYTES,
    AdapterDecision,
    AdapterRequest,
    ContractValidationError,
    parse_adapter_decision_json,
    serialize_adapter_request,
)


_READ_LIMIT = MAX_MESSAGE_BYTES + 2


class PortExchangeError(RuntimeError):
    """Raised when a bounded local JSON Lines exchange fails closed."""


class PolicyAdapterPort(Protocol):
    """Policy boundary that observes current state before making one decision."""

    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        """Return one typed decision from read-only current observations."""
        ...


class JsonLinesExchangePort(Protocol):
    """Exchange one bounded JSON line through a template-provisioned channel."""

    def exchange(self, request_line: str) -> str:
        """Write one request line and read at most one capped response line."""
        ...


class BoundedJsonLinesExchange:
    """One request/response exchange over template-owned binary streams.

    The streams are provisioned by trusted template composition.  No wake or
    policy response can select a program, endpoint, environment, or file path.
    Reads stop at the contract cap, and a lock prevents response interleaving.
    """

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        if not hasattr(reader, "readline") or not hasattr(writer, "write"):
            raise TypeError("reader and writer must be binary streams")
        self._reader = reader
        self._writer = writer
        self._lock = threading.Lock()

    def exchange(self, request_line: str) -> str:
        request_bytes = request_line.encode("utf-8")
        if len(request_bytes) > MAX_MESSAGE_BYTES or b"\n" in request_bytes:
            raise PortExchangeError("request is not one bounded JSON line")
        with self._lock:
            try:
                written = self._writer.write(request_bytes + b"\n")
                self._writer.flush()
                response = self._reader.readline(_READ_LIMIT)
            except (OSError, TypeError, ValueError) as error:
                raise PortExchangeError("template-controlled JSON Lines port failed") from error
        if not isinstance(response, bytes):
            raise PortExchangeError("JSON Lines response must be bytes")
        if written is not None and written != len(request_bytes) + 1:
            raise PortExchangeError("JSON Lines request was only partially written")
        if len(response) >= _READ_LIMIT:
            raise PortExchangeError("JSON Lines response exceeds 1 MiB")
        if response.endswith(b"\n"):
            response = response[:-1]
        if b"\n" in response or b"\r" in response:
            raise PortExchangeError("port returned more than one JSON line")
        if len(response) > MAX_MESSAGE_BYTES:
            raise PortExchangeError("JSON Lines response exceeds 1 MiB")
        if not response:
            raise PortExchangeError("port returned an empty JSON line")
        try:
            return response.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PortExchangeError("JSON Lines response is not UTF-8") from error


class JsonLinesPolicyAdapter:
    """Parse one decision from a bounded template-owned JSON Lines channel."""

    def __init__(self, exchange: JsonLinesExchangePort) -> None:
        self._exchange = exchange

    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        request_line = serialize_adapter_request(request)
        response_line = self._exchange.exchange(request_line)
        try:
            return parse_adapter_decision_json(response_line)
        except ContractValidationError as error:
            raise PortExchangeError("adapter returned an invalid closed decision") from error
